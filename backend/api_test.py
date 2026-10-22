"""End-to-end HTTP test for the P1 analytics API.

Unlike ``smoke_test.py`` (which calls the engine directly), this exercises the
real HTTP surface - uploads, dataset switching, insights and chat - against a
running server.

Start the API first::

    .\\.venv\\Scripts\\python.exe -m uvicorn main:app --port 8000

then run::

    .\\.venv\\Scripts\\python.exe api_test.py

The suite generates its own fixture files in a temporary folder, so it never
depends on any data shipped with the app.

Exit code 0 means every assertion passed.
"""

from __future__ import annotations

import csv
import json
import random
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE = "http://127.0.0.1:8000"
TIMEOUT = 180

PASSED: List[str] = []
FAILED: List[Tuple[str, str]] = []


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _request(
    method: str,
    path: str,
    *,
    body: Optional[bytes] = None,
    content_type: str = "application/json",
) -> Tuple[int, Any]:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method=method,
        headers={"Content-Type": content_type} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload


def get(path: str) -> Tuple[int, Any]:
    return _request("GET", path)


def post_json(path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
    return _request("POST", path, body=json.dumps(payload).encode("utf-8"))


def post_files(path: str, files: List[Tuple[str, bytes]]) -> Tuple[int, Any]:
    boundary = f"----p1{uuid.uuid4().hex}"
    chunks: List[bytes] = []
    for filename, payload in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="files"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return _request(
        "POST",
        path,
        body=b"".join(chunks),
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def ask(question: str) -> Dict[str, Any]:
    status, payload = post_json("/api/chat", {"question": question})
    return payload if isinstance(payload, dict) else {"status": f"http_{status}"}


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def build_csv(path: Path, rows: int = 800) -> Path:
    random.seed(3)
    tiers = ["Starter", "Growth", "Scale", "Enterprise"]
    countries = ["United States", "Germany", "Japan", "India", "Brazil"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["account_name", "signup_date", "plan_tier", "country",
             "mrr", "seats", "support_tickets"]
        )
        for index in range(rows):
            month = (index // 60) % 12 + 1
            tier = tiers[index % len(tiers)]
            base = {"Starter": 49, "Growth": 249, "Scale": 999, "Enterprise": 4200}[tier]
            writer.writerow(
                [
                    f"Account {index + 1:04d}",
                    f"2024-{month:02d}-{(index % 28) + 1:02d}",
                    tier,
                    countries[index % len(countries)],
                    round(base * random.uniform(0.8, 1.4), 2),
                    random.randint(1, 400),
                    int(abs(random.gauss(2.5, 2.0))),
                ]
            )
    return path


def build_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Q1"
    sheet.append(["region", "product", "units", "unit_price"])
    for index in range(50):
        sheet.append(
            [["North", "South", "East", "West"][index % 4],
             ["Alpha", "Beta"][index % 2],
             (index % 20) + 3,
             19.99 + (index % 5)]
        )
    second = workbook.create_sheet("Q2")
    second.append(["region", "product", "units", "unit_price"])
    for index in range(30):
        second.append(
            [["North", "South", "East", "West"][index % 4],
             ["Alpha", "Beta"][index % 2],
             (index % 15) + 5,
             21.5 + (index % 3)]
        )
    workbook.save(str(path))
    return path


# --------------------------------------------------------------------------- #
# Chat expectations
# --------------------------------------------------------------------------- #

# question, expected status, expected analysis, draws a chart
CHAT_CASES: List[Tuple[str, str, str, bool]] = [
    ("what is the total mrr", "ok", "overview", False),
    ("mrr by plan tier", "ok", "breakdown", True),
    ("mrr over time", "ok", "trend", True),
    ("top 10 accounts by mrr", "ok", "ranking", True),
    ("distribution of mrr", "ok", "distribution", True),
    ("compare mrr by plan tier and country", "ok", "comparison", True),
    ("relationship between mrr and seats", "ok", "correlation", True),
    ("what columns do we have", "ok", "preview", False),
]


def main() -> int:
    section("Health")
    status, health = get("/api/health")
    if status != 200:
        print(f"  FAIL  the API is not reachable at {BASE} (status {status})")
        return 1
    check("health responds", status == 200)
    print(f"  model={health.get('model')} state={health.get('model_state')}")
    if health.get("model_state") != "ready":
        print("  FAIL  the model is not ready yet - wait for startup to finish")
        return 1
    check("model is ready", health.get("model_state") == "ready")

    temp_root = Path(tempfile.mkdtemp(prefix="p1-api-"))
    fixture = build_csv(temp_root / "subscriptions.csv")
    workbook = build_xlsx(temp_root / "quarterly.xlsx")

    # -- clean slate ------------------------------------------------------ #
    section("Reset existing datasets")
    status, listing = get("/api/datasets")
    for dataset in listing.get("datasets", []):
        _request("DELETE", f"/api/datasets/{dataset['id']}")
    check("starts with no datasets", len(get("/api/datasets")[1].get("datasets", [])) == 0)

    # -- upload ------------------------------------------------------------ #
    section("Upload a CSV")
    status, created = post_files("/api/datasets", [("subscriptions.csv", fixture.read_bytes())])
    check("upload accepted", status == 200, str(status))
    dataset = created.get("dataset") or {}
    dataset_id = dataset.get("id")
    check("dataset has an id", bool(dataset_id), str(created)[:200])
    check("row count is reported", dataset.get("row_count") == 800, str(dataset.get("row_count")))
    check("column count is reported", dataset.get("column_count") == 7, str(dataset.get("column_count")))
    check("no warnings", not created.get("warnings"), str(created.get("warnings")))

    insights = created.get("insights") or {}
    charts = insights.get("charts") or []
    check("insights returned charts", len(charts) >= 2, str(len(charts)))
    chart_ids = {c.get("id") for c in charts}
    check("insights include a trend", "trend" in chart_ids, str(chart_ids))
    check("insights include a breakdown", "breakdown" in chart_ids, str(chart_ids))
    summary = insights.get("summary") or {}
    check("insights report a date range", bool(summary.get("date_range")), str(summary))
    check(
        "mrr is recognised as a money column",
        summary.get("metric_unit") == "money",
        str(summary.get("metric_unit")),
    )

    section("Schema")
    status, schema = get("/api/schema")
    check("schema responds", status == 200)
    roles = {c["name"]: c["role"] for c in schema.get("columns", [])}
    check("mrr is a metric", roles.get("mrr") == "metric", str(roles))
    check("plan_tier is a dimension", roles.get("plan_tier") == "dimension", str(roles))
    check("signup_date is time", roles.get("signup_date") == "time", str(roles))
    labels = {c["name"]: c["label"] for c in schema.get("columns", [])}
    check("acronyms keep their capitals", labels.get("mrr") == "MRR", str(labels.get("mrr")))

    section("Insights endpoint")
    status, live = get("/api/insights")
    check("insights responds", status == 200)
    check("insights match the upload", len(live.get("charts") or []) == len(charts))
    for chart in live.get("charts") or []:
        check(
            f"{chart.get('id')} chart has points",
            bool(chart.get("points") or chart.get("series")),
            chart.get("type", ""),
        )

    # -- chat -------------------------------------------------------------- #
    section("Chat: every analysis")
    for question, expected_status, expected_analysis, wants_chart in CHAT_CASES:
        answer = ask(question)
        ok = (
            answer.get("status") == expected_status
            and answer.get("analysis") == expected_analysis
        )
        check(
            f"{question!r} -> {expected_analysis}",
            ok,
            f"got {answer.get('status')}/{answer.get('analysis')} ({answer.get('confidence')})",
        )
        if ok:
            if wants_chart:
                check(f"{question!r} draws a chart", bool(answer.get("chart")))
            check(f"{question!r} returns rows", (answer.get("row_count") or 0) > 0)

    section("Chat: the answer contains real markdown")
    answer = ask("mrr by plan tier")
    text = answer.get("answer") or ""
    check("answer includes a markdown table", "|" in text and "---" in text, text[:120])
    check("answer names the plan tier column", "Plan tier" in text, text[:200])
    chart = answer.get("chart") or {}
    check("breakdown chart labels are present", all(p.get("label") for p in chart.get("points", [])))

    section("Chat: refuses what it cannot do")
    off_scope = ask("tell me a joke about penguins")
    check("off-domain question is refused", off_scope.get("status") == "fallback",
          f"{off_scope.get('status')}/{off_scope.get('analysis')}")
    missing = ask("how many employees do we have")
    check("missing column is reported", missing.get("status") == "needs_column",
          f"{missing.get('status')}/{missing.get('analysis')}")
    check(
        "missing column message names the term",
        "employee" in (missing.get("answer") or "").lower(),
        (missing.get("answer") or "")[:160],
    )

    section("Chat: no dataset yet")
    _request("DELETE", f"/api/datasets/{dataset_id}")
    empty = ask("what is the total mrr")
    check("asks for an upload when empty", empty.get("status") == "needs_dataset",
          str(empty.get("status")))

    # -- excel ------------------------------------------------------------- #
    section("Upload an Excel workbook with two sheets")
    status, created_xlsx = post_files("/api/datasets", [("quarterly.xlsx", workbook.read_bytes())])
    check("workbook accepted", status == 200, str(status))
    xlsx_dataset = created_xlsx.get("dataset") or {}
    table_names = [t["name"] for t in xlsx_dataset.get("tables", [])]
    check("both sheets became tables", len(table_names) == 2, str(table_names))
    check("sheet rows add up", xlsx_dataset.get("row_count") == 80, str(xlsx_dataset.get("row_count")))
    xlsx_answer = ask("units by region")
    check("workbook data is queryable", xlsx_answer.get("status") == "ok",
          f"{xlsx_answer.get('status')}/{xlsx_answer.get('analysis')}")

    # -- switching --------------------------------------------------------- #
    section("Dataset switching")
    status, created_csv = post_files("/api/datasets", [("subscriptions.csv", fixture.read_bytes())])
    csv_id = (created_csv.get("dataset") or {}).get("id")
    status, listing = get("/api/datasets")
    check("two datasets are listed", len(listing.get("datasets") or []) == 2,
          str(len(listing.get("datasets") or [])))
    status, activated = _request(
        "POST", f"/api/datasets/{xlsx_dataset['id']}/activate", body=b""
    )
    check("activation succeeds", status == 200, str(status))
    status, schema = get("/api/schema")
    check(
        "schema follows the active dataset",
        (schema.get("dataset") or {}).get("id") == xlsx_dataset["id"],
        str((schema.get("dataset") or {}).get("id")),
    )
    _request("POST", f"/api/datasets/{csv_id}/activate", body=b"")
    status, schema = get("/api/schema")
    check(
        "switching back works",
        (schema.get("dataset") or {}).get("id") == csv_id,
        str((schema.get("dataset") or {}).get("id")),
    )

    section("Cleanup")
    for dataset_id in (csv_id, xlsx_dataset.get("id")):
        status, _ = _request("DELETE", f"/api/datasets/{dataset_id}")
        check(f"delete {dataset_id} succeeds", status == 200, str(status))
    check("everything is removed", len(get("/api/datasets")[1].get("datasets", [])) == 0)

    print("\n" + "=" * 60)
    print(f"  PASSED: {len(PASSED)}")
    print(f"  FAILED: {len(FAILED)}")
    if FAILED:
        for name, detail in FAILED:
            print(f"    - {name}: {detail}")
        print("=" * 60)
        return 1
    print("  ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
