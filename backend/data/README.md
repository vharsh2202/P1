# Data

Static source files for the P1 warehouse. DuckDB loads these at startup with
`read_csv_auto`, so the column types below are *inferred* from the CSV content,
not declared.

## `sales.csv` — fact table (one row per order)

| Column | Inferred type | Notes |
|---|---|---|
| `order_id` | VARCHAR | primary key, `ORD-10001` … `ORD-11500` |
| `order_date` | DATE | 2024-01-01 → 2026-09-30 |
| `customer_id` | VARCHAR | → `customers.customer_id` |
| `region` | VARCHAR | North America / EMEA / APAC / LATAM |
| `city` | VARCHAR | denormalised from the customer for fast grouping |
| `channel` | VARCHAR | Web / Mobile App / Marketplace / Retail Store / Partner Reseller |
| `category` | VARCHAR | Electronics / Furniture / Apparel / Office Supplies / Home & Kitchen |
| `product` | VARCHAR | 22 products across the 5 categories |
| `units` | BIGINT | quantity on the order |
| `unit_price` | DOUBLE | list price, ±18% variance around the base price |
| `discount` | DOUBLE | 0–0.28, depends on channel (marketplaces discount most) |
| `revenue` | DOUBLE | `units × unit_price × (1 − discount)` |
| `cost` | DOUBLE | `units × unit_price × cost_ratio` |
| `profit` | DOUBLE | `revenue − cost` |
| `is_returned` | BOOLEAN | ~5.5% of orders |

**Sign convention:** returned orders carry **negative** `revenue`, `cost` and
`profit`. The intent SQL therefore filters with `WHERE is_returned = FALSE` to
report gross revenue, and the `returns_analysis` intent uses
`SUM(CASE WHEN is_returned THEN -revenue ELSE 0 END)` to report returned
value as a positive number.

## `customers.csv` — dimension table (one row per customer)

| Column | Inferred type | Notes |
|---|---|---|
| `customer_id` | VARCHAR | primary key, `CUST-0001` … `CUST-0140` |
| `customer_name` | VARCHAR | unique per row |
| `segment` | VARCHAR | Enterprise / SMB / Consumer |
| `region` | VARCHAR | North America / EMEA / APAC / LATAM |
| `country` | VARCHAR | |
| `city` | VARCHAR | |
| `signup_date` | DATE | 30–900 days before the first order |
| `loyalty_tier` | VARCHAR | Bronze / Silver / Gold / Platinum |

## Regenerating

```powershell
cd backend
python data\generate_data.py
```

The generator is seeded (`SEED = 42`), so the output is byte-for-byte
reproducible. It writes both CSVs into this directory.

Current figures: 1,500 orders, 140 customers, \$613,708.25 gross revenue,
\$197,905.28 gross profit.

## Using your own data

Replace the two CSVs, keeping the column names above, and the intent SQL keeps
working unchanged. If you change a column name, update the matching
`Intent.sql` in `backend/main.py` — those are the only places the schema is
referenced.

Alternatively point the app at a different directory:

```powershell
$env:P1_DATA_DIR = "C:\path\to\my\data"
```
