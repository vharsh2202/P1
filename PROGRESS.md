# P1 — Build Status & Handoff

> Snapshot taken 2026-10-21, **after full verification**.
> Git: repo initialised, remote `origin` set, **0 commits** — nothing pushed yet.
> Live right now: API on `http://127.0.0.1:8000`, Vite on `http://localhost:5173`.

---

## 1. Objective

Build a **local, lightweight data-analytics web app** with no Docker and no
cloud LLM:

- **Frontend:** React (Vite, JavaScript), card-based chat UI, typing indicator,
  safe markdown rendering (tables + bullets).
- **Backend:** FastAPI on `:8000`, CORS fully open, React dev server on `:5173`.
- **Database:** DuckDB (in-memory) hydrated from static CSV files.
- **Routing:** no text-to-SQL LLM. A local CPU `sentence-transformers/all-MiniLM-L6-v2`
  embedder matches the question against a fixed `INTENT_DATABASE` of intents,
  each holding pre-written, parameter-free SQL plus a formatter.
- **Guard rails:** a similarity threshold for fallback and an ambiguity margin
  for tie-breaking.

Full design rationale and usage: see `README.md`.

---

## 2. Architecture decisions (already made — do not revisit)

| Decision | Choice | Why |
|---|---|---|
| Analytical engine | **DuckDB** (not SQLite) | Scans CSV directly; supports `LAG`, `MEDIAN`, `FILTER`, window functions — all used by the intent SQL. |
| Embedding model | `all-MiniLM-L6-v2`, CPU | ~90 MB, ~12 ms/question, no API key, no GPU needed. |
| SQL injection surface | **None by construction** | Every query is a constant string authored at build time; user text is never interpolated. |
| Routing storage | Pre-encoded phrase matrix | One matrix–vector product per request instead of per-phrase encoding. |
| Out-of-scope handling | **Negative class + vocabulary guard**, not just a threshold | A pure threshold provably cannot separate *"how many employees do we have"* (0.52, false positive) from *"give me the big picture numbers"* (0.53, true positive). See §5.1. |
| Backend layout | Single `main.py` | The whole decision surface is readable in one scroll, as requested. |
| Markdown safety | `react-markdown` + `remark-gfm`, **no** `rehype-raw` | Raw HTML is never parsed; `skipHtml` + default `urlTransform` add defence in depth. |
| Installed versions | duckdb 2.0.0, fastapi 0.156.8, sentence-transformers 6.2.0, transformers 5.20.2, torch 2.14.1+cpu, numpy 2.5.4, uvicorn 0.54.0, vite 6.4.5, React 18.3 | Resolved on Python 3.13.7 / Node 24.20.0. |

---

## 3. Completed work ✅

### 3.1 Repository

- [x] Cloned `https://github.com/vharsh2202/P1` into
      `C:\Users\harsh\OneDrive\Desktop\Deployed\P1` (repo was **completely empty**).
- [x] `origin` → `https://github.com/vharsh2202/P1.git`, branch `main`.
- [x] `.gitignore` written and verified — excludes `backend/.venv`,
      `backend/.cache`, `frontend/node_modules`, `frontend/dist`,
      `frontend/.verify-out.*`.
- [x] `gh` CLI is **not** installed; plain `git` is sufficient for push.

### 3.2 Backend (`backend/`)

- [x] `requirements.txt` — fastapi, uvicorn[standard], duckdb, sentence-transformers, numpy.
- [x] `main.py` — complete FastAPI app:
  - [x] `HF_HOME` redirected to `backend/.cache/huggingface` **before** importing
        sentence-transformers → project-local, offline-capable model cache.
  - [x] `Column` / `Intent` dataclasses; `INTENT_DATABASE` with **11 intents**.
  - [x] Every intent: 6–8 paraphrases, parameter-free SQL, column spec,
        formatter, follow-up suggestions.
  - [x] Formatters: `kpi` (bullets), `table` (markdown table + Total row),
        `trend` (narrative + monthly table).
  - [x] `Warehouse` — DuckDB in-memory, `read_csv_auto`, thread-safe via lock,
        schema introspection, date-range detection.
  - [x] `SemanticIntentRouter` — normalised embedding matrix, cosine
        max-per-intent scoring, threshold fallback, ambiguity margin,
        **negative class**, **off-domain vocabulary guard**, result cache.
  - [x] Endpoints: `GET /`, `/api/health`, `/api/meta`, `/api/schema`, `POST /api/chat`.
  - [x] Env-var config: `P1_INTENT_THRESHOLD`, `P1_INTENT_AMBIGUITY_MARGIN`,
        `P1_SCOPE_MARGIN`, `P1_RESULT_ROW_CAP`, `P1_CORS_ORIGINS`,
        `P1_EMBEDDING_DEVICE`, `P1_DATA_DIR`, `P1_EMBEDDING_MODEL`.
  - [x] CORS: explicit origins **plus** `allow_origin_regex` for any localhost port.
- [x] `data/generate_data.py` — deterministic (seed=42) dataset generator.
- [x] `data/sales.csv` — **1,500 orders**, 2024-01-01 → 2026-09-30, gross revenue
      $613,708.25, gross profit $197,905.28.
- [x] `data/customers.csv` — **140 customers**, segment/region/tier dimensions.
- [x] `smoke_test.py` — SQL-execution + routing-accuracy + rejection + near-miss suite.

### 3.3 Frontend (`frontend/`)

- [x] `package.json` (incl. `verify:markdown` script), `package-lock.json`,
      `vite.config.js` (port 5173, `strictPort`), `index.html`, `.env.example`.
- [x] `src/main.jsx`, `src/api.js` (`VITE_API_BASE` aware, 60 s timeout).
- [x] `src/App.jsx` — complete chat UI:
  - [x] Safe markdown renderer (custom `table`/`thead`/`tbody`/`tr`/`th`/`td`,
        `skipHtml`, no `rehype-raw`, `target="_blank" rel="noopener noreferrer"`).
  - [x] `TypingIndicator` with staggered bouncing dots.
  - [x] Message cards, intent badge, confidence %, row count, collapsible SQL panel,
        follow-up chips.
  - [x] Status pill (connecting / online / offline) with 30 s health polling.
  - [x] `Enter` to send, `Shift+Enter` for newline; graceful offline error card.
- [x] `src/index.css` — dark theme, sticky table headers, zebra striping,
      table horizontal scroll, 720 px responsive breakpoint.
- [x] `src/markdown.verify.jsx` — server-side render + XSS-safety assertions.

### 3.4 Verification performed ✅

**Backend**

- [x] All **11** intent SQL statements execute against DuckDB 2.0.0 and return
      the expected columns.
- [x] Routing accuracy **22/22 = 100%** on held-out paraphrases.
- [x] All **8** out-of-scope questions rejected.
- [x] All **3** near-miss questions still answered.
- [x] `smoke_test.py` exit code **0**, prints `RESULT: PASS`.
- [x] Live API on `:8000`:
  - [x] `GET /api/health` → `{"status":"ok","model":"sentence-transformers/all-MiniLM-L6-v2","intents":11,"threshold":0.42}`
  - [x] `POST /api/chat` *"revenue by region"* → `matched`, `revenue_by_region`,
        confidence 1.0, 4 rows, correct markdown table **with a Total row**.
  - [x] `POST /api/chat` *"are we profitable"* → `matched`, `profitability`,
        correct bullet list.
  - [x] `POST /api/chat` *"show me the monthly revenue trend"* → `matched`,
        `revenue_trend`, 33 rows, narrative + table.
  - [x] `POST /api/chat` *"how many employees do we have"* → `status=fallback`,
        **`sql` is null** (no SQL executed), lists all 11 supported scopes.
  - [x] `GET /api/meta` → correct model, thresholds, data range, row counts, intents.
- [x] CORS verified with `curl.exe`:
  - [x] Preflight `OPTIONS` → 200, `access-control-allow-origin: http://localhost:5173`,
        `allow-credentials: true`, all methods.
  - [x] Arbitrary localhost port (`http://localhost:9999`) allowed via regex.

**Frontend**

- [x] `npm install` → 166 packages, exit 0.
- [x] `npm run build` → 284 modules, no errors, `dist/` 9.66 kB CSS +
      311.35 kB JS (97.59 kB gzip), 12.40 s.
- [x] `npm run verify:markdown` → **14/14 assertions PASS**, exit 0:
      - [x] GFM table → real `<table>/<th>/<td>`, values preserved, `.table-scroll` wrapper.
      - [x] Bullet list → `<ul>/<li>/<strong>`.
      - [x] `<script>` / `<img onerror>` → **completely stripped** (output is empty).
      - [x] `javascript:` href → **stripped** (`href=""`).
      - [x] Safe link kept, with `target="_blank" rel="noopener noreferrer"`.
      - [x] Inline code rendered.
- [x] Vite dev server serving on `:5173` → HTTP 200.
- [x] `git check-ignore` confirmed venv / `node_modules` / `dist` / `.verify-out.*` excluded.

> **Note on browser-based UI verification:** the sandboxed desktop browser in this
> environment could not navigate to `localhost:5173` (it stayed on `about:blank`),
> so a visual screenshot was not captured. The UI was instead verified by
> (a) a clean production build, (b) server-side rendering assertions on the
> actual `Markdown` component, and (c) live API calls against the running server.
> **The browser-based checklist in §5.3 is still worth doing manually.**

---

## 4. In-flight ⏳

Nothing is running in the background. Both servers are currently **up**:
- API: `http://127.0.0.1:8000` (uvicorn, background shell `sh_15eb4a523001cDDU4ADjHGRPr0`)
- Vite: `http://localhost:5173` (background shell `sh_15ebab183001nm0kr2C54Ajp1u`)

The embedding model is cached in `backend/.cache/huggingface` — **no further
downloads needed**.

---

## 5. Remaining work ⬜

### 5.1 (Optional) Extend the intent catalogue

The router is data-driven; adding a capability is a single `Intent` entry in
`backend/main.py` — no other file changes. See **"Adding a new capability"** in
`README.md` for the template and the rules of thumb.

### 5.2 (Optional) Polish

- [ ] Add `backend/.env.example` documenting the `P1_*` env vars.
- [ ] Add `backend/data/README.md` describing the schema for contributors.
- [ ] Decide whether `PROGRESS.md` belongs in the public repo — delete it
      before the final push if not.
- [ ] Consider lowering `P1_INTENT_THRESHOLD` if real-world questions are
      being rejected; re-run `smoke_test.py` after any change.

### 5.3 Manual browser checklist

The one thing that could not be verified automatically:

- [ ] Open `http://localhost:5173` — header shows a green **API online** pill.
- [ ] Header shows `data 2024-01-01 → 2026-09-30` and `1,500 orders`.
- [ ] Typing indicator appears while a request is in flight.
- [ ] *"revenue by region"* renders a styled table with a **Total** row.
- [ ] *"are we profitable?"* renders bullets.
- [ ] *"how many employees do we have"* renders the fallback card, **no** table.
- [ ] Follow-up chips populate the composer.
- [ ] **SQL executed** disclosure expands and shows the trusted SQL.

### 5.4 Commit and push

```powershell
cd C:\Users\harsh\OneDrive\Desktop\Deployed\P1
git add -A
git status                      # sanity-check the staged file list
git commit -m "P1: local semantic data analytics app (React + FastAPI + DuckDB)"
git push -u origin main
```

Expected to be committed (18 files):

```
.gitignore
PROGRESS.md
README.md
backend/main.py
backend/requirements.txt
backend/smoke_test.py
backend/data/generate_data.py
backend/data/sales.csv
backend/data/customers.csv
frontend/package.json
frontend/package-lock.json
frontend/vite.config.js
frontend/index.html
frontend/.env.example
frontend/src/main.jsx
frontend/src/App.jsx
frontend/src/api.js
frontend/src/index.css
frontend/src/markdown.verify.jsx
```

**Must NOT be committed:** `backend/.venv/`, `backend/.cache/`, `frontend/node_modules/`,
`frontend/dist/`, `frontend/.verify-out.*`.

---

## 6. Environment notes

| Item | Value |
|---|---|
| Working dir | `C:\Users\harsh\OneDrive\Desktop\Deployed\P1` |
| Git | `git 2.56.0.windows.2` — remote `origin` set, **0 commits** |
| Python | 3.13.7 — venv at `backend/.venv` |
| Node | **v24.20.0** at `C:\Program Files\nodejs` — installed but **not on PATH** in some shells |
| npm | 11.19.0 (same PATH caveat) |
| `gh` CLI | **not installed** — use plain `git` |
| OneDrive | the repo sits inside OneDrive; large installs (`.venv`, `node_modules`) sync slowly — this is why `pip install` took ~20 min |

**PATH fix if `node`/`npm` are not found:**

```powershell
$env:Path = "C:\Program Files\nodejs;$env:Path"
```

**Restart the servers from scratch:**

```powershell
# backend
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# frontend
cd ..\frontend
npm run dev
```

---

## 7. Known issues / gotchas

1. **PyTorch CUDA build.** Always install the CPU wheel first:
   `pip install torch --index-url https://download.pytorch.org/whl/cpu`.
   On Windows the default PyPI wheel pulls CUDA and balloons to ~2.5 GB.
2. **Startup takes ~50 s.** The model weights are cached, but importing
   `sentence_transformers` + `transformers` and loading weights through
   OneDrive-backed storage is slow. It is a one-off per start, not per request.
3. **SSL flakiness** against `download.pytorch.org` was seen once
   (`SSLEOFError`) and resolved on pip's automatic retry.
4. **npm 11 script gate** warns that `esbuild`'s postinstall was not run. The
   binary is present and the build succeeds; `npm install-scripts approve esbuild`
   silences it.
5. **DuckDB CSV paths on Windows** — `main.py` uses `path.resolve().as_posix()`
   so backslashes never reach the SQL string literal.
6. **DuckDB connections are not thread-safe** — every query goes through the
   `Warehouse` lock and runs via `run_in_threadpool`.
7. **DuckDB may return `Decimal`** for some aggregates, so the total-row
   computation coerces with `float()` rather than `isinstance` checks.
8. **The model is downloaded on first run** and cached in `backend/.cache/`
   (gitignored). Delete that folder to force a re-download.
9. **`verify:markdown` bundles to CJS**, so esbuild warns that `import.meta` is
   unavailable. That is harmless — `api.js` reads `import.meta.env?.VITE_API_BASE`
   with optional chaining and falls back to the default URL.
