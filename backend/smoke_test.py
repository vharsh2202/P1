"""
Smoke test for P1.

Two things are verified:

1. EXECUTION - every intent's SQL runs against the DuckDB warehouse and
   returns a well-formed result set.
2. ROUTING   - a held-out evaluation set of paraphrases (none of which appear
   in the training data) is routed to the expected intent, and the overall
   accuracy is reported along with any misses.

Run it with the virtualenv active:

    python smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from main import (  # noqa: E402
    AMBIGUITY_MARGIN,
    DATA_DIR,
    INTENT_DATABASE,
    MODEL_NAME,
    OFF_DOMAIN_TERMS,
    OUT_OF_SCOPE_PHRASES,
    SCOPE_MARGIN,
    SIMILARITY_THRESHOLD,
    SemanticIntentRouter,
    Warehouse,
)

# Held-out paraphrases: deliberately NOT the phrases used for training.
EVAL_SET = [
    ("what did we make in total", "total_revenue"),
    ("give me the big picture numbers", "total_revenue"),
    ("how has sales moved month by month", "revenue_trend"),
    ("plot our monthly numbers over time", "revenue_trend"),
    ("which geographic market is the biggest", "revenue_by_region"),
    ("split our sales by geography", "revenue_by_region"),
    ("which product lines earn the most", "revenue_by_category"),
    ("compare our different product categories", "revenue_by_category"),
    ("what is selling the best for us", "top_products"),
    ("show me our best performing items", "top_products"),
    ("who spends the most with us", "top_customers"),
    ("our biggest spenders please", "top_customers"),
    ("how do enterprise versus consumers compare", "customer_segments"),
    ("break down revenue by customer type", "customer_segments"),
    ("is the web store outperforming the marketplace", "channel_performance"),
    ("compare our different sales channels", "channel_performance"),
    ("are we actually making money", "profitability"),
    ("what are our margins looking like", "profitability"),
    ("what does a typical basket look like", "average_order_value"),
    ("how much do people spend per order on average", "average_order_value"),
    ("how many orders came back", "returns_analysis"),
    ("what is our refund rate", "returns_analysis"),
]

# Questions that must NOT map to any intent.
OUT_OF_SCOPE = [
    "how many employees do we have",
    "what is the weather in mumbai tomorrow",
    "who won the world cup in 1998",
    "delete all the sales records",
    "what is our headcount",
    "how many offices do we operate",
    "who is the president of france",
    "what is the capital of japan",
]

# Near-miss questions: structurally similar to a valid intent ("how many X do
# we have") but about something outside the warehouse. These are the cases that
# defeat a pure similarity threshold.
NEAR_MISS = [
    "how many orders do we have",
    "how many products do we sell",
    "how many customers do we serve",
]


def check_execution(warehouse: Warehouse) -> bool:
    print("=" * 72)
    print("1. EXECUTION - every intent's SQL against the DuckDB warehouse")
    print("=" * 72)

    ok = True
    for intent in INTENT_DATABASE:
        try:
            columns, rows = warehouse.query(intent.sql)
            if not columns:
                print(f"  [FAIL] {intent.name}: query returned no columns")
                ok = False
                continue
            if intent.formatter != "kpi" and not rows:
                print(f"  [WARN] {intent.name}: zero rows (expected some data)")
                continue
            sample = rows[0] if rows else {}
            missing = [
                c.key for c in intent.columns if c.key not in columns
            ]
            if missing:
                print(f"  [FAIL] {intent.name}: columns missing from result: {missing}")
                ok = False
                continue
            print(
                f"  [ok]   {intent.name:<22} {len(rows):>4} row(s), "
                f"{len(columns)} col(s) | e.g. {list(sample.items())[:2]}"
            )
        except Exception as exc:
            print(f"  [FAIL] {intent.name}: {exc}")
            ok = False

    print()
    return ok


def check_routing(warehouse: Warehouse) -> bool:
    print("=" * 72)
    print(f"2. ROUTING - held-out paraphrases (threshold={SIMILARITY_THRESHOLD}, "
          f"margin={AMBIGUITY_MARGIN})")
    print("=" * 72)

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME, device="cpu")
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

    hits = 0
    misses = []
    for question, expected in EVAL_SET:
        result = router.route(question)
        got = result.intent.name if result.intent else f"<{result.status}>"
        ok = got == expected
        hits += ok
        flag = "ok  " if ok else "MISS"
        print(f"  [{flag}] {question:<48} -> {got:<22} ({result.score:.3f})")
        if not ok:
            misses.append((question, expected, got, result.score))

    accuracy = hits / len(EVAL_SET)
    print(f"\n  accuracy: {hits}/{len(EVAL_SET)} = {accuracy:.0%}")
    if misses:
        print("\n  misses:")
        for question, expected, got, score in misses:
            print(f"    - {question!r}: expected {expected}, got {got} ({score:.3f})")

    print("\n  out-of-scope questions (must fall back):")
    guards_ok = True
    for question in OUT_OF_SCOPE:
        result = router.route(question)
        fell_back = result.status in ("fallback", "ambiguous")
        guards_ok = guards_ok and fell_back
        print(f"    [{'ok  ' if fell_back else 'MISS'}] {question:<48} -> {result.status} ({result.score:.3f})")

    print("\n  near-miss questions (must still be answered, not rejected):")
    for question in NEAR_MISS:
        result = router.route(question)
        answered = result.status == "matched"
        guards_ok = guards_ok and answered
        got = result.intent.name if result.intent else f"<{result.status}>"
        print(f"    [{'ok  ' if answered else 'MISS'}] {question:<48} -> {got} ({result.score:.3f})")

    print()
    return accuracy >= 0.85 and guards_ok


def main() -> int:
    print(f"Data directory: {DATA_DIR}\n")

    warehouse = Warehouse(DATA_DIR)
    warehouse.connect()
    print(f"Loaded tables: {warehouse.row_counts}")
    print(f"Data range:    {warehouse.data_range}\n")

    try:
        execution_ok = check_execution(warehouse)
        routing_ok = check_routing(warehouse)
    finally:
        warehouse.close()

    print("=" * 72)
    if execution_ok and routing_ok:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL (execution_ok=%s routing_ok=%s)" % (execution_ok, routing_ok))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
