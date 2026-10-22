"""Automatic charts derived straight from a freshly uploaded dataset.

The moment a file lands, the service profiles it and draws the three charts
that are almost always the ones a person wants first: how the main measure
moved over time, how it splits across the main category, and how the values
are distributed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .analyses import ANALYSES_BY_NAME, AnalysisResult, NeedsColumn
from .profile import (
    Binding,
    DatasetProfile,
    default_dimension,
    default_metric,
    default_time,
    quote_ident,
)
from .store import Dataset

MAX_CHART_POINTS = 400


def _run(dataset: Dataset, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """Execute a query against the dataset and return dict rows."""
    with dataset.lock:
        result = dataset.con.execute(sql, params or [])
        columns = [d[0] for d in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


def _date_range(dataset: Dataset, profile: DatasetProfile) -> Optional[str]:
    time_col = default_time(profile)
    table = profile.primary_table
    if time_col is None or table is None:
        return None
    bucket = quote_ident(time_col.name)
    try:
        rows = _run(
            dataset,
            f"SELECT MIN({bucket}) AS lo, MAX({bucket}) AS hi FROM {table.name}",
        )
    except Exception:  # pragma: no cover - defensive
        return None
    if not rows or rows[0].get("lo") is None:
        return None
    lo = str(rows[0]["lo"])[:10]
    hi = str(rows[0]["hi"])[:10]
    return f"{lo} → {hi}"


def _run_analysis(
    dataset: Dataset,
    name: str,
    binding: Binding,
    profile: DatasetProfile,
) -> Optional[AnalysisResult]:
    """Plan, execute and render one analysis, returning None when it cannot run."""
    analysis = ANALYSES_BY_NAME.get(name)
    if analysis is None:
        return None
    try:
        plan = analysis.plan(binding, profile)
        rows = _run(dataset, plan.sql, plan.params)
        answer, chart = analysis.render(plan, rows, binding)
    except NeedsColumn:
        return None
    except Exception:  # pragma: no cover - defensive
        return None
    return AnalysisResult(
        sql=plan.sql,
        columns=plan.columns,
        rows=rows,
        answer=answer,
        chart=chart,
    )


def build_insights(dataset: Dataset) -> Dict[str, Any]:
    """Build the automatic overview for a dataset."""
    profile = dataset.profile()
    table = profile.primary_table
    if table is None:
        return {"summary": {}, "charts": [], "columns": []}

    metric = default_metric(profile)
    dimension = default_dimension(profile)
    time_col = default_time(profile)

    binding = Binding(
        table=table.name,
        metric=metric,
        dimension=dimension,
        time=time_col,
    )

    charts: List[Dict[str, Any]] = []

    if metric is not None:
        if time_col is not None:
            trend = _run_analysis(dataset, "trend", binding, profile)
            if trend and trend.chart:
                charts.append(
                    {
                        "id": "trend",
                        "caption": "Movement over time",
                        **trend.chart,
                    }
                )
        breakdown = _run_analysis(dataset, "breakdown", binding, profile)
        if breakdown and breakdown.chart:
            charts.append(
                {
                    "id": "breakdown",
                    "caption": "Split by category",
                    **breakdown.chart,
                }
            )
        distribution = _run_analysis(dataset, "distribution", binding, profile)
        if distribution and distribution.chart:
            charts.append(
                {
                    "id": "distribution",
                    "caption": "How the values are spread",
                    **distribution.chart,
                }
            )

    summary: Dict[str, Any] = {
        "row_count": table.row_count,
        "column_count": len(table.columns),
        "table_count": len(profile.tables),
        "tables": [t.name for t in profile.tables],
        "date_range": _date_range(dataset, profile),
        "primary_metric": metric.label if metric else None,
        "primary_dimension": dimension.label if dimension else None,
        "primary_time": time_col.label if time_col else None,
    }

    if metric is not None:
        try:
            rows = _run(
                dataset,
                f"SELECT SUM({quote_ident(metric.name)}) AS total, "
                f"AVG({quote_ident(metric.name)}) AS average "
                f"FROM {quote_ident(table.name)}",
            )
            if rows:
                summary["metric_total"] = float(rows[0].get("total") or 0)
                summary["metric_average"] = float(rows[0].get("average") or 0)
                summary["metric_unit"] = metric.unit
        except Exception:  # pragma: no cover - defensive
            pass

    columns = [
        {**column.to_dict(), "table": table.name}
        for column in table.columns
    ]

    return {"summary": summary, "charts": charts, "columns": columns}
