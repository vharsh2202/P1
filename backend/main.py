"""P1 - Local Semantic Data Analytics API
=========================================

A local, dependency-light analytics service. No Docker, no cloud LLM.

    React (Vite, :5173)  --HTTP-->  FastAPI (:8000)
                                     |
                                     +-- SemanticRouter      all-MiniLM-L6-v2, CPU, local
                                     +-- SchemaBinder        question -> the user's own columns
                                     +-- Analyses             schema-agnostic SQL templates
                                     +-- DuckDB (in-memory)   <- hydrated from the user's own files

Design rules
------------
1. The app ships **no data of its own**. Everything it knows comes from a
   file the user uploaded, which the backend ingests and profiles.
2. No LLM performs text-to-SQL. The question is embedded by a small local
   sentence-transformer and matched against a catalogue of *analysis kinds*.
3. Every statement is assembled from identifiers that DuckDB itself reported.
   User text is never interpolated into SQL, so there is no injection surface.
4. If the best similarity is below the threshold the request is refused with
   an explicit fallback rather than guessed at.
5. Ambiguity is detected as well: if the top two analyses score within a
   small margin the user is offered the candidates.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from analytics.analyses import ANALYSES, ANALYSES_BY_NAME, NeedsColumn
from analytics.config import (
    AMBIGUITY_MARGIN,
    CORS_ORIGINS,
    DEVICE,
    MAX_ROWS,
    MAX_UPLOAD_FILES,
    MODEL_NAME,
    SCOPE_MARGIN,
    SIMILARITY_THRESHOLD,
    STORE_DIR,
)
from analytics.insights import build_insights
from analytics.profile import Binding, DatasetProfile, SchemaBinder
from analytics.router import SemanticRouter
from analytics.store import Dataset, DatasetStore

# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #

store = DatasetStore()
router: Optional[SemanticRouter] = None
model: Any = None
_binders: Dict[str, SchemaBinder] = {}
_binder_lock = threading.Lock()
_model_state = {"status": "loading", "error": None}


def _load_model() -> None:
    """Load the local embedding model and build the routing index."""
    global router, model
    started = time.time()
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(MODEL_NAME, device=DEVICE)
        new_router = SemanticRouter(
            model,
            analyses=ANALYSES,
            threshold=SIMILARITY_THRESHOLD,
            ambiguity_margin=AMBIGUITY_MARGIN,
            scope_margin=SCOPE_MARGIN,
        )
        new_router.build_index()
        router = new_router
        _model_state["status"] = "ready"
        _model_state["error"] = None
        print(f"[p1] model ready in {time.time() - started:.1f}s", flush=True)
    except Exception as exc:  # pragma: no cover - depends on the environment
        _model_state["status"] = "error"
        _model_state["error"] = str(exc)
        print(f"[p1] model failed to load: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.restore()
    print(f"[p1] restored {len(store.list_datasets())} dataset(s)", flush=True)
    threading.Thread(target=_load_model, name="p1-model", daemon=True).start()
    yield


app = FastAPI(
    title="P1 Analytics API",
    version="2.0.0",
    description="Local semantic analytics over your own data.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)


class OutputColumnModel(BaseModel):
    key: str
    label: str
    unit: str = "text"


class ChatResponse(BaseModel):
    status: str
    answer: str
    analysis: Optional[str] = None
    analysis_description: Optional[str] = None
    confidence: float = 0.0
    sql: Optional[str] = None
    columns: List[OutputColumnModel] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    chart: Optional[Dict[str, Any]] = None
    suggestions: List[str] = Field(default_factory=list)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    binding: Optional[Dict[str, Any]] = None
    dataset: Optional[str] = None


class DatasetCreated(BaseModel):
    dataset: Dict[str, Any]
    insights: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    """Coerce DuckDB / Python values into JSON-safe ones."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _binder_for(dataset: Dataset) -> SchemaBinder:
    """Return (and cache) the binder for a dataset."""
    with _binder_lock:
        binder = _binders.get(dataset.id)
        if binder is None:
            binder = SchemaBinder(model, dataset.profile())
            _binders[dataset.id] = binder
        return binder


def _drop_binder(dataset_id: str) -> None:
    with _binder_lock:
        _binders.pop(dataset_id, None)


def _generic_suggestions() -> List[str]:
    return [
        "What is the total?",
        "Break it down by category",
        "Show me the trend over time",
        "Which are the top 10?",
        "What columns are in this dataset?",
    ]


def _suggestions_for(dataset: Optional[Dataset]) -> List[str]:
    """Build questions the user can actually ask about *this* dataset."""
    if dataset is None:
        return _generic_suggestions()

    from analytics.profile import (
        ROLE_DIMENSION,
        default_dimension,
        default_entity,
        default_metric,
        default_time,
    )

    profile = dataset.profile()
    metric = default_metric(profile)
    dimension = default_dimension(profile)
    time_col = default_time(profile)
    entity = default_entity(profile)

    out: List[str] = []
    if metric:
        out.append(f"What is the total {metric.label.lower()}?")
        out.append(f"How is {metric.label.lower()} distributed?")
    if metric and dimension:
        out.append(f"{metric.label} by {dimension.label.lower()}")
    if metric and time_col:
        out.append(f"{metric.label} over time")
    if metric and entity:
        # "Top 10 Account name by MRR" reads wrong; drop a trailing "name".
        noun = re.sub(r"\s+name$", "", entity.label, flags=re.IGNORECASE)
        out.append(f"Top 10 {noun.lower()} by {metric.label.lower()}")
    if metric and dimension:
        others = [
            c for c in profile.columns_with_role(ROLE_DIMENSION) if c.name != dimension.name
        ]
        if others:
            out.append(
                f"Compare {metric.label} by {dimension.label.lower()} "
                f"and {others[0].label.lower()}"
            )
    out.append("What columns are in this dataset?")
    return out[:5]


def _dataset_summary(dataset: Optional[Dataset]) -> Optional[Dict[str, Any]]:
    if dataset is None:
        return None
    summary = dataset.to_summary()
    summary["active"] = True
    return summary


def _binding_dict(binding: Binding) -> Dict[str, Any]:
    return {
        "table": binding.table,
        "metric": binding.metric.name if binding.metric else None,
        "dimension": binding.dimension.name if binding.dimension else None,
        "second_dimension": binding.second_dimension.name if binding.second_dimension else None,
        "time": binding.time.name if binding.time else None,
        "entity": binding.entity.name if binding.entity else None,
        "second_metric": binding.second_metric.name if binding.second_metric else None,
    }


# --------------------------------------------------------------------------- #
# Core endpoints
# --------------------------------------------------------------------------- #


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "P1 Analytics API",
        "version": "2.0.0",
        "docs": "/docs",
        "model": MODEL_NAME,
        "model_status": _model_state["status"],
        "datasets": len(store.list_datasets()),
    }


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    dataset = store.active()
    return {
        "status": "ok" if _model_state["status"] == "ready" else _model_state["status"],
        "model": MODEL_NAME,
        "device": DEVICE,
        "model_state": _model_state["status"],
        "error": _model_state["error"],
        "analyses": len(ANALYSES),
        "threshold": SIMILARITY_THRESHOLD,
        "datasets": len(store.list_datasets()),
        "active_dataset": dataset.name if dataset else None,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/api/meta")
async def meta() -> Dict[str, Any]:
    dataset = store.active()
    return {
        "model": MODEL_NAME,
        "device": DEVICE,
        "model_state": _model_state["status"],
        "threshold": SIMILARITY_THRESHOLD,
        "analyses": [a.to_dict() for a in ANALYSES],
        "dataset": _dataset_summary(dataset),
        "datasets": store.list_datasets(),
        "suggestions": _suggestions_for(dataset),
    }


@app.get("/api/schema")
async def schema() -> Dict[str, Any]:
    dataset = store.active()
    if dataset is None:
        return {"dataset": None, "tables": [], "columns": []}
    profile = dataset.profile()
    return {
        "dataset": _dataset_summary(dataset),
        "tables": [t.to_dict() for t in profile.tables],
        "columns": [
            {**column.to_dict(), "table": table.name}
            for table in profile.tables
            for column in table.columns
        ],
    }


@app.get("/api/insights")
async def insights() -> Dict[str, Any]:
    dataset = store.active()
    if dataset is None:
        return {"summary": {}, "charts": [], "columns": []}
    return await run_in_threadpool(build_insights, dataset)


# --------------------------------------------------------------------------- #
# Dataset management
# --------------------------------------------------------------------------- #


@app.get("/api/datasets")
async def list_datasets() -> Dict[str, Any]:
    return {"datasets": store.list_datasets()}


@app.post("/api/datasets", response_model=DatasetCreated)
async def create_dataset(files: List[UploadFile] = File(...)) -> DatasetCreated:
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Upload at most {MAX_UPLOAD_FILES} files at a time.",
        )

    payloads: List[Any] = []
    for upload in files:
        data = await upload.read()
        payloads.append((upload.filename or "upload", data))
        await upload.close()

    name = payloads[0][0] if len(payloads) == 1 else f"{len(payloads)} files"
    dataset, warnings = await run_in_threadpool(store.create, name, payloads)

    if dataset is None:
        return DatasetCreated(
            dataset={},
            insights={"summary": {}, "charts": [], "columns": []},
            warnings=warnings or ["Nothing could be read from those files."],
        )

    _drop_binder(dataset.id)
    built = await run_in_threadpool(build_insights, dataset)
    return DatasetCreated(dataset=dataset.to_summary(), insights=built, warnings=warnings)


@app.post("/api/datasets/{dataset_id}/activate")
async def activate_dataset(dataset_id: str) -> Dict[str, Any]:
    dataset = store.activate(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="No such dataset.")
    return {"dataset": dataset.to_summary()}


@app.patch("/api/datasets/{dataset_id}")
async def rename_dataset(dataset_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    dataset = store.rename(dataset_id, str(payload.get("name") or ""))
    if dataset is None:
        raise HTTPException(status_code=404, detail="No such dataset.")
    return {"dataset": dataset.to_summary()}


@app.delete("/api/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str) -> Dict[str, Any]:
    _drop_binder(dataset_id)
    if not store.delete(dataset_id):
        raise HTTPException(status_code=404, detail="No such dataset.")
    return {"deleted": dataset_id, "datasets": store.list_datasets()}


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    """Route a question onto the user's own data and answer it."""
    question = payload.question.strip()
    dataset = store.active()

    if dataset is None:
        return ChatResponse(
            status="needs_dataset",
            answer=(
                "Upload a dataset first and I can answer questions about it. "
                "CSV, TSV, JSON and Excel files all work - drop one onto the "
                "upload panel and I will profile it and draw the first charts."
            ),
            suggestions=_generic_suggestions(),
        )

    if router is None:
        return ChatResponse(
            status="loading",
            answer=(
                "The language model is still warming up. "
                "Give it a few seconds and try again."
            ),
            dataset=dataset.name,
            suggestions=_suggestions_for(dataset),
        )

    route = router.route(question)
    suggestions = _suggestions_for(dataset)

    # -- fallback: the question is not about this data ---------------------- #
    if route.status == "fallback":
        if route.reason == "off_domain":
            answer = (
                "That question is outside what this assistant does. I analyse the "
                "dataset you uploaded - ask about totals, breakdowns, trends, "
                "rankings or distributions."
            )
        elif route.reason == "out_of_scope":
            answer = (
                "That does not look like a question about your data. "
                "I can total it, split it, chart it over time, rank it and "
                "profile it - try one of the suggestions below."
            )
        else:
            closest = route.candidates[0] if route.candidates else None
            hint = ""
            if closest:
                hint = (
                    f"\n\n_The closest thing I can do is **{closest['analysis']}** "
                    f"({closest['score']:.2f} similarity, threshold "
                    f"{SIMILARITY_THRESHOLD:.2f})._"
                )
            answer = (
                "I could not match that to an analysis I know how to run on this "
                f"dataset.{hint}"
            )
        return ChatResponse(
            status="fallback",
            answer=answer,
            confidence=round(route.score, 4),
            candidates=route.candidates,
            suggestions=suggestions,
            dataset=dataset.name,
        )

    # -- ambiguous --------------------------------------------------------- #
    if route.status == "ambiguous":
        names = [c["analysis"] for c in route.candidates[:3]]
        readable = ", ".join(
            f"**{ANALYSES_BY_NAME[n].description.lower()}**"
            for n in names
            if n in ANALYSES_BY_NAME
        )
        return ChatResponse(
            status="ambiguous",
            answer=(
                "That question could mean more than one thing. I can "
                f"{readable} - try naming the column or the outcome you want."
            ),
            confidence=round(route.score, 4),
            candidates=route.candidates,
            suggestions=suggestions,
            dataset=dataset.name,
        )

    # -- bind the question to the user's columns ---------------------------- #
    binder = _binder_for(dataset)
    profile = dataset.profile()
    binding = binder.bind(question)

    # -- the question names something this dataset does not have ----------- #
    if binding.unmatched and route.analysis != "preview":
        missing = ", ".join(f"**{term}**" for term in binding.unmatched[:3])
        plural_word = "columns" if len(binding.unmatched) > 1 else "column"
        return ChatResponse(
            status="needs_column",
            answer=(
                f"This dataset has no {plural_word} called {missing}. "
                f"It contains "
                + ", ".join(f"`{c.name}`" for c in profile.all_columns()[:12])
                + ". Ask about one of those instead."
            ),
            analysis=route.analysis,
            confidence=round(route.score, 4),
            candidates=route.candidates,
            suggestions=suggestions,
            binding=_binding_dict(binding),
            dataset=dataset.name,
        )

    # -- run the analysis (falling through to the next candidate) ----------- #
    attempts: List[str] = []
    if route.analysis:
        attempts.append(route.analysis)
    for candidate in route.candidates:
        name = candidate.get("analysis")
        if name and name not in attempts:
            attempts.append(name)

    last_error: Optional[str] = None
    for attempt in attempts[:3]:
        analysis = ANALYSES_BY_NAME.get(attempt)
        if analysis is None:
            continue
        try:
            plan = analysis.plan(binding, profile)
        except NeedsColumn as exc:
            last_error = exc.message
            continue

        try:
            rows = await run_in_threadpool(_execute, dataset, plan.sql, plan.params)
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"The query failed: {exc}"
            continue

        answer, chart = analysis.render(plan, rows, binding)
        return ChatResponse(
            status="ok",
            answer=answer,
            analysis=analysis.name,
            analysis_description=analysis.description,
            confidence=round(route.score, 4),
            sql=plan.sql,
            columns=[
                OutputColumnModel(key=c.key, label=c.label, unit=c.unit)
                for c in plan.columns
            ],
            rows=[{k: _jsonable(v) for k, v in row.items()} for row in rows[:MAX_ROWS]],
            row_count=len(rows),
            chart=chart,
            suggestions=suggestions,
            candidates=route.candidates,
            binding=_binding_dict(binding),
            dataset=dataset.name,
        )

    return ChatResponse(
        status="needs_column",
        answer=last_error
        or "I could not work out which columns to use for that question.",
        analysis=route.analysis,
        confidence=round(route.score, 4),
        candidates=route.candidates,
        suggestions=suggestions,
        binding=_binding_dict(binding),
        dataset=dataset.name,
    )


def _execute(dataset: Dataset, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    """Run one statement against the dataset and return dict rows."""
    with dataset.lock:
        result = dataset.con.execute(sql, params)
        columns = [d[0] for d in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


# --------------------------------------------------------------------------- #
# Optional: serve the built frontend
# --------------------------------------------------------------------------- #

_FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/app", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
