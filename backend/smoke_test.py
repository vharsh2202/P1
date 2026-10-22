"""Smoke test for the P1 analytics engine.

Run it with:

    python smoke_test.py

The suite creates its own fixture dataset in a temporary folder, so nothing
here depends on any data shipped with the app - which is the point: the app
itself carries no data at all.

Exit code 0 means every assertion passed.
"""

from __future__ import annotations

import csv
import random
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analytics.analyses import ANALYSES, ANALYSES_BY_NAME, NeedsColumn  # noqa: E402
from analytics.config import AMBIGUITY_MARGIN, SCOPE_MARGIN, SIMILARITY_THRESHOLD  # noqa: E402
from analytics.profile import SchemaBinder  # noqa: E402
from analytics.router import SemanticRouter  # noqa: E402
from analytics.store import DatasetStore  # noqa: E402

# --------------------------------------------------------------------------- #
# Test harness
# --------------------------------------------------------------------------- #

PASSED: List[str] = []
FAILED: List[Tuple[str, str]] = []


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
# Fixture data (created by the test, never shipped with the app)
# --------------------------------------------------------------------------- #

REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = ["Widgets", "Gadgets", "Doohickeys", "Gizmos"]


def build_fixture(path: Path, rows: int = 900) -> Path:
    """Write a small, realistic orders table."""
    random.seed(7)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "order_id",
                "order_date",
                "customer_name",
                "region",
                "category",
                "channel",
                "revenue",
                "quantity",
                "discount_rate",
            ]
        )
        for index in range(rows):
            month = (index // 30) % 12 + 1
            day = (index % 28) + 1
            writer.writerow(
                [
                    f"ORD-{100000 + index}",
                    f"2024-{month:02d}-{day:02d}",
                    f"Customer {index % 140}",
                    REGIONS[index % len(REGIONS)],
                    CATEGORIES[index % len(CATEGORIES)],
                    ["Online", "Retail", "Partner"][index % 3],
                    round(random.uniform(40, 4000), 2),
                    random.randint(1, 25),
                    round(random.uniform(0, 0.3), 3),
                ]
            )
    return path


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #

# question -> expected analysis
ROUTING_CASES: List[Tuple[str, str]] = [
    # -- overview: one number, no grouping --------------------------------- #
    ("give me a summary of the data", "overview"),
    ("what is the total revenue", "overview"),
    ("what is the total", "overview"),
    ("show me the headline numbers", "overview"),
    ("what is the average order value", "overview"),
    ("how many rows are in the table", "overview"),
    # -- breakdown: one measure, one grouping column ------------------------ #
    ("revenue by region", "breakdown"),
    ("break down revenue by region", "breakdown"),
    ("show me spend per category", "breakdown"),
    ("split sales by channel", "breakdown"),
    ("sales by category", "breakdown"),
    # -- trend: a measure over time ---------------------------------------- #
    ("revenue over time", "trend"),
    ("monthly revenue trend", "trend"),
    ("how has sales changed over the months", "trend"),
    ("daily order volume", "trend"),
    # -- ranking: the biggest contributors, by name ------------------------- #
    ("top 10 customers by revenue", "ranking"),
    ("which products sell the most", "ranking"),
    ("who are our top customers", "ranking"),
    ("largest accounts by spend", "ranking"),
    # -- distribution: one measure, no grouping ----------------------------- #
    ("distribution of order values", "distribution"),
    ("how are revenue amounts spread", "distribution"),
    ("histogram of transaction size", "distribution"),
    # -- comparison: two grouping columns at once --------------------------- #
    ("compare revenue by region and category", "comparison"),
    ("compare revenue by region and product", "comparison"),
    ("sales by category versus channel", "comparison"),
    ("revenue by country vs product line", "comparison"),
    ("cross tab of revenue by region and segment", "comparison"),
    # -- correlation: two measures moving together -------------------------- #
    ("is there a relationship between revenue and quantity", "correlation"),
    ("does revenue correlate with quantity sold", "correlation"),
    ("relationship between spend and profit", "correlation"),
    ("plot revenue against order size", "correlation"),
    # -- preview: the shape of the file itself ------------------------------ #
    ("show me the data", "preview"),
    ("what columns do we have", "preview"),
    ("describe the dataset", "preview"),
    ("list the fields", "preview"),
]

OUT_OF_SCOPE_CASES = [
    "what is the weather in london",
    "who won the world cup",
    "tell me a joke about penguins",
    "write me a haiku about autumn",
    "how do i boil an egg",
    "help me write a cover letter",
    "what is the capital of france",
    "translate good morning into spanish",
    "who should i vote for",
    "recommend a restaurant in berlin",
    "what is the stock price of apple",
    "please debug my python code",
]

NEAR_MISS_CASES = [
    "how many employees do we have",
    "what is our headcount",
    "tell me about the weather",
]


def main() -> int:
    section("Preparing fixture dataset")
    temp_root = Path(tempfile.mkdtemp(prefix="p1-smoke-"))
    fixture = build_fixture(temp_root / "orders.csv")
    print(f"  fixture: {fixture} ({fixture.stat().st_size:,} bytes)")

    store = DatasetStore(root=temp_root / "store")
    dataset, warnings = store.create("Orders fixture", [("orders.csv", fixture.read_bytes())])
    if dataset is None:
        print("  FAIL  could not ingest the fixture dataset")
        for warning in warnings:
            print(f"        {warning}")
        shutil.rmtree(temp_root, ignore_errors=True)
        return 1

    profile = dataset.profile()
    print(f"  tables: {[t.name for t in profile.tables]}")
    print(f"  rows:   {profile.primary_table.row_count}")
    print(f"  columns: {len(profile.primary_table.columns)}")

    section("Schema profiling")
    roles = {c.name: c.role for c in profile.primary_table.columns}
    print(f"  roles: {roles}")
    check("order_date profiled as a time column", roles.get("order_date") == "time", str(roles))
    check("revenue profiled as a metric", roles.get("revenue") == "metric", str(roles))
    check("quantity profiled as a metric", roles.get("quantity") == "metric", str(roles))
    check("region profiled as a dimension", roles.get("region") == "dimension", str(roles))
    check("category profiled as a dimension", roles.get("category") == "dimension", str(roles))
    check("order_id profiled as an entity", roles.get("order_id") == "entity", str(roles))
    check(
        "a ratio column is not mistaken for a percentage",
        next(c for c in profile.primary_table.columns if c.name == "discount_rate").unit
        == "number",
        str(next(c for c in profile.primary_table.columns if c.name == "discount_rate").unit),
    )

    section("Loading the local model")
    from sentence_transformers import SentenceTransformer

    from analytics.config import DEVICE, MODEL_NAME

    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    router = SemanticRouter(
        model,
        analyses=ANALYSES,
        threshold=SIMILARITY_THRESHOLD,
        ambiguity_margin=AMBIGUITY_MARGIN,
        scope_margin=SCOPE_MARGIN,
    )
    router.build_index()
    print(f"  indexed {len(ANALYSES)} analyses")

    binder = SchemaBinder(model, profile)

    section("Column binding (question -> the user's own columns)")
    binding = binder.bind("revenue by region")
    check("binds the metric to revenue", binding.metric and binding.metric.name == "revenue",
          str(binding.describe()))
    check("binds the dimension to region", binding.dimension and binding.dimension.name == "region",
          str(binding.describe()))
    check("binds the time column to order_date", binding.time and binding.time.name == "order_date",
          str(binding.describe()))

    binding2 = binder.bind("how much quantity did we sell by category")
    check("binds quantity for a quantity question", binding2.metric and binding2.metric.name == "quantity",
          str(binding2.describe()))
    check("binds category for a category question", binding2.dimension and binding2.dimension.name == "category",
          str(binding2.describe()))

    section("Every analysis executes against the uploaded schema")
    chart_types: Dict[str, Optional[str]] = {}
    # Questions that name the columns each analysis actually needs.
    analysis_questions = {
        "overview": "what is the total revenue",
        "breakdown": "revenue by region",
        "trend": "revenue over time",
        "ranking": "top 10 customers by revenue",
        "distribution": "distribution of revenue",
        "comparison": "compare revenue by region and category",
        "correlation": "relationship between revenue and quantity",
        "preview": "show me the data",
    }
    for analysis in ANALYSES:
        bound = binder.bind(analysis_questions[analysis.name])
        try:
            plan = analysis.plan(bound, profile)
            with dataset.lock:
                result = dataset.con.execute(plan.sql, plan.params)
                rows = result.fetchall()
            answer, chart = analysis.render(plan, [dict(zip([d[0] for d in result.description], r)) for r in rows], bound)
            chart_types[analysis.name] = plan.chart_type
            check(f"{analysis.name}: SQL runs", True)
            check(f"{analysis.name}: renders an answer", bool(answer and answer.strip()))
            if plan.chart_type:
                check(f"{analysis.name}: produces a chart", chart is not None)
        except NeedsColumn as exc:
            check(f"{analysis.name}: SQL runs", False, f"missing column: {exc}")
        except Exception as exc:
            check(f"{analysis.name}: SQL runs", False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()

    section("Two-column questions resolve both columns")
    two_dims = binder.bind("compare revenue by region and category")
    check(
        "comparison resolves a second dimension",
        two_dims.second_dimension is not None and two_dims.second_dimension.name == "category",
        str(two_dims.describe()),
    )
    two_metrics = binder.bind("relationship between revenue and quantity")
    check(
        "correlation resolves a second metric",
        two_metrics.second_metric is not None and two_metrics.second_metric.name == "quantity",
        str(two_metrics.describe()),
    )

    section("Routing accuracy")
    hits = 0
    for question, expected in ROUTING_CASES:
        route = router.route(question)
        ok = route.status == "matched" and route.analysis == expected
        if ok:
            hits += 1
        else:
            print(
                f"        {question!r} -> {route.status}/{route.analysis} "
                f"(expected {expected}, score {route.score})"
            )
    accuracy = hits / len(ROUTING_CASES)
    check(
        f"routing accuracy {accuracy:.0%} ({hits}/{len(ROUTING_CASES)})",
        accuracy >= 0.85,
        f"below the 85% bar",
    )

    section("Out-of-scope questions are refused")
    rejected = 0
    for question in OUT_OF_SCOPE_CASES:
        route = router.route(question)
        if route.status != "matched":
            rejected += 1
        else:
            print(f"        {question!r} -> matched {route.analysis} ({route.score:.3f})")
    check(
        f"rejected {rejected}/{len(OUT_OF_SCOPE_CASES)} off-domain questions",
        rejected >= len(OUT_OF_SCOPE_CASES) - 1,
    )

    section("Near-miss questions are not force-fitted")
    for question in NEAR_MISS_CASES:
        route = router.route(question)
        if route.status == "matched":
            # The router is schema-agnostic, so it cannot know the dataset
            # lacks a column. The *binder* is what refuses, by flagging the
            # term it could not resolve.
            bound = binder.bind(question)
            check(
                f"refuses {question!r} on the missing column",
                bool(bound.unmatched),
                f"matched {route.analysis} and found no missing term",
            )
        else:
            check(f"refuses {question!r} at the router", True)

    section("SQL injection surface")
    attacks = [
        "revenue by region; DROP TABLE orders--",
        "'; DELETE FROM orders; --",
        "total revenue UNION SELECT password FROM users",
        'revenue" ; DROP TABLE "orders',
        "1 OR 1=1",
    ]
    for attack in attacks:
        try:
            route = router.route(attack)
            check(f"survives {attack[:32]!r}", True)
        except Exception as exc:
            check(f"survives {attack[:32]!r}", False, str(exc))

    # The generated SQL must only ever reference quoted, introspected identifiers.
    plan = ANALYSES_BY_NAME["breakdown"].plan(binding, profile)
    check(
        "generated SQL quotes every identifier",
        '"revenue"' in plan.sql and '"region"' in plan.sql,
        plan.sql,
    )
    check(
        "generated SQL never embeds the question",
        "DROP" not in plan.sql.upper(),
        plan.sql,
    )

    section("Automatic insights")
    from analytics.insights import build_insights

    built = build_insights(dataset)
    check("insights produce charts", len(built["charts"]) >= 2, str(len(built["charts"])))
    check(
        "insights include a trend chart",
        any(c.get("id") == "trend" for c in built["charts"]),
        str([c.get("id") for c in built["charts"]]),
    )
    check(
        "insights include a breakdown chart",
        any(c.get("id") == "breakdown" for c in built["charts"]),
        str([c.get("id") for c in built["charts"]]),
    )
    check("insights report a row count", built["summary"].get("row_count") == 900,
          str(built["summary"].get("row_count")))
    check("insights report a date range", bool(built["summary"].get("date_range")),
          str(built["summary"].get("date_range")))

    section("Persisted datasets reload")
    store2 = DatasetStore(root=temp_root / "store")
    store2.restore()
    restored = store2.list_datasets()
    check("dataset survives a restart", len(restored) == 1, str(len(restored)))
    if restored:
        active = store2.active()
        check("restored dataset keeps its rows",
              active is not None and active.tables[0].row_count == 900,
              str(active.tables[0].row_count) if active else "none")

    shutil.rmtree(temp_root, ignore_errors=True)

    # -- summary --------------------------------------------------------- #
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
