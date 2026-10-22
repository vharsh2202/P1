"""Schema-agnostic analysis templates.

Every analysis answers one *kind* of question and works against whatever
schema the user uploaded. Nothing here mentions a specific table or column;
the columns arrive through a :class:`~analytics.profile.Binding` that was
resolved from the user's own question.

SQL safety
----------
Every statement is assembled from identifiers that came out of DuckDB's own
catalogue and are re-validated by :func:`analytics.profile.quote_ident` before
they are used. The only user-supplied values that ever reach the engine are
bound as query parameters, so there is no injection surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .formatting import (
    MISSING,
    OutputColumn,
    bullets,
    compact_number,
    fmt_value,
    humanise_unit,
    markdown_table,
    plural,
    truncate,
)
from .profile import (
    Binding,
    ColumnInfo,
    DatasetProfile,
    quote_ident,
    time_bucket_expression,
)

# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


@dataclass
class Plan:
    """The SQL and presentation a template wants to run."""

    sql: str
    params: List[Any] = field(default_factory=list)
    columns: List[OutputColumn] = field(default_factory=list)
    chart_type: Optional[str] = None
    title: str = ""
    subtitle: Optional[str] = None
    x_label: str = ""
    y_label: str = ""
    unit: str = "number"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    sql: str
    columns: List[OutputColumn]
    rows: List[Dict[str, Any]]
    answer: str
    chart: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)


class NeedsColumn(Exception):
    """Raised when the user's data cannot support the requested analysis."""

    def __init__(self, role: str, message: str) -> None:
        super().__init__(message)
        self.role = role
        self.message = message


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require_metric(binding: Binding) -> ColumnInfo:
    if binding.metric is None:
        raise NeedsColumn(
            "metric",
            "I could not find a numeric column to measure. "
            "Upload a dataset with at least one number in it, or name the column "
            "you mean (for example *revenue* or *amount*).",
        )
    return binding.metric


def _require_dimension(binding: Binding) -> ColumnInfo:
    if binding.dimension is None:
        raise NeedsColumn(
            "dimension",
            "I could not find a column to group by. "
            "Name the grouping column in your question (for example *by region* "
            "or *per category*).",
        )
    return binding.dimension


def _require_time(binding: Binding) -> ColumnInfo:
    if binding.time is None:
        raise NeedsColumn(
            "time",
            "I could not find a date column in this dataset, so I cannot build a "
            "timeline. Add a date column, or ask for a breakdown instead.",
        )
    return binding.time


def _unit_for(column: Optional[ColumnInfo]) -> str:
    if column is None:
        return "number"
    if column.unit in {"money", "percent", "count"}:
        return column.unit
    return "number"


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _chart(
    chart_type: str,
    title: str,
    points: Sequence[Dict[str, Any]],
    *,
    x_label: str,
    y_label: str,
    unit: str,
    subtitle: Optional[str] = None,
    series: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "type": chart_type,
        "title": title,
        "subtitle": subtitle,
        "x_label": x_label,
        "y_label": y_label,
        "unit": unit,
        "points": list(points),
    }
    if series:
        spec["series"] = list(series)
    return spec


def _table_sql(binding: Binding) -> str:
    if not binding.table:
        raise NeedsColumn("table", "I could not work out which table to query.")
    return quote_ident(binding.table)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Analysis:
    """One analysis kind: examples for the router, plus plan/render."""

    name: str
    description: str
    examples: Tuple[str, ...]
    plan: Callable[[Binding, DatasetProfile], Plan]
    render: Callable[[Plan, List[Dict[str, Any]], Binding], Tuple[str, Optional[Dict[str, Any]]]]
    needs: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "needs": list(self.needs),
        }


# -- 1. overview ------------------------------------------------------------ #


def _plan_overview(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    unit = _unit_for(metric)
    table = _table_sql(binding)
    col = quote_ident(metric.name)
    sql = (
        f"SELECT COUNT(*) AS row_count, "
        f"COUNT({col}) AS measured_rows, "
        f"SUM({col}) AS total, "
        f"AVG({col}) AS average, "
        f"MIN({col}) AS minimum, "
        f"MAX({col}) AS maximum "
        f"FROM {table}"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("row_count", "Rows", "count"),
            OutputColumn("measured_rows", "Rows with a value", "count"),
            OutputColumn("total", f"Total {metric.label.lower()}", unit),
            OutputColumn("average", f"Average {metric.label.lower()}", unit),
            OutputColumn("minimum", "Minimum", unit),
            OutputColumn("maximum", "Maximum", unit),
        ],
        title=f"{metric.label} at a glance",
        x_label="Measure",
        y_label=metric.label,
        unit=unit,
    )


def _render_overview(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return "That table is empty.", None
    row = rows[0]
    metric = binding.metric
    unit = plan.unit
    total = row.get("total")

    lines = [
        f"**{metric.label}** across {plural(int(row.get('row_count') or 0), 'row')} "
        f"in `{binding.table}`."
    ]
    if total is not None:
        lines.append("")
        lines.append(
            bullets(
                [
                    f"**Total** {fmt_value(total, unit)}",
                    f"**Average** {fmt_value(row.get('average'), unit)}",
                    f"**Range** {fmt_value(row.get('minimum'), unit)} → "
                    f"{fmt_value(row.get('maximum'), unit)}",
                ]
            )
        )
    else:
        lines.append("")
        lines.append(
            f"The `{metric.name}` column has no non-empty values in this table."
        )
    return "\n".join(lines), None


# -- 2. breakdown ----------------------------------------------------------- #


def _plan_breakdown(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    dimension = _require_dimension(binding)
    unit = _unit_for(metric)
    table = _table_sql(binding)
    m = quote_ident(metric.name)
    d = quote_ident(dimension.name)
    sql = (
        f"SELECT {d} AS label, SUM({m}) AS value, COUNT(*) AS records "
        f"FROM {table} "
        f"WHERE {d} IS NOT NULL AND {m} IS NOT NULL "
        f"GROUP BY 1 ORDER BY value DESC"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("label", dimension.label, "text"),
            OutputColumn("value", f"Total {metric.label.lower()}", unit),
            OutputColumn("records", "Records", "count"),
        ],
        chart_type="bar",
        title=f"{metric.label} by {dimension.label.lower()}",
        x_label=dimension.label,
        y_label=f"Total {metric.label.lower()}",
        unit=unit,
    )


def _render_breakdown(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return (
            f"No rows matched **{binding.dimension.label}** and "
            f"**{binding.metric.label}** together.",
            None,
        )
    unit = plan.unit
    leader = rows[0]
    leader_label = truncate(str(leader.get("label") or MISSING), 40)
    leader_value = fmt_value(leader.get("value"), unit)

    lines = [
        f"**{binding.metric.label}** by **{binding.dimension.label.lower()}** - "
        f"{plural(len(rows), 'group')}, led by **{leader_label}** at {leader_value}."
    ]
    lines.append("")
    lines.append(markdown_table(plan.columns, rows, max_rows=25))

    points = [
        {"label": str(r.get("label") or MISSING), "value": _to_float(r.get("value"))}
        for r in rows[:20]
    ]
    chart = _chart(
        "hbar" if len(rows) > 8 else "bar",
        plan.title,
        points,
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit=unit,
        subtitle=f"{plural(len(rows), 'group')}",
    )
    return "\n".join(lines), chart


# -- 3. trend --------------------------------------------------------------- #


def _plan_trend(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    time_col = _require_time(binding)
    unit = _unit_for(metric)
    table = _table_sql(binding)
    m = quote_ident(metric.name)
    bucket = time_bucket_expression(time_col, "month")
    sql = (
        f"SELECT label, SUM(value) AS value, COUNT(*) AS records FROM ("
        f"  SELECT {bucket} AS label, {m} AS value FROM {table}"
        f") WHERE label IS NOT NULL AND value IS NOT NULL "
        f"GROUP BY 1 ORDER BY 1"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("label", "Period", "text"),
            OutputColumn("value", f"Total {metric.label.lower()}", unit),
            OutputColumn("records", "Records", "count"),
        ],
        chart_type="line",
        title=f"{metric.label} over time",
        x_label="Period",
        y_label=f"Total {metric.label.lower()}",
        unit=unit,
    )


def _render_trend(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return (
            f"No dated rows found for **{binding.metric.label}** using "
            f"`{binding.time.name}`.",
            None,
        )
    unit = plan.unit
    values = [_to_float(r.get("value")) for r in rows]
    total = sum(values)
    best_index = max(range(len(values)), key=lambda i: values[i])
    best_label = rows[best_index].get("label")
    first_label = rows[0].get("label")
    last_label = rows[-1].get("label")

    lines = [
        f"**{binding.metric.label}** by month across {plural(len(rows), 'period')} "
        f"({first_label} → {last_label})."
    ]
    lines.append("")
    lines.append(
        bullets(
            [
                f"**Total** {fmt_value(total, unit)}",
                f"**Best month** {best_label} ({fmt_value(values[best_index], unit)})",
                f"**Monthly average** "
                f"{fmt_value(total / len(rows) if rows else 0, unit)}",
            ]
        )
    )
    lines.append("")
    lines.append(markdown_table(plan.columns, rows, max_rows=24))

    points = [
        {"label": str(r.get("label") or MISSING), "value": _to_float(r.get("value"))}
        for r in rows
    ]
    chart = _chart(
        "line",
        plan.title,
        points,
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit=unit,
        subtitle=f"{first_label} → {last_label}",
    )
    return "\n".join(lines), chart


# -- 4. ranking ------------------------------------------------------------- #


def _plan_ranking(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    target = binding.entity or binding.dimension
    if target is None:
        raise NeedsColumn(
            "entity",
            "I could not find a column to rank - I need a name, id or category "
            "column such as *customer*, *product* or *region*.",
        )
    unit = _unit_for(metric)
    table = _table_sql(binding)
    m = quote_ident(metric.name)
    e = quote_ident(target.name)
    sql = (
        f"SELECT {e} AS label, SUM({m}) AS value, COUNT(*) AS records "
        f"FROM {table} "
        f"WHERE {e} IS NOT NULL AND {m} IS NOT NULL "
        f"GROUP BY 1 ORDER BY value DESC LIMIT 10"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("label", target.label, "text"),
            OutputColumn("value", f"Total {metric.label.lower()}", unit),
            OutputColumn("records", "Records", "count"),
        ],
        chart_type="hbar",
        title=f"Top 10 {target.label.lower()} by {metric.label.lower()}",
        x_label=target.label,
        y_label=f"Total {metric.label.lower()}",
        unit=unit,
    )


def _render_ranking(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return f"No rows to rank for **{binding.metric.label}**.", None
    unit = plan.unit
    top = rows[0]
    total = sum(_to_float(r.get("value")) for r in rows)
    top_value = _to_float(top.get("value"))
    share = (top_value / total * 100.0) if total else 0.0

    lines = [
        f"Top {plural(len(rows), 'entry', 'entries')} by "
        f"**{binding.metric.label.lower()}** - "
        f"**{truncate(str(top.get('label') or MISSING), 40)}** leads with "
        f"{fmt_value(top_value, unit)} ({share:.1f}% of the top-10 total)."
    ]
    lines.append("")
    lines.append(markdown_table(plan.columns, rows))

    points = [
        {"label": str(r.get("label") or MISSING), "value": _to_float(r.get("value"))}
        for r in rows
    ]
    chart = _chart(
        "hbar",
        plan.title,
        points,
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit=unit,
        subtitle=f"Top {len(rows)} of {binding.table}",
    )
    return "\n".join(lines), chart


# -- 5. distribution -------------------------------------------------------- #


def _plan_distribution(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    unit = _unit_for(metric)
    table = _table_sql(binding)
    m = quote_ident(metric.name)
    sql = (
        f"WITH src AS (SELECT {m} AS v FROM {table} WHERE {m} IS NOT NULL), "
        f"bounds AS (SELECT MIN(v) AS lo, MAX(v) AS hi FROM src), "
        f"binned AS ("
        f"  SELECT CASE WHEN bounds.hi = bounds.lo THEN 0 ELSE LEAST(9, "
        f"    CAST(FLOOR((v - bounds.lo) / NULLIF(bounds.hi - bounds.lo, 0) * 10) "
        f"    AS INTEGER)) END AS bin "
        f"  FROM src, bounds"
        f") "
        f"SELECT (SELECT lo FROM bounds) AS lo, (SELECT hi FROM bounds) AS hi, "
        f"bin, COUNT(*) AS records FROM binned GROUP BY bin ORDER BY bin"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("lo", "Low", unit),
            OutputColumn("hi", "High", unit),
            OutputColumn("bin", "Bucket", "text"),
            OutputColumn("records", "Rows", "count"),
        ],
        chart_type="histogram",
        title=f"Distribution of {metric.label.lower()}",
        x_label=metric.label,
        y_label="Rows",
        unit=unit,
    )


def _render_distribution(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return f"**{binding.metric.label}** has no values to distribute.", None
    unit = plan.unit
    total = sum(int(r.get("records") or 0) for r in rows)
    lo = _to_float(rows[0].get("lo"))
    hi = _to_float(rows[0].get("hi"))
    width = (hi - lo) / len(rows) if rows else 0.0

    points: List[Dict[str, Any]] = []
    table_rows: List[Dict[str, Any]] = []
    for row in rows:
        index = int(row.get("bin") or 0)
        low = lo + index * width
        high = low + width
        label = (
            f"{fmt_value(low, unit)}"
            if len(rows) == 1
            else f"{fmt_value(low, unit)} – {fmt_value(high, unit)}"
        )
        records = int(row.get("records") or 0)
        points.append({"label": label, "value": float(records)})
        table_rows.append({"bucket": label, "records": records})

    peak = max(points, key=lambda p: p["value"]) if points else None
    lines = [
        f"**{binding.metric.label}** spread across {len(points)} buckets "
        f"({fmt_value(lo, unit)} → {fmt_value(hi, unit)}), "
        f"{plural(total, 'row')} in total."
    ]
    if peak:
        lines.append("")
        lines.append(
            f"The busiest bucket is **{truncate(peak['label'], 40)}** with "
            f"{plural(int(peak['value']), 'row')}."
        )
    lines.append("")
    lines.append(
        markdown_table(
            [OutputColumn("bucket", "Range", "text"), OutputColumn("records", "Rows", "count")],
            table_rows,
        )
    )

    chart = _chart(
        "histogram",
        plan.title,
        points,
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit="count",
        subtitle=f"{plural(total, 'row')}",
    )
    return "\n".join(lines), chart


# -- 6. comparison ---------------------------------------------------------- #


def _plan_comparison(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    first = _require_dimension(binding)
    if binding.second_dimension is None:
        raise NeedsColumn(
            "dimension",
            "I need a second column to compare against. Name both, for example "
            "*revenue by region and product line*.",
        )
    second = binding.second_dimension
    unit = _unit_for(metric)
    table = _table_sql(binding)
    m = quote_ident(metric.name)
    d1 = quote_ident(first.name)
    d2 = quote_ident(second.name)
    sql = (
        f"SELECT {d1} AS label, {d2} AS series, SUM({m}) AS value "
        f"FROM {table} "
        f"WHERE {d1} IS NOT NULL AND {d2} IS NOT NULL AND {m} IS NOT NULL "
        f"GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("label", first.label, "text"),
            OutputColumn("series", second.label, "text"),
            OutputColumn("value", f"Total {metric.label.lower()}", unit),
        ],
        chart_type="grouped_bar",
        title=f"{metric.label} by {first.label.lower()} and {second.label.lower()}",
        x_label=first.label,
        y_label=f"Total {metric.label.lower()}",
        unit=unit,
    )


def _render_comparison(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not rows:
        return "Nothing to compare - the two columns never overlap.", None
    unit = plan.unit
    labels: List[str] = []
    series_names: List[str] = []
    values: Dict[Tuple[str, str], float] = {}
    for row in rows:
        label = str(row.get("label") or MISSING)
        series = str(row.get("series") or MISSING)
        if label not in labels:
            labels.append(label)
        if series not in series_names:
            series_names.append(series)
        values[(label, series)] = _to_float(row.get("value"))

    series = [
        {
            "label": name,
            "points": [
                {"label": label, "value": values.get((label, name), 0.0)}
                for label in labels
            ],
        }
        for name in series_names
    ]

    lines = [
        f"**{binding.metric.label}** compared across **{binding.dimension.label.lower()}** "
        f"and **{binding.second_dimension.label.lower()}** — "
        f"{plural(len(labels), 'group')}, "
        f"{plural(len(series_names), 'series')}."
    ]
    lines.append("")
    lines.append(markdown_table(plan.columns, rows, max_rows=25))

    chart = _chart(
        "grouped_bar",
        plan.title,
        [],
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit=unit,
        subtitle=", ".join(series_names[:4]),
        series=series,
    )
    return "\n".join(lines), chart


# -- 7. correlation --------------------------------------------------------- #


def _plan_correlation(binding: Binding, profile: DatasetProfile) -> Plan:
    metric = _require_metric(binding)
    if binding.second_metric is None:
        raise NeedsColumn(
            "metric",
            "I need two numeric columns to look for a relationship. Name both, "
            "for example *does revenue track order count*.",
        )
    second = binding.second_metric
    table = _table_sql(binding)
    m1 = quote_ident(metric.name)
    m2 = quote_ident(second.name)
    sql = (
        f"SELECT {m1} AS x, {m2} AS y FROM {table} "
        f"WHERE {m1} IS NOT NULL AND {m2} IS NOT NULL LIMIT 2000"
    )
    return Plan(
        sql=sql,
        columns=[
            OutputColumn("x", metric.label, _unit_for(metric)),
            OutputColumn("y", second.label, _unit_for(second)),
        ],
        chart_type="scatter",
        title=f"{metric.label} vs {second.label.lower()}",
        x_label=metric.label,
        y_label=second.label,
        unit=_unit_for(metric),
    )


def _render_correlation(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    if len(rows) < 3:
        return "There are not enough paired values to look for a relationship.", None
    xs = [_to_float(r.get("x")) for r in rows]
    ys = [_to_float(r.get("y")) for r in rows]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    r = cov / ((var_x**0.5) * (var_y**0.5)) if var_x and var_y else 0.0
    strength = (
        "strong positive" if r > 0.6 else
        "moderate positive" if r > 0.3 else
        "weak positive" if r > 0.05 else
        "strong negative" if r < -0.6 else
        "moderate negative" if r < -0.3 else
        "weak negative" if r < -0.05 else
        "no clear linear"
    )

    lines = [
        f"**{binding.metric.label}** against **{binding.second_metric.label.lower()}** "
        f"across {plural(n, 'row')} - the correlation is **{strength}** "
        f"(r = {r:.2f})."
    ]
    lines.append("")
    lines.append(
        bullets(
            [
                f"**{binding.metric.label}** mean {fmt_value(mean_x, plan.unit)}",
                f"**{binding.second_metric.label}** mean {fmt_value(mean_y, plan.unit)}",
            ]
        )
    )

    points = [
        {"label": f"{x:,.4g}", "value": y, "x": x}
        for x, y in zip(xs, ys)
    ]
    chart = _chart(
        "scatter",
        plan.title,
        points,
        x_label=plan.x_label,
        y_label=plan.y_label,
        unit=plan.unit,
        subtitle=f"r = {r:.2f}",
    )
    return "\n".join(lines), chart


# -- 8. preview ------------------------------------------------------------- #


def _plan_preview(binding: Binding, profile: DatasetProfile) -> Plan:
    table = _table_sql(binding)
    table_profile = profile.table(binding.table)
    columns = [
        OutputColumn(c.name, c.label, c.unit) for c in (table_profile.columns if table_profile else [])
    ]
    return Plan(
        sql=f"SELECT * FROM {table} LIMIT 25",
        columns=columns,
        title=f"First rows of {binding.table}",
        x_label="Column",
        y_label="Type",
        unit="text",
    )


def _render_preview(
    plan: Plan, rows: List[Dict[str, Any]], binding: Binding
) -> Tuple[str, Optional[Dict[str, Any]]]:
    profile = binding
    table = binding.table
    lines = [
        f"Here is a sample of `{table}` - the first {len(rows)} rows."
    ]
    lines.append("")
    lines.append(markdown_table(plan.columns, rows, max_rows=25))
    return "\n".join(lines), None


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

ANALYSES: Tuple[Analysis, ...] = (
    Analysis(
        name="overview",
        description="Headline totals and averages for the main measure",
        examples=(
            "give me a summary of the data",
            "what is the total",
            "how much is there in total",
            "show me the headline numbers",
            "what is the average value",
            "big picture numbers please",
            "how many rows are there",
            "what is the maximum and minimum",
            "give me an overview",
            "overall summary of the numbers",
        ),
        plan=_plan_overview,
        render=_render_overview,
        needs=("metric",),
    ),
    Analysis(
        name="breakdown",
        description="One measure split by a category",
        examples=(
            "break down the total by category",
            "total by category",
            "show me the numbers per group",
            "split the numbers by type",
            "total by country",
            "how much for each group",
            "break it down by segment",
            "values across groups",
            "total by department",
            "group the values by category",
        ),
        plan=_plan_breakdown,
        render=_render_breakdown,
        needs=("metric", "dimension"),
    ),
    Analysis(
        name="trend",
        description="A measure over time",
        examples=(
            "values over time",
            "monthly trend",
            "how has it changed over the months",
            "show me the trend by year",
            "daily volume",
            "plot the values over time",
            "how is it trending",
            "time series of the values",
            "growth month by month",
            "values by month",
        ),
        plan=_plan_trend,
        render=_render_trend,
        needs=("metric", "time"),
    ),
    Analysis(
        name="ranking",
        description="The biggest contributors",
        examples=(
            "top 10 by value",
            "which ones are the biggest",
            "largest by amount",
            "best performing entries",
            "who are the top ones",
            "rank by value",
            "biggest entries",
            "leading entries by value",
            "top sellers",
            "most valuable",
        ),
        plan=_plan_ranking,
        render=_render_ranking,
        needs=("metric", "entity"),
    ),
    Analysis(
        name="distribution",
        description="How a measure is spread",
        examples=(
            "distribution of values",
            "how are the amounts spread",
            "histogram of values",
            "show the spread of the numbers",
            "distribution of amounts",
            "how varied are the numbers",
            "bucket by value",
            "histogram of the amounts",
            "spread of values across rows",
            "what sizes do the entries come in",
        ),
        plan=_plan_distribution,
        render=_render_distribution,
        needs=("metric",),
    ),
    Analysis(
        name="comparison",
        description="A measure across two categories at once",
        examples=(
            "compare two categories",
            "two groups side by side",
            "cross tab of two categories",
            "values versus another group",
            "two dimensions at once",
            "compare two groups at the same time",
            "both dimensions together",
            "versus another category",
            "side by side comparison of two groups",
            "compare across two categories",
        ),
        plan=_plan_comparison,
        render=_render_comparison,
        needs=("metric", "dimension", "second_dimension"),
    ),
    Analysis(
        name="correlation",
        description="Whether two measures move together",
        examples=(
            "is there a relationship between two measures",
            "does one measure correlate with another",
            "relationship between two numbers",
            "plot one measure against another",
            "do higher values mean more of something else",
            "is there a correlation between them",
            "how do two measures relate",
            "check if one tracks the other",
            "do these two move together",
            "are two measures related",
        ),
        plan=_plan_correlation,
        render=_render_correlation,
        needs=("metric", "second_metric"),
    ),
    Analysis(
        name="preview",
        description="The shape of the data itself",
        examples=(
            "show me the data",
            "what columns do we have",
            "preview the table",
            "describe the dataset",
            "what does the file contain",
            "list the fields",
            "give me a sample of rows",
            "what is in this dataset",
            "show the schema",
            "let me see some rows",
        ),
        plan=_plan_preview,
        render=_render_preview,
        needs=(),
    ),
)

ANALYSES_BY_NAME: Dict[str, Analysis] = {a.name: a for a in ANALYSES}
