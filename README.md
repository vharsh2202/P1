# P1 · Local Data Analytics

A local, lightweight analytics web app. **No Docker. No cloud LLM. No API keys.**

A natural-language question is embedded by a small local sentence-transformer,
matched against a fixed catalogue of analytical intents, and answered by a
pre-written SQL query running against a local DuckDB warehouse.

```
┌──────────────────────┐         ┌────────────────────────────────────────┐
│  React + Vite        │  HTTP   │  FastAPI  (:8000)                      │
│  :5173               │ ──────► │                                        │
│  chat UI + markdown  │  JSON   │  ┌──────────────────────────────────┐  │
└──────────────────────┘         │  │ SemanticIntentRouter            │  │
                                 │  │  all-MiniLM-L6-v2 · CPU · local │  │
                                 │  │  cosine match → INTENT_DATABASE  │  │
                                 │  └──────────────────────────────────┘  │
                                 │                  │                    │
                                 │                  ▼                    │
                                 │  ┌──────────────────────────────────┐  │
                                 │  │ DuckDB (in-memory)               │  │
                                 │  │  ← data/sales.csv                │  │
                                 │  │  ← data/customers.csv            │  │
                                 │  └──────────────────────────────────┘  │
                                 └────────────────────────────────────────┘
```

**Why this design:** the expensive, error-prone part of text-to-SQL is *deciding
what query to run*. This app removes that problem instead of solving it — every
capability is a hand-written query with a known-good shape. The model only has
to answer a much easier question: *which of these N known analyses is the user
asking for?* That is a classification task a 22M-parameter model handles well on
CPU in ~15ms, with no hallucination risk and no token cost.

---

## Phase 0 · Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ (3.13 verified) | `python --version` |
| Node.js | 18+ (24.20 verified) | includes npm |
| Disk | ~3 GB free | mostly the CPU-only PyTorch wheel (~250 MB) + model cache (~90 MB) |
| RAM | 4 GB+ | ~400 MB resident with the model loaded |

No GitHub CLI is required — plain `git` is enough.

---

## Phase 1 · Backend

```powershell
cd backend

# 1. Create the virtual environment
python -m venv .venv

# 2. Activate it (PowerShell)
.\.venv\Scripts\Activate.ps1
#    cmd.exe:      .venv\Scripts\activate.bat
#    Git Bash:     source .venv/Scripts/activate
#    If PowerShell blocks the script:
#    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 3. Install CPU-only PyTorch FIRST (avoids the ~2.5 GB CUDA build on Windows)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 4. Install the rest
pip install -r requirements.txt

# 5. Generate the sample analytical dataset
python data\generate_data.py

# 6. Run the API
uvicorn main:app --reload --port 8000
```

The first start downloads `all-MiniLM-L6-v2` (~90 MB) into `backend/.cache/huggingface`.
Subsequent starts are instant and fully offline.

**Verify it:**

```powershell
curl http://localhost:8000/api/health
# {"status":"ok","model":"sentence-transformers/all-MiniLM-L6-v2","intents":11,"threshold":0.42}

curl http://localhost:8000/docs
```

---

## Phase 2 · Frontend

```powershell
cd frontend
npm install
npm run dev
```

Vite serves on **http://localhost:5173** (strict port). CORS is fully open to
`localhost:*` on the backend, so the two talk directly.

**Verify it:** open http://localhost:5173 — the header should show a green
**API online** pill.

---

## Phase 3 · Run it

Open http://localhost:5173 and ask:

| Question | Intent matched |
|---|---|
| What is our total revenue? | `total_revenue` |
| Show me the monthly revenue trend | `revenue_trend` |
| Revenue by region | `revenue_by_region` |
| What are our top selling products? | `top_products` |
| Which sales channel performs best? | `channel_performance` |
| Are we profitable? | `profitability` |
| Who are our top customers? | `top_customers` |
| How many orders were returned? | `returns_analysis` |

Ask something unsupported — *"how many employees work here?"* — and you get the
explicit fallback listing every supported scope. **The app never runs SQL for a
question it could not map to a known intent.**

---

## Project layout

```
P1/
├── .gitignore
├── README.md
├── PROGRESS.md                      # build status / handoff notes
│
├── backend/
│   ├── .venv/                       # virtualenv (gitignored)
│   ├── .cache/huggingface/          # model cache (gitignored, ~90 MB)
│   ├── requirements.txt
│   ├── main.py                      # ★ API + router + intents + SQL + formatting
│   ├── smoke_test.py                # ★ SQL execution + routing accuracy suite
│   └── data/
│       ├── generate_data.py         # deterministic dataset generator
│       ├── sales.csv                # fact table   (1,500 orders)
│       └── customers.csv            # dimension    (140 customers)
│
└── frontend/
    ├── package.json
    ├── package-lock.json
    ├── vite.config.js
    ├── index.html
    ├── .env.example
    └── src/
        ├── main.jsx                 # React entry point
        ├── App.jsx                  # ★ chat UI, markdown renderer, typing indicator
        ├── api.js                   # fetch client (VITE_API_BASE aware)
        ├── index.css                # theme + component styles
        └── markdown.verify.jsx      # ★ server-side render + XSS-safety suite
```

`main.py` is deliberately self-contained: the intent catalogue, the SQL, the
router and the formatters all live in one file so the whole decision surface is
readable in a single scroll.

---

## Data schema

**`sales`** — one row per order

| Column | Type | Notes |
|---|---|---|
| `order_id` | VARCHAR | primary key |
| `order_date` | DATE | 2024-01-01 → 2026-09-30 |
| `customer_id` | VARCHAR | → `customers.customer_id` |
| `region`, `city` | VARCHAR | denormalised for fast grouping |
| `channel` | VARCHAR | Web / Mobile App / Marketplace / Retail Store / Partner Reseller |
| `category`, `product` | VARCHAR | 5 categories, 22 products |
| `units` | INTEGER | quantity |
| `unit_price` | DECIMAL | list price after variance |
| `discount` | DECIMAL | 0–0.28, channel dependent |
| `revenue` | DECIMAL | **negative for returned orders** |
| `cost`, `profit` | DECIMAL | `profit = revenue − cost` |
| `is_returned` | BOOLEAN | ~5.5% of orders |

**`customers`** — one row per customer: `customer_id`, `customer_name`,
`segment` (Enterprise/SMB/Consumer), `region`, `country`, `city`, `signup_date`,
`loyalty_tier` (Bronze/Silver/Gold/Platinum).

Swap these two CSVs for your own and the same intents keep working — as long as
the column names match. Regenerate at any time with
`python data\generate_data.py` (seeded, so it is reproducible).

---

## How the router works

1. **Index (once at startup).** Every training phrase in `INTENT_DATABASE` is
   encoded with `all-MiniLM-L6-v2` using `normalize_embeddings=True`, giving a
   unit-norm matrix. Embedding lookup is a single matrix–vector product.
2. **Route.** The question is embedded the same way; the cosine similarity to
   every training phrase is computed. Each intent keeps its **best** phrase
   score; the highest-scoring intent wins.
3. **Format.** The result set is rendered as a markdown table or bullet list by
   the intent's formatter.

User text is **never** interpolated into SQL. Every query is a constant string
authored at build time, so there is no injection surface at all — not a
parameterised escape hatch, simply none.

### The three guard layers

A single similarity threshold is **not** sufficient, and the test suite proves
it. Measured against the held-out set:

| Question | Best intent score | Correct? |
|---|---|---|
| *"what did we make in total"* | 0.76 | yes |
| *"give me the big picture numbers"* | 0.53 | yes |
| **"how many employees do we have"** | **0.52** | **no — matched a revenue query** |
| *"what is the weather in mumbai"* | 0.09 | yes (rejected) |

The false positive (0.52) sits *inside* the legitimate range (0.53–0.90). **No
threshold can separate them**, because "how many employees do we have" and
"how much revenue did we make" are structurally the same sentence. So the
router uses three layers instead of one:

1. **Vocabulary guard** — a question naming a term the warehouse has never
   heard of (`employee`, `headcount`, `salary`, `weather`, …) is refused before
   it is even embedded. Cheap, and exactly precise.
2. **Negative class** — `OUT_OF_SCOPE_PHRASES` is encoded into the *same index*
   as the intents but is not an intent. If the closest out-of-scope phrase
   scores within `0.03` of the closest intent, the question is rejected. This
   is what catches *"how many offices do we operate"* (0.55 → rejected).
3. **Similarity floor** — below `0.42` the question is rejected outright.

The negative class is also why *"how many orders do we have"* still routes
correctly to `total_revenue` (0.94): the discriminating token is the **noun**,
and the model handles that well.

### Tuning

Both guards are environment variables:

```powershell
$env:P1_INTENT_THRESHOLD = "0.42"        # lower = broader coverage
$env:P1_INTENT_AMBIGUITY_MARGIN = "0.05"  # higher = fewer ambiguous prompts
$env:P1_SCOPE_MARGIN = "0.03"             # higher = stricter out-of-scope rejection
$env:P1_RESULT_ROW_CAP = "500"            # max rows returned to the client
$env:P1_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
$env:P1_EMBEDDING_DEVICE = "cpu"
```

If valid questions are being rejected, lower the threshold toward `0.35`. If
wrong questions are being accepted, raise it toward `0.55`. A misrouted intent
is almost always a *training-phrase coverage* problem, not a threshold problem —
the fix is to add a paraphrase that uses the same noun the user used.

---

## Adding a new capability

No other file needs to change — the router, the API and the UI pick up the new
intent automatically.

```python
# backend/main.py -> INTENT_DATABASE
Intent(
    name="discount_impact",
    description="How much revenue discounts cost us.",
    examples=(
        "how much are discounts costing us",
        "impact of discounts on revenue",
        "which products lose money to discounts",
    ),
    sql="""
        SELECT
            channel,
            ROUND(AVG(discount) * 100, 1) AS avg_discount_pct,
            ROUND(SUM(revenue), 2) AS revenue
        FROM sales
        WHERE is_returned = FALSE
        GROUP BY channel
        ORDER BY avg_discount_pct DESC
    """,
    columns=(
        Column("channel", "Channel", "text"),
        Column("avg_discount_pct", "Avg discount %", "pct"),
        Column("revenue", "Revenue", "money"),
    ),
    formatter="table",
),
```

Rules of thumb:

- Write **3–5 genuine paraphrases**, not keywords. "revenue by region" and
  "which region is our biggest market" embed far apart; keyword lists do not.
- SQL must be **parameter-free** and reference only the real schema.
- `formatter` is `kpi` (single row → bullets), `table` (multi-row → table) or
  `trend` (time series → narrative + table).
- `total_row=True` appends a computed total for full breakdowns. Leave it off
  for `LIMIT`ed queries like top-N, where a total would be misleading.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness + model name + threshold |
| `GET` | `/api/meta` | intents, schema summary, data range, tuning values |
| `GET` | `/api/schema` | tables and column types |
| `POST` | `/api/chat` | `{ "question": "..." }` → answer, SQL, rows, confidence |
| `GET` | `/docs` | interactive OpenAPI UI |

`/api/chat` response:

```json
{
  "status": "matched",
  "answer": "- **Total revenue:** $613,708.25\n...",
  "intent": "total_revenue",
  "confidence": 0.7342,
  "sql": "SELECT ...",
  "columns": ["total_revenue", "total_profit"],
  "rows": [{ "total_revenue": 613708.25 }],
  "row_count": 1,
  "suggestions": ["Break revenue down by region"],
  "candidates": [{ "intent": "total_revenue", "score": 0.7342 }]
}
```

`status` is one of `matched` · `fallback` · `ambiguous` · `error`.

---

## Markdown safety

The client renders model-produced markdown with `react-markdown` + `remark-gfm`:

- raw HTML is **never** parsed — `rehype-raw` is not installed, and `skipHtml`
  removes HTML nodes outright;
- `urlTransform` uses react-markdown's default allow-list, which strips
  `javascript:` and `data:` URLs from links and images;
- GFM tables are parsed by `remark-gfm`, which cannot emit script or event
  handlers.

Tables additionally get a sticky header, zebra striping and horizontal scroll
on narrow viewports.

---

## Verification

Two suites ship with the project. Run both before you trust a change.

### Backend — execution + routing accuracy

```powershell
cd backend
.\.venv\Scripts\python.exe smoke_test.py
```

Checks that **every** intent's SQL executes against the warehouse and returns
the expected columns, then routes a **held-out** set of paraphrases — phrases
that deliberately do not appear in the training data — and reports accuracy.
It also asserts that out-of-scope questions are rejected and that near-misses
(*"how many orders do we have"*) are still answered.

Current result:

```
accuracy: 22/22 = 100%
out-of-scope: 8/8 rejected
near-miss:    3/3 answered
RESULT: PASS
```

### Frontend — markdown rendering + XSS safety

```powershell
cd frontend
npm run verify:markdown
```

Renders the real `Markdown` component server-side with `react-dom/server` and
asserts on the produced HTML: GFM tables become real `<table>` markup, bullet
lists become `<ul>/<li>`, raw HTML in the payload never becomes live DOM, and
`javascript:` URLs are stripped from links.

```
RESULT: PASS   (14/14 assertions)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `API offline` in the header | backend not running | start uvicorn on :8000 |
| First request is slow | model downloading | wait for the first `pip install`/start to finish; it is cached afterwards |
| `ModuleNotFoundError: duckdb` | wrong interpreter | confirm the venv is active: `python -c "import sys; print(sys.executable)"` |
| pip downloads a 2.5 GB torch | CUDA build pulled in | install the CPU wheel first (Phase 1, step 3) |
| Every question falls back | threshold too high | lower `P1_INTENT_THRESHOLD` to `0.35` |
| Two intents tie | genuinely ambiguous question | lower `P1_INTENT_AMBIGUITY_MARGIN` to `0.03` |
| Port already in use | stale process | `Get-NetTCPConnection -LocalPort 8000 \| Select OwningProcess` then `Stop-Process -Id <pid>` |
| `npm` not recognised | Node not on PATH | `& "C:\Program Files\nodejs\npm.cmd" install` |
| npm warns about blocked install scripts | npm 11+ gates lifecycle scripts | `npm install-scripts approve esbuild` |

---

## Performance

| Operation | Time |
|---|---|
| Model load (cached) | ~1.5 s |
| Question embedding | ~12 ms |
| Intent match | <1 ms (matrix–vector product) |
| DuckDB query | ~5–40 ms |
| **End-to-end** | **~60 ms** |

---

## Deploying this repo

```powershell
git add -A
git commit -m "P1: local semantic data analytics app (React + FastAPI + DuckDB)"
git push -u origin main
```

`.gitignore` excludes `.venv/`, `node_modules/`, `dist/` and the model cache, so
the repository stays small.
