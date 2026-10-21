"""
P1 - Local Semantic Data Analytics API
======================================

A local, dependency-light analytics service. No Docker, no cloud LLM.

    React (Vite, :5173)  --HTTP-->  FastAPI (:8000)
                                     |
                                     +-- SemanticIntentRouter   all-MiniLM-L6-v2, CPU, local
                                     +-- INTENT_DATABASE        pre-written, parameter-free SQL
                                     +-- DuckDB (in-memory)      <- hydrated from static CSV files

Design rules
------------
1. No LLM performs text-to-SQL. The user question is embedded by a small local
   sentence-transformer and matched against a fixed catalogue of intents.
2. Every intent owns a hand-written SQL statement with no user input in it.
   User text is NEVER interpolated into SQL, so there is no injection surface.
3. If the best cosine similarity is below ``INTENT_THRESHOLD`` the request is
   rejected with an explicit fallback payload rather than guessing.
4. Ambiguity is detected as well: if the top two intents score within
   ``INTENT_AMBIGUITY_MARGIN`` of each other the user is asked to disambiguate.
"""

from __future__ import annotations

# The HuggingFace/torch cache must be redirected BEFORE sentence-transformers is
# imported, otherwise the model lands in the user-global cache. Keeping it in the
# project makes the app self-contained and offline-capable after the first run.
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(_BASE_DIR, ".cache", "huggingface"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import duckdb
import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer
from starlette.concurrency import run_in_threadpool

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(_BASE_DIR)
DATA_DIR = Path(os.getenv("P1_DATA_DIR", BASE_DIR / "data"))

MODEL_NAME = os.getenv("P1_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DEVICE = os.getenv("P1_EMBEDDING_DEVICE", "cpu")

# Cosine similarity floor. all-MiniLM-L6-v2 typically scores 0.40-0.90 for a
# paraphrase of a training phrase and < 0.25 for an unrelated sentence.
# Lower  -> broader coverage, more false positives.  Higher -> stricter.
SIMILARITY_THRESHOLD = float(os.getenv("P1_INTENT_THRESHOLD", "0.42"))

# If the runner-up intent scores this close to the winner, the question is
# treated as ambiguous and the user is offered the candidates.
AMBIGUITY_MARGIN = float(os.getenv("P1_INTENT_AMBIGUITY_MARGIN", "0.05"))

# If the out-of-scope class scores within this margin of the best intent, the
# question is treated as out of scope rather than answered.
SCOPE_MARGIN = float(os.getenv("P1_SCOPE_MARGIN", "0.03"))

MAX_ROWS = int(os.getenv("P1_RESULT_ROW_CAP", "500"))

CORS_ORIGINS = os.getenv(
    "P1_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

# Hard guard. A question naming one of these nouns can never be answered by the
# warehouse, so it is refused before routing even if it embeds close to an
# intent. "how many employees do we have" structurally resembles "how much
# revenue did we make" and scores 0.52 - too high to reject on similarity
# alone, so it is rejected on vocabulary instead.
OFF_DOMAIN_TERMS = frozenset(
    {
        "employee", "employees", "staff", "headcount", "salary", "salaries",
        "payroll", "weather", "forecast", "election", "president", "cricket",
        "football", "recipe", "diagnosis", "symptom", "password", "recipe",
    }
)

# The negative class of the router. These are encoded into the same index as
# the intents, but they are not intents: if the question resembles this class
# more closely than it resembles any analytical intent, it is out of scope.
OUT_OF_SCOPE_PHRASES: Tuple[str, ...] = (
    "how many employees do we have",
    "how many staff members work at this company",
    "what is our headcount",
    "how many offices do we operate",
    "how many branches do we run",
    "what is the weather tomorrow",
    "will it rain on friday",
    "who won the cricket world cup",
    "who won the football world cup",
    "what is the capital of japan",
    "who is the president of france",
    "what time does the sun set",
    "tell me a joke",
    "how do i reset my password",
    "how many calories are in an apple",
    "what is the latest news today",
    "write me a poem about autumn",
    "what is the population of mars",
    "how far is the moon",
    "what is my horoscope",
)

# --------------------------------------------------------------------------- #
# Intent catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Column:
    """Describes one output column: label + how to format its values."""

    key: str
    label: str
    fmt: str = "text"  # text | int | money | money0 | pct | num2 | month
    bullet: bool = True  # include in bullet-style summaries


@dataclass(frozen=True)
class Intent:
    """One analytical capability the assistant can answer."""

    name: str
    description: str
    examples: Tuple[str, ...]
    sql: str
    columns: Tuple[Column, ...]
    formatter: str = "table"  # kpi | table | trend
    total_row: bool = False  # append a computed Total row for full breakdowns
    sort_column: Optional[str] = None  # the column the query orders by
    suggestions: Tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# How to add a new capability:
#   1. Add an `Intent` to INTENT_DATABASE below.
#   2. Write parameter-free SQL against the `sales` / `customers` tables.
#   3. Add 3-5 realistic *paraphrases* of how a human would ask for it.
#   4. Map the result columns via the `columns` tuple.
# No other file needs to change - the router, API and UI pick it up automatically.
# --------------------------------------------------------------------------- #

INTENT_DATABASE: Tuple[Intent, ...] = (
    Intent(
        name="total_revenue",
        description="Headline revenue, profit, order count and average order value.",
        examples=(
            "what is our total revenue",
            "how much revenue did we make",
            "give me the revenue summary",
            "what are total sales so far",
            "overall revenue and profit figures",
            "show me the headline numbers",
            "how many orders do we have in total",
            "total order count and revenue",
            "how many customers do we serve",
            "how many unique customers do we have",
        ),
        sql="""
            SELECT
                ROUND(SUM(revenue), 2)                                AS total_revenue,
                ROUND(SUM(profit), 2)                                 AS total_profit,
                COUNT(DISTINCT order_id)                              AS total_orders,
                COUNT(DISTINCT customer_id)                           AS unique_customers,
                ROUND(SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0), 2) AS avg_order_value
            FROM sales
            WHERE is_returned = FALSE
        """,
        columns=(
            Column("total_revenue", "Total revenue", "money"),
            Column("total_profit", "Total profit", "money"),
            Column("total_orders", "Orders", "int"),
            Column("unique_customers", "Unique customers", "int"),
            Column("avg_order_value", "Avg order value", "money"),
        ),
        formatter="kpi",
        suggestions=("Break revenue down by region", "Show the monthly revenue trend"),
    ),
    Intent(
        name="revenue_trend",
        description="Month-by-month revenue and profit with growth rates.",
        examples=(
            "show me the monthly revenue trend",
            "how has revenue changed over time",
            "revenue growth by month",
            "plot monthly sales over the year",
            "what is our month over month growth",
            "sales trend over time",
        ),
        sql="""
            WITH monthly AS (
                SELECT
                    DATE_TRUNC('month', order_date) AS month,
                    COUNT(DISTINCT order_id)         AS orders,
                    ROUND(SUM(revenue), 2)            AS revenue,
                    ROUND(SUM(profit), 2)             AS profit,
                    ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
                FROM sales
                WHERE is_returned = FALSE
                GROUP BY 1
            ),
            with_growth AS (
                SELECT
                    month, orders, revenue, profit, margin_pct,
                    LAG(revenue) OVER (ORDER BY month) AS prev_revenue
                FROM monthly
            )
            SELECT
                month,
                orders,
                revenue,
                profit,
                margin_pct,
                ROUND(100.0 * (revenue - prev_revenue)
                      / NULLIF(prev_revenue, 0), 1) AS mom_growth_pct
            FROM with_growth
            ORDER BY month
        """,
        columns=(
            Column("month", "Month", "month"),
            Column("orders", "Orders", "int"),
            Column("revenue", "Revenue", "money"),
            Column("profit", "Profit", "money"),
            Column("margin_pct", "Margin %", "pct", bullet=False),
            Column("mom_growth_pct", "MoM %", "pct", bullet=False),
        ),
        formatter="trend",
        suggestions=("Which region is growing fastest?", "Compare profitability by category"),
    ),
    Intent(
        name="revenue_by_region",
        description="Revenue, orders and profit split by geographic region.",
        examples=(
            "revenue by region",
            "which region is the biggest market",
            "sales breakdown by geography",
            "how much does each region bring in",
            "compare regional performance",
            "regional revenue breakdown",
        ),
        sql="""
            SELECT
                region,
                COUNT(DISTINCT order_id)    AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                ROUND(SUM(revenue), 2)      AS revenue,
                ROUND(SUM(profit), 2)       AS profit,
                ROUND(100.0 * SUM(revenue) / SUM(SUM(revenue)) OVER (), 1) AS revenue_share_pct
            FROM sales
            WHERE is_returned = FALSE
            GROUP BY region
            ORDER BY revenue DESC
        """,
        columns=(
            Column("region", "Region", "text"),
            Column("orders", "Orders", "int"),
            Column("customers", "Customers", "int"),
            Column("revenue", "Revenue", "money"),
            Column("profit", "Profit", "money"),
            Column("revenue_share_pct", "Share %", "pct", bullet=False),
        ),
        total_row=True,
        sort_column="revenue",
        suggestions=("Top selling products", "Which channel performs best?"),
    ),
    Intent(
        name="revenue_by_category",
        description="Revenue and profit for each product category, with the leading product.",
        examples=(
            "revenue by category",
            "sales by product category",
            "how do our categories compare",
            "which category is the most profitable",
            "category performance breakdown",
            "compare product lines",
            "which product lines earn the most",
            "product line revenue performance",
        ),
        sql="""
            SELECT
                category,
                COUNT(DISTINCT order_id)      AS orders,
                ROUND(SUM(revenue), 2)        AS revenue,
                ROUND(SUM(profit), 2)         AS profit,
                ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct,
                ROUND(100.0 * SUM(revenue) / SUM(SUM(revenue)) OVER (), 1) AS revenue_share_pct,
                ARG_MAX(product, revenue)     AS top_product
            FROM sales
            WHERE is_returned = FALSE
            GROUP BY category
            ORDER BY revenue DESC
        """,
        columns=(
            Column("category", "Category", "text"),
            Column("orders", "Orders", "int"),
            Column("revenue", "Revenue", "money"),
            Column("profit", "Profit", "money"),
            Column("margin_pct", "Margin %", "pct", bullet=False),
            Column("revenue_share_pct", "Share %", "pct", bullet=False),
            Column("top_product", "Top product", "text", bullet=False),
        ),
        total_row=True,
        sort_column="revenue",
        suggestions=("Top 10 products by revenue", "Show revenue by region"),
    ),
    Intent(
        name="top_products",
        description="Best selling products ranked by revenue.",
        examples=(
            "what are our top selling products",
            "best sellers by revenue",
            "top 10 products",
            "which products sell the most",
            "rank our products",
            "highest revenue items",
            "which individual products earn the most",
            "our best selling individual products",
        ),
        sql="""
            SELECT
                product,
                category,
                SUM(units)                    AS units_sold,
                COUNT(DISTINCT order_id)       AS orders,
                ROUND(SUM(revenue), 2)         AS revenue,
                ROUND(SUM(profit), 2)          AS profit,
                ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
            FROM sales
            WHERE is_returned = FALSE
            GROUP BY product, category
            ORDER BY revenue DESC
            LIMIT 10
        """,
        columns=(
            Column("product", "Product", "text"),
            Column("category", "Category", "text", bullet=False),
            Column("units_sold", "Units", "int"),
            Column("orders", "Orders", "int"),
            Column("revenue", "Revenue", "money"),
            Column("profit", "Profit", "money"),
            Column("margin_pct", "Margin %", "pct", bullet=False),
        ),
        sort_column="revenue",
        suggestions=("Revenue by category", "What is our average order value?"),
    ),
    Intent(
        name="top_customers",
        description="Highest value customers with segment and loyalty tier.",
        examples=(
            "who are our top customers",
            "best customers by revenue",
            "highest value customers",
            "show me our biggest buyers",
            "customer leaderboard",
            "which customers spend the most",
        ),
        sql="""
            SELECT
                c.customer_name,
                c.segment,
                c.loyalty_tier,
                COUNT(DISTINCT s.order_id)  AS orders,
                ROUND(SUM(s.revenue), 2)    AS revenue,
                ROUND(AVG(s.revenue), 2)    AS avg_order_value
            FROM sales s
            JOIN customers c USING (customer_id)
            WHERE s.is_returned = FALSE
            GROUP BY 1, 2, 3
            ORDER BY revenue DESC
            LIMIT 10
        """,
        columns=(
            Column("customer_name", "Customer", "text"),
            Column("segment", "Segment", "text"),
            Column("loyalty_tier", "Tier", "text", bullet=False),
            Column("orders", "Orders", "int"),
            Column("revenue", "Revenue", "money"),
            Column("avg_order_value", "Avg order", "money"),
        ),
        sort_column="revenue",
        suggestions=("Revenue by customer segment", "Revenue by region"),
    ),
    Intent(
        name="customer_segments",
        description="Revenue and order behaviour per customer segment.",
        examples=(
            "how do customer segments perform",
            "revenue by customer segment",
            "enterprise vs smb vs consumer revenue",
            "compare our customer segments",
            "segment performance",
            "which segment is most valuable",
            "how many customers are in each segment",
            "customer segment breakdown",
        ),
        sql="""
            SELECT
                c.segment,
                COUNT(DISTINCT c.customer_id) AS customers,
                COUNT(DISTINCT s.order_id)    AS orders,
                ROUND(SUM(s.revenue), 2)      AS revenue,
                ROUND(AVG(s.revenue), 2)      AS avg_order_value,
                ROUND(100.0 * SUM(s.revenue) / SUM(SUM(s.revenue)) OVER (), 1) AS revenue_share_pct
            FROM sales s
            JOIN customers c USING (customer_id)
            WHERE s.is_returned = FALSE
            GROUP BY c.segment
            ORDER BY revenue DESC
        """,
        columns=(
            Column("segment", "Segment", "text"),
            Column("customers", "Customers", "int"),
            Column("orders", "Orders", "int"),
            Column("revenue", "Revenue", "money"),
            Column("avg_order_value", "Avg order", "money"),
            Column("revenue_share_pct", "Share %", "pct", bullet=False),
        ),
        total_row=True,
        sort_column="revenue",
        suggestions=("Who are our top customers?", "Which channel performs best?"),
    ),
    Intent(
        name="channel_performance",
        description="Revenue, share and margin by sales channel.",
        examples=(
            "which sales channel performs best",
            "revenue by channel",
            "compare web mobile and marketplace",
            "channel performance",
            "how much comes from each channel",
            "sales channel breakdown",
        ),
        sql="""
            SELECT
                channel,
                COUNT(DISTINCT order_id)    AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                ROUND(SUM(revenue), 2)      AS revenue,
                ROUND(100.0 * SUM(revenue) / SUM(SUM(revenue)) OVER (), 1) AS revenue_share_pct,
                ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
            FROM sales
            WHERE is_returned = FALSE
            GROUP BY channel
            ORDER BY revenue DESC
        """,
        columns=(
            Column("channel", "Channel", "text"),
            Column("orders", "Orders", "int"),
            Column("customers", "Customers", "int"),
            Column("revenue", "Revenue", "money"),
            Column("revenue_share_pct", "Share %", "pct", bullet=False),
            Column("margin_pct", "Margin %", "pct", bullet=False),
        ),
        total_row=True,
        sort_column="revenue",
        suggestions=("Revenue by region", "Which products are most profitable?"),
    ),
    Intent(
        name="profitability",
        description="Profit, margin, cost and loss-making order counts.",
        examples=(
            "are we profitable",
            "what is our profit margin",
            "profitability analysis",
            "show me profit and costs",
            "which orders lost money",
            "net profit and margin",
        ),
        sql="""
            SELECT
                ROUND(SUM(revenue), 2)   AS revenue,
                ROUND(SUM(cost), 2)      AS cost,
                ROUND(SUM(profit), 2)    AS profit,
                ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct,
                ROUND(AVG(profit), 2)    AS avg_profit_per_order,
                COUNT(DISTINCT CASE WHEN profit < 0 THEN order_id END) AS loss_making_orders,
                COUNT(DISTINCT order_id) AS total_orders
            FROM sales
            WHERE is_returned = FALSE
        """,
        columns=(
            Column("revenue", "Revenue", "money"),
            Column("cost", "Cost", "money"),
            Column("profit", "Net profit", "money"),
            Column("margin_pct", "Margin %", "pct"),
            Column("avg_profit_per_order", "Avg profit / order", "money", bullet=False),
            Column("loss_making_orders", "Loss-making orders", "int", bullet=False),
            Column("total_orders", "Total orders", "int", bullet=False),
        ),
        formatter="kpi",
        suggestions=("Revenue by category", "Which region is most profitable?"),
    ),
    Intent(
        name="average_order_value",
        description="Average, median and per-customer order economics.",
        examples=(
            "what is our average order value",
            "average basket size",
            "median order value",
            "how much does a typical customer spend",
            "average order amount",
            "typical spend per order",
        ),
        sql="""
            SELECT
                ROUND(AVG(revenue), 2)    AS avg_order_value,
                ROUND(MEDIAN(revenue), 2) AS median_order_value,
                ROUND(AVG(units), 2)      AS avg_units_per_order,
                ROUND(SUM(revenue) / NULLIF(COUNT(DISTINCT customer_id), 0), 2) AS revenue_per_customer,
                COUNT(DISTINCT order_id)  AS total_orders
            FROM sales
            WHERE is_returned = FALSE
        """,
        columns=(
            Column("avg_order_value", "Avg order value", "money"),
            Column("median_order_value", "Median order value", "money"),
            Column("avg_units_per_order", "Avg units / order", "num2"),
            Column("revenue_per_customer", "Revenue per customer", "money", bullet=False),
            Column("total_orders", "Total orders", "int", bullet=False),
        ),
        formatter="kpi",
        suggestions=("Show the monthly revenue trend", "Who are our top customers?"),
    ),
    Intent(
        name="returns_analysis",
        description="Return rate and returned value by region.",
        examples=(
            "how many orders were returned",
            "return rate by region",
            "analyze refunds and returns",
            "what is our return rate",
            "which region has the most returns",
            "returned order value",
        ),
        sql="""
            SELECT
                region,
                COUNT(DISTINCT order_id)   AS total_orders,
                COUNT(DISTINCT CASE WHEN is_returned THEN order_id END) AS returned_orders,
                ROUND(100.0 * COUNT(DISTINCT CASE WHEN is_returned THEN order_id END)
                      / NULLIF(COUNT(DISTINCT order_id), 0), 1) AS return_rate_pct,
                ROUND(SUM(CASE WHEN is_returned THEN -revenue ELSE 0 END), 2) AS returned_value,
                ROUND(SUM(revenue), 2)     AS net_revenue
            FROM sales
            GROUP BY region
            ORDER BY return_rate_pct DESC
        """,
        columns=(
            Column("region", "Region", "text"),
            Column("total_orders", "Orders", "int"),
            Column("returned_orders", "Returned", "int"),
            Column("return_rate_pct", "Return rate %", "pct"),
            Column("returned_value", "Returned value", "money", bullet=False),
            Column("net_revenue", "Net revenue", "money", bullet=False),
        ),
        total_row=True,
        sort_column="return_rate_pct",
        suggestions=("Show total revenue", "Revenue by region"),
    ),
)

INTENT_BY_NAME: Dict[str, Intent] = {i.name: i for i in INTENT_DATABASE}

# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

MISSING = "—"


def fmt_value(value: Any, kind: str) -> str:
    """Render a single cell according to its declared format kind."""
    if value is None:
        return MISSING
    try:
        if kind == "int":
            return f"{int(round(float(value))):,}"
        if kind == "money":
            return f"${float(value):,.2f}"
        if kind == "money0":
            return f"${float(value):,.0f}"
        if kind == "pct":
            return f"{float(value):,.1f}%"
        if kind == "num2":
            return f"{float(value):,.2f}"
        if kind == "month":
            if hasattr(value, "strftime"):
                return value.strftime("%Y-%m")
            return str(value)[:7]
    except (TypeError, ValueError):
        return str(value)
    return str(value)


def md_escape(text: Any) -> str:
    """Escape characters that would break a GitHub-flavoured markdown table."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Build a GFM markdown table (parsed safely by remark-gfm on the client)."""
    if not rows:
        return "_No rows returned._"
    head = "| " + " | ".join(md_escape(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(md_escape(c) for c in row) + " |" for row in rows
    ]
    return "\n".join([head, sep, *body])


def format_kpi(intent: Intent, rows: List[Dict[str, Any]], ctx: Dict[str, Any]) -> str:
    """Single-row intent rendered as a bullet list of headline figures."""
    if not rows:
        return "The query matched, but it returned no rows."
    row = rows[0]
    lines = [
        f"- **{col.label}:** {fmt_value(row.get(col.key), col.fmt)}"
        for col in intent.columns
        if col.bullet
    ]
    extra = [
        f"- **{col.label}:** {fmt_value(row.get(col.key), col.fmt)}"
        for col in intent.columns
        if not col.bullet
    ]
    lines.extend(extra)
    span = ctx.get("data_range")
    if span:
        lines.append(f"\n_Data covers {span['start']} to {span['end']}._")
    return "\n".join(lines)


def format_table(intent: Intent, rows: List[Dict[str, Any]], ctx: Dict[str, Any]) -> str:
    """Multi-row intent rendered as a markdown table with an optional total row."""
    if not rows:
        return "The query matched, but it returned no rows."

    display_rows = list(rows)
    if intent.total_row and len(rows) > 1:
        totals: Dict[str, Any] = {}
        for col in intent.columns:
            if col.fmt in ("int", "money", "money0", "num2"):
                # DuckDB can hand back int, float or Decimal depending on the
                # expression, so coerce defensively rather than type-checking.
                numeric: List[float] = []
                for row in rows:
                    raw = row.get(col.key)
                    if raw is None:
                        continue
                    try:
                        numeric.append(float(raw))
                    except (TypeError, ValueError):
                        continue
                if numeric:
                    totals[col.key] = sum(numeric)
        # Label the grouping column so the total row reads properly.
        label_col = intent.columns[0]
        if label_col.fmt == "text":
            totals[label_col.key] = "Total"
        display_rows.append(totals)

    headers = [col.label for col in intent.columns]
    body = [
        [fmt_value(r.get(col.key), col.fmt) for col in intent.columns]
        for r in display_rows
    ]
    table = md_table(headers, body)

    # Headline: name the leading group and the metric it actually leads on.
    label_col = intent.columns[0]
    metric_col = None
    if intent.sort_column:
        metric_col = next(
            (c for c in intent.columns if c.key == intent.sort_column), None
        )
    if metric_col is None:
        metric_col = next(
            (c for c in intent.columns if c.fmt in ("money", "pct", "int")), None
        )
    if metric_col is not None:
        entity = rows[0].get(label_col.key)
        headline = (
            f"**{entity}** leads with "
            f"{fmt_value(rows[0].get(metric_col.key), metric_col.fmt)} "
            f"{metric_col.label.lower()} across {len(rows)} group(s)."
        )
        return f"{headline}\n\n{table}"

    return table


def format_trend(intent: Intent, rows: List[Dict[str, Any]], ctx: Dict[str, Any]) -> str:
    """Time-series intent: narrative summary followed by the monthly table."""
    if not rows:
        return "The query matched, but it returned no rows."

    first, last = rows[0], rows[-1]
    total_rev = sum(float(r.get("revenue") or 0) for r in rows)
    growth = None
    if first.get("revenue") and last.get("revenue"):
        prev = float(first["revenue"])
        growth = (float(last["revenue"]) - prev) / prev * 100 if prev else None

    best = max(rows, key=lambda r: float(r.get("revenue") or 0))
    worst = min(rows, key=lambda r: float(r.get("revenue") or 0))

    bullets = [
        f"- **{len(rows)} months** of data, from "
        f"{fmt_value(first.get('month'), 'month')} to {fmt_value(last.get('month'), 'month')}.",
        f"- **{total_rev:,.2f}** in total revenue "
        f"(avg {total_rev / len(rows):,.2f} per month).",
    ]
    if growth is not None:
        direction = "up" if growth >= 0 else "down"
        bullets.append(
            f"- Revenue moved **{direction} {abs(growth):,.1f}%** from "
            f"{fmt_value(first.get('revenue'), 'money')} to {fmt_value(last.get('revenue'), 'money')}."
        )
    bullets.append(
        f"- Best month: **{fmt_value(best.get('month'), 'month')}** "
        f"({fmt_value(best.get('revenue'), 'money')})."
    )
    bullets.append(
        f"- Weakest month: **{fmt_value(worst.get('month'), 'month')}** "
        f"({fmt_value(worst.get('revenue'), 'money')})."
    )

    headers = [col.label for col in intent.columns]
    body = [
        [fmt_value(r.get(col.key), col.fmt) for col in intent.columns] for r in rows
    ]
    return "\n".join(bullets) + "\n\n" + md_table(headers, body)


FORMATTERS: Dict[str, Callable[[Intent, List[Dict[str, Any]], Dict[str, Any]], str]] = {
    "kpi": format_kpi,
    "table": format_table,
    "trend": format_trend,
}

# --------------------------------------------------------------------------- #
# DuckDB warehouse
# --------------------------------------------------------------------------- #

SOURCE_FILES = {"sales": "sales.csv", "customers": "customers.csv"}


class Warehouse:
    """Thin, thread-safe wrapper around an in-memory DuckDB loaded from CSV.

    DuckDB connections are not safe for concurrent use, so every query is
    serialised behind a lock. That is irrelevant at this scale and removes a
    whole class of intermittent failures.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._lock = threading.Lock()
        self.schema: Dict[str, List[Dict[str, str]]] = {}
        self.row_counts: Dict[str, int] = {}
        self.data_range: Dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> None:
        con = duckdb.connect(":memory:")
        for table, filename in SOURCE_FILES.items():
            path = self._data_dir / filename
            if not path.exists():
                raise FileNotFoundError(
                    f"Missing source file '{path}'. Run `python data/generate_data.py` "
                    f"or point P1_DATA_DIR at a directory containing {filename}."
                )
            # as_posix() keeps Windows separators out of the SQL string literal,
            # which DuckDB's parser handles far more reliably.
            safe = path.resolve().as_posix().replace("'", "''")
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{safe}')")

        self._con = con
        self._describe()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def _describe(self) -> None:
        assert self._con is not None
        for table in SOURCE_FILES:
            cols = self._con.execute(f"DESCRIBE {table}").fetchall()
            self.schema[table] = [
                {"name": c[0], "type": c[1], "nullable": c[2] == "YES"} for c in cols
            ]
            self.row_counts[table] = self._con.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

        bounds = self._con.execute(
            "SELECT MIN(order_date), MAX(order_date) FROM sales"
        ).fetchone()
        if bounds and bounds[0] is not None:
            self.data_range = {"start": str(bounds[0]), "end": str(bounds[1])}

    # -- querying ----------------------------------------------------------- #

    def query(self, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Execute a trusted SQL statement and return (columns, row dicts)."""
        if self._con is None:
            raise RuntimeError("Warehouse is not connected.")
        with self._lock:
            cur = self._con.execute(sql)
            names = [d[0] for d in cur.description]
            raw = cur.fetchall()
        rows = [dict(zip(names, r)) for r in raw]
        return names, rows


# --------------------------------------------------------------------------- #
# Semantic intent router
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """Lowercase, strip punctuation edges and collapse whitespace."""
    cleaned = re.sub(r"[^\w\s%]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass
class RouteResult:
    status: str  # matched | fallback | ambiguous
    intent: Optional[Intent] = None
    score: float = 0.0
    candidates: List[Dict[str, Any]] = field(default_factory=list)


class SemanticIntentRouter:
    """Maps a natural-language question to one of the known analytical intents.

    The catalogue is embedded at import time: each intent contributes a set of
    training phrases which are encoded once into a single normalised matrix.
    Routing is then a single matrix-vector product - no LLM, no network, no
    per-request model loading.

    A second, *negative* class is encoded alongside the intents. It represents
    questions the warehouse simply cannot answer. A question whose closest
    out-of-scope phrase scores within ``scope_margin`` of its closest intent is
    rejected rather than answered, which stops structurally-similar but
    off-domain questions ("how many employees do we have") from being served by
    a revenue query.
    """

    def __init__(
        self,
        model: SentenceTransformer,
        intents: Sequence[Intent],
        threshold: float,
        ambiguity_margin: float,
        scope_phrases: Sequence[str] = (),
        scope_margin: float = 0.03,
        off_domain_terms: frozenset = OFF_DOMAIN_TERMS,
    ) -> None:
        self._model = model
        self._intents = list(intents)
        self._threshold = threshold
        self._ambiguity_margin = ambiguity_margin
        self._scope_phrases = list(scope_phrases)
        self._scope_margin = scope_margin
        self._off_domain_terms = off_domain_terms
        self._matrix: Optional[np.ndarray] = None
        self._owners: List[str] = []
        self._scope_matrix: Optional[np.ndarray] = None
        self._cache: Dict[str, RouteResult] = {}
        self._cache_lock = threading.Lock()

    # -- indexing ----------------------------------------------------------- #

    def build_index(self) -> None:
        phrases: List[str] = []
        owners: List[str] = []
        for intent in self._intents:
            for phrase in intent.examples:
                phrases.append(normalize(phrase))
                owners.append(intent.name)
        embeddings = self._model.encode(
            phrases,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._matrix = np.asarray(embeddings, dtype=np.float32)
        self._owners = owners

        if self._scope_phrases:
            scope_embeddings = self._model.encode(
                [normalize(p) for p in self._scope_phrases],
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            self._scope_matrix = np.asarray(scope_embeddings, dtype=np.float32)

    def warmup(self) -> None:
        """Force any lazy model initialisation before the first real request."""
        self._model.encode(["warmup"], normalize_embeddings=True, show_progress_bar=False)

    # -- routing ------------------------------------------------------------ #

    def route(self, question: str) -> RouteResult:
        key = normalize(question)
        if not key:
            return RouteResult(status="fallback", score=0.0)

        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        # Cheap vocabulary guard first - no point embedding a question that
        # names something the warehouse has never heard of.
        tokens = set(key.split())
        if tokens & self._off_domain_terms:
            result = RouteResult(status="fallback", score=0.0)
            with self._cache_lock:
                self._cache[key] = result
            return result

        vector = self._model.encode(
            [key], normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True
        )[0]
        similarities = self._matrix @ vector  # cosine, vectors are unit-norm

        best_per_intent: Dict[str, float] = {}
        for owner, score in zip(self._owners, similarities):
            score = float(score)
            if score > best_per_intent.get(owner, -1.0):
                best_per_intent[owner] = score

        ranked = sorted(best_per_intent.items(), key=lambda kv: kv[1], reverse=True)
        candidates = [
            {"intent": name, "score": round(score, 4)} for name, score in ranked[:3]
        ]

        best_name, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        # How close is this question to the out-of-scope class?
        scope_score = 0.0
        if self._scope_matrix is not None:
            scope_score = float(np.max(self._scope_matrix @ vector))

        if best_score < self._threshold:
            result = RouteResult(
                status="fallback", score=round(best_score, 4), candidates=candidates
            )
        elif scope_score > best_score - self._scope_margin:
            # The negative class is as close as the best intent - this question
            # is about something the warehouse does not model.
            result = RouteResult(
                status="fallback", score=round(best_score, 4), candidates=candidates
            )
        elif best_score - runner_up < self._ambiguity_margin:
            result = RouteResult(
                status="ambiguous",
                score=round(best_score, 4),
                candidates=candidates,
            )
        else:
            result = RouteResult(
                status="matched",
                intent=INTENT_BY_NAME[best_name],
                score=round(best_score, 4),
                candidates=candidates,
            )

        with self._cache_lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = result
        return result


# --------------------------------------------------------------------------- #
# API schemas
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)


class ChatResponse(BaseModel):
    status: str
    answer: str
    intent: Optional[str] = None
    intent_description: Optional[str] = None
    confidence: float = 0.0
    sql: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    suggestions: List[str] = Field(default_factory=list)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

warehouse = Warehouse(DATA_DIR)
router: Optional[SemanticIntentRouter] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the warehouse and the embedding model once, at startup."""
    global router

    warehouse.connect()

    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    router = SemanticIntentRouter(
        model=model,
        intents=INTENT_DATABASE,
        threshold=SIMILARITY_THRESHOLD,
        ambiguity_margin=AMBIGUITY_MARGIN,
        scope_phrases=OUT_OF_SCOPE_PHRASES,
        scope_margin=SCOPE_MARGIN,
        off_domain_terms=OFF_DOMAIN_TERMS,
    )
    router.build_index()
    router.warmup()
    app.state.model_name = MODEL_NAME

    try:
        yield
    finally:
        warehouse.close()


app = FastAPI(
    title="P1 Local Analytics API",
    description=(
        "Semantic intent router over a local DuckDB warehouse. "
        "No LLM is used for text-to-SQL."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _capability_list() -> str:
    return "\n".join(f"- **{i.description}**" for i in INTENT_DATABASE)


def _suggestions_for(intent: Optional[Intent]) -> List[str]:
    if intent and intent.suggestions:
        return list(intent.suggestions)
    return [
        "What is our total revenue?",
        "Show me the monthly revenue trend",
        "Revenue by region",
        "What are our top selling products?",
        "Which sales channel performs best?",
    ]


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "P1 Local Analytics API",
        "docs": "/docs",
        "endpoints": ["/api/health", "/api/meta", "/api/schema", "/api/chat"],
    }


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok" if router is not None else "starting",
        "model": MODEL_NAME,
        "intents": len(INTENT_DATABASE),
        "threshold": SIMILARITY_THRESHOLD,
    }


@app.get("/api/meta")
async def meta() -> Dict[str, Any]:
    """Everything the frontend needs to render its header and suggestion chips."""
    return {
        "model": MODEL_NAME,
        "device": DEVICE,
        "threshold": SIMILARITY_THRESHOLD,
        "ambiguity_margin": AMBIGUITY_MARGIN,
        "data_range": warehouse.data_range,
        "row_counts": warehouse.row_counts,
        "intents": [
            {
                "name": i.name,
                "description": i.description,
                "examples": list(i.examples[:3]),
            }
            for i in INTENT_DATABASE
        ],
    }


@app.get("/api/schema")
async def schema() -> Dict[str, Any]:
    """The analytical schema available to the router (tables + column types)."""
    return {
        "tables": warehouse.schema,
        "row_counts": warehouse.row_counts,
        "data_range": warehouse.data_range,
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    """Route a question to an intent, execute its SQL, and format the result."""
    if router is None:
        return ChatResponse(
            status="fallback",
            answer="The router is still loading. Please retry in a moment.",
        )

    question = payload.question.strip()
    if not question:
        return ChatResponse(
            status="fallback",
            answer="Please enter a question.",
            suggestions=_suggestions_for(None),
        )

    route = router.route(question)

    # ---- fallback: below the similarity threshold -------------------------
    if route.status == "fallback":
        best = route.candidates[0] if route.candidates else None
        hint = (
            f"\n\n_Closest match was **{best['intent']}** at "
            f"{best['score']:.2f} similarity, below the "
            f"{SIMILARITY_THRESHOLD:.2f} threshold._"
            if best
            else ""
        )
        return ChatResponse(
            status="fallback",
            answer=(
                "I could not confidently map that question to one of my supported "
                "analyses, so I did not run anything.\n\n"
                "**These are the questions I can answer:**\n"
                f"{_capability_list()}{hint}"
            ),
            confidence=route.score,
            candidates=route.candidates,
            suggestions=_suggestions_for(None),
        )

    # ---- ambiguous: two intents are too close to call ---------------------
    if route.status == "ambiguous":
        options = "\n".join(
            f"- **{c['intent']}** ({c['score']:.2f})" for c in route.candidates
        )
        return ChatResponse(
            status="ambiguous",
            answer=(
                "Your question could match more than one analysis, so I want to be "
                f"sure before running anything.\n\n**Closest matches:**\n{options}\n\n"
                "Rephrase, or pick one of the suggestions below."
            ),
            confidence=route.score,
            candidates=route.candidates,
            suggestions=_suggestions_for(None),
        )

    # ---- matched: execute the trusted SQL --------------------------------
    intent = route.intent
    assert intent is not None

    try:
        columns, rows = await run_in_threadpool(warehouse.query, intent.sql)
    except Exception as exc:  # pragma: no cover - defensive
        return ChatResponse(
            status="error",
            answer=f"The query for `{intent.name}` failed: {exc}",
            intent=intent.name,
            intent_description=intent.description,
            confidence=route.score,
            sql=intent.sql.strip(),
            suggestions=_suggestions_for(intent),
        )

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    formatter = FORMATTERS.get(intent.formatter, format_table)
    answer = formatter(
        intent, rows, {"question": question, "data_range": warehouse.data_range}
    )
    if truncated:
        answer += f"\n\n_Showing the first {MAX_ROWS} rows._"

    return ChatResponse(
        status="matched",
        answer=answer,
        intent=intent.name,
        intent_description=intent.description,
        confidence=route.score,
        sql=intent.sql.strip(),
        columns=columns,
        rows=rows,
        row_count=len(rows),
        suggestions=_suggestions_for(intent),
        candidates=route.candidates,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
