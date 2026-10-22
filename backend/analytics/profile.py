"""Schema introspection, semantic column roles, and question -> column binding.

The app answers questions about *whatever data the user uploaded*, so nothing
here can assume a fixed schema. Instead every table is profiled once at ingest
time and each column is assigned a **semantic role**:

``metric``     a numeric column that can be summed or averaged
``dimension``  a low-cardinality categorical column that can be grouped by
``time``       a date/timestamp column (or a period column such as a year)
``entity``     a high-cardinality label column (a customer, a product, a row id)

Binding a natural-language question to concrete columns is a two-signal job:

1. **Lexical** - token overlap between the question and the column name,
   expanded through a small synonym table ("sales" -> "revenue").
2. **Semantic** - cosine similarity between the question and a short natural
   language description of the column, using the same local MiniLM model that
   drives routing.

Neither signal is an LLM and neither can execute anything; they only *choose*
which already-introspected column to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROLE_METRIC = "metric"
ROLE_DIMENSION = "dimension"
ROLE_TIME = "time"
ROLE_ENTITY = "entity"

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL",
}
_DATE_TYPES = {"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ", "TIME"}
_TEXT_TYPES = {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "UUID"}

# --------------------------------------------------------------------------- #
# Vocabulary used for role inference
# --------------------------------------------------------------------------- #

# Words that mark a numeric column as a *quantity of money* rather than a count.
_MONEY_WORDS = {
    "revenue", "sales", "amount", "price", "cost", "profit", "spend", "spending",
    "budget", "value", "subtotal", "total", "discount", "tax", "tip", "fee",
    "charge", "invoice", "payment", "salary", "wage", "balance", "income",
    "earnings", "turnover", "expense", "expenses", "refund", "payout", "rent",
    "premium", "contribution",
    # Common business acronyms that mean money.
    "mrr", "arr", "acv", "ltv", "cac", "gmv", "aov", "roi", "cogs", "ebitda",
    "gp", "ar", "ap",
}
# Acronyms that should keep their capitals when a column is presented.
ACRONYMS = {
    "mrr", "arr", "acv", "ltv", "cac", "gmv", "aov", "roi", "cogs", "ebitda",
    "gp", "ar", "ap", "id", "sku", "url", "api", "kpi", "yoy", "mom", "qoq",
    "usd", "eur", "gbp", "inr", "jpy", "pct", "vat", "csv", "tsv", "pdf",
}
# Words that mark a numeric column as a *ratio*. These are formatted as plain
# numbers because the scale is unknown (0.12 could mean 12% or 0.12%).
_PERCENT_WORDS = {"rate", "ratio", "percent", "pct", "share", "margin", "discount_rate"}
# Only these are unambiguously percentages.
_EXPLICIT_PERCENT_WORDS = {"percent", "pct", "percentage", "percentile"}
# Words that mark a column as a time dimension.
_TIME_WORDS = {
    "date", "datetime", "timestamp", "time", "month", "year", "week", "day",
    "quarter", "period", "created", "updated", "ordered", "shipped", "paid",
    "due", "start", "end", "birth", "joined", "closed", "occurred",
}
# Words that mark a column as a groupable dimension.
_DIMENSION_WORDS = {
    "region", "country", "state", "city", "territory", "zone", "area", "market",
    "category", "type", "kind", "class", "group", "segment", "tier", "band",
    "channel", "source", "medium", "platform", "vendor", "status", "stage",
    "product", "brand", "department", "dept", "team", "division", "role",
    "plan", "method", "payment", "device", "os", "campaign", "industry",
    "priority", "rating", "tier", "bucket", "portfolio",
}
# Words that mark a column as a per-row identifier rather than a metric.
_IDENTITY_WORDS = {
    "id", "uuid", "guid", "key", "code", "sku", "isbn", "ean", "zip", "postcode",
    "email", "phone", "address", "url", "link", "handle", "slug",
}

# Question words that should also light up a column whose name differs.
COLUMN_SYNONYMS: Dict[str, set] = {
    "revenue": {"sales", "income", "turnover", "gmv", "topline", "takings"},
    "profit": {"earnings", "profit", "gain"},
    "amount": {"value", "total", "sum", "spend"},
    "quantity": {"qty", "units", "count", "volume"},
    "price": {"rate", "cost", "unit_price", "unitprice"},
    "cost": {"spend", "expense", "expenses", "cogs"},
    "region": {"area", "territory", "geography", "market", "zone", "location"},
    "category": {"type", "kind", "class", "group", "segment"},
    "channel": {"source", "medium", "platform", "vendor"},
    "customer": {"client", "buyer", "account", "user", "shopper"},
    "product": {"item", "sku", "article", "product"},
    "date": {"day", "time", "when", "period"},
    "month": {"monthly"},
    "year": {"yearly", "annual"},
    "status": {"state", "stage"},
    "segment": {"tier", "band", "cohort"},
    "profit_margin": {"margin"},
}

_STOPWORDS = frozenset(
    """
    a an the and or of for to in on at by with from as is are was were be been being
    do does did done have has had having will would shall should can could may might must
    me my we our you your it its this that these those there here what which who whom whose
    when where why how all any both each few more most other some such no nor not only own
    same so than too very s t just don now about into over under again further then once
    show give get make please tell list let us i
    """.split()
)

# Words that describe an analysis rather than a column. They never count as a
# reference to data the user does not have.
GENERIC_QUERY_WORDS = frozenset(
    """
    total totals sum summed average averaged avg mean median mode max maxes maximum
    min mins minimum count counts counted number numbers value values amount amounts
    figure figures stat stats statistic statistics metric measure measurement
    top bottom best worst biggest largest smallest highest lowest least most
    first last next previous latest recent historic historical current overall
    summary summarise summarize overview headline picture snapshot
    list lists show shows display draw chart charts plot plots graph graphs table
    breakdown break breaks breaking split splits splitting group groups grouped
    compare compares comparison versus vs against between paired relationship
    relate relates correlated correlation
    distribution distributed spread spread out histogram bucket buckets
    trend trends trending move moves movement change changes changed growing
    row rows record records entry entries line lines item items
    data dataset datasets file files column columns field fields
    many much more less few several all any some none
    made make makes giving give tell please let want need know look see show
    there here now then also just only even still yet
    """.split()
)


def singular(word: str) -> str:
    """A crude singulariser: enough to match `customers` to `customer`."""
    text = str(word or "").lower()
    if len(text) > 4 and text.endswith("ies"):
        return text[:-3] + "y"
    if len(text) > 4 and text.endswith("ses"):
        return text[:-2]
    if len(text) > 3 and text.endswith("s") and not text.endswith("ss"):
        return text[:-1]
    return text


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens, splitting camelCase and snake_case."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    spaced = re.sub(r"[_\-./\\]+", " ", spaced)
    return [t for t in re.sub(r"[^A-Za-z0-9%]+", " ", spaced).lower().split() if t]


def content_tokens(text: str) -> List[str]:
    return [t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 1]


def base_type(dtype: str) -> str:
    """Normalise a DuckDB type to its base name.

    DuckDB reports parameterised types such as ``DECIMAL(10, 2)``,
    ``VARCHAR(80)`` and ``TIMESTAMP WITH TIME ZONE``; role inference only
    cares about the family.
    """
    text = str(dtype or "").strip().upper()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    if text in {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"}:
        return "TIMESTAMP WITH TIME ZONE"
    if text in {"TIMESTAMP_NS", "TIMESTAMP_MS", "TIMESTAMP_S"}:
        return "TIMESTAMP"
    return text


def quote_ident(name: str) -> str:
    """Quote an identifier for DuckDB, refusing anything unexpected.

    Identifiers only ever come from DuckDB's own catalogue, but the check is
    cheap defence in depth against a hostile CSV header.
    """
    if not IDENT_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def humanise(name: str) -> str:
    """`order_date` / `orderDate` -> `Order date`, keeping acronyms intact."""
    parts = tokenize(name)
    if not parts:
        return str(name)
    text = " ".join(part.upper() if part in ACRONYMS else part for part in parts)
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- #
# Time bucketing
# --------------------------------------------------------------------------- #

_ISO_MONTH = re.compile(r"^\d{4}-\d{1,2}$")
_ISO_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_US_DATE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
_ISO_YEAR = re.compile(r"^\d{4}$")


def detect_time_format(samples: Sequence[str]) -> str:
    """Guess the textual layout of a date-ish column from its samples."""
    probe = [s.strip() for s in samples if s and s.strip()][:6]
    if not probe:
        return "unknown"
    if all(_ISO_MONTH.match(s) for s in probe):
        return "iso_month"
    if all(_ISO_DATE.match(s) for s in probe):
        return "iso_date"
    if all(_ISO_YEAR.match(s) for s in probe):
        return "iso_year"
    if all(_US_DATE.match(s) for s in probe):
        return "us_date"
    return "unknown"


def time_bucket_expression(column: ColumnInfo, granularity: str = "month") -> str:
    """SQL expression that buckets a time column into a period label.

    Works for real ``DATE``/``TIMESTAMP`` columns and for textual period
    columns such as ``"2024-03"`` or ``"03/14/2024"``. The expression only
    ever references the quoted column identifier - no user text is involved.
    """
    quoted = quote_ident(column.name)
    upper = base_type(column.dtype)

    if upper in _DATE_TYPES:
        fmt = "%Y-%m" if granularity == "month" else "%Y"
        return f"strftime({quoted}, '{fmt}')"

    if upper in _NUMERIC_TYPES:
        # A numeric year column such as `fiscal_year`.
        return f"CAST({quoted} AS VARCHAR)"

    layout = detect_time_format(column.samples)
    if layout == "iso_month":
        return f"substr({quoted}, 1, 7)"
    if layout == "iso_date":
        return "substr(" + quoted + ", 1, 7)"
    if layout == "iso_year":
        return quoted
    if layout == "us_date":
        fmt = "'%m/%d/%Y'" if "/" in (column.samples[0] if column.samples else "/") else "'%m-%d-%Y'"
        return f"strftime(TRY_STRPTIME({quoted}, {fmt}), '%Y-%m')"
    # Unknown layout: group by the raw value and let the formatter shorten it.
    return quoted


# --------------------------------------------------------------------------- #
# Profile model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    label: str
    dtype: str
    role: str
    distinct: int = 0
    null_ratio: float = 0.0
    samples: Tuple[str, ...] = ()
    unit: str = "number"  # money | percent | count | number | text | date

    @property
    def is_numeric(self) -> bool:
        return base_type(self.dtype) in _NUMERIC_TYPES

    @property
    def is_temporal(self) -> bool:
        return base_type(self.dtype) in _DATE_TYPES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.dtype,
            "role": self.role,
            "distinct": self.distinct,
            "null_ratio": round(self.null_ratio, 4),
            "samples": list(self.samples[:4]),
            "unit": self.unit,
        }


@dataclass(frozen=True)
class TableProfile:
    name: str
    label: str
    row_count: int
    columns: Tuple[ColumnInfo, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "row_count": self.row_count,
            "columns": [c.to_dict() for c in self.columns],
        }


@dataclass(frozen=True)
class DatasetProfile:
    id: str
    name: str
    created_at: str
    source_files: Tuple[str, ...]
    tables: Tuple[TableProfile, ...]

    # -- convenience accessors ------------------------------------------- #

    @property
    def primary_table(self) -> Optional[TableProfile]:
        return self.tables[0] if self.tables else None

    def all_columns(self) -> List[ColumnInfo]:
        out: List[ColumnInfo] = []
        for table in self.tables:
            out.extend(table.columns)
        return out

    def columns_with_role(self, role: str) -> List[ColumnInfo]:
        return [c for c in self.all_columns() if c.role == role]

    def table_of(self, column: ColumnInfo) -> Optional[TableProfile]:
        for table in self.tables:
            if any(c.name == column.name for c in table.columns):
                return table
        return None

    def table(self, name: str) -> Optional[TableProfile]:
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def find_column(self, table: str, column: str) -> Optional[ColumnInfo]:
        for t in self.tables:
            if t.name == table:
                for c in t.columns:
                    if c.name == column:
                        return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "source_files": list(self.source_files),
            "tables": [t.to_dict() for t in self.tables],
        }


# --------------------------------------------------------------------------- #
# Role + unit inference
# --------------------------------------------------------------------------- #


def infer_unit(name: str, dtype: str) -> str:
    upper = base_type(dtype)
    if upper in _DATE_TYPES:
        return "date"
    if upper not in _NUMERIC_TYPES:
        return "text"
    words = set(tokenize(name))
    if words & _EXPLICIT_PERCENT_WORDS or "%" in str(name):
        return "percent"
    if words & _PERCENT_WORDS:
        return "number"
    if words & _MONEY_WORDS:
        return "money"
    if words & {"count", "quantity", "qty", "units", "volume", "records", "rows", "orders"}:
        return "count"
    return "number"


def infer_role(
    name: str,
    dtype: str,
    distinct: int,
    row_count: int,
    samples: Sequence[str],
) -> str:
    """Assign a semantic role to one column."""
    upper = base_type(dtype)
    words = set(tokenize(name))
    label = humanise(name).lower()

    # Time is unambiguous: a real date type, or a period-ish name.
    if upper in _DATE_TYPES:
        return ROLE_TIME
    if words & {"year", "month", "week", "quarter"} and upper in _NUMERIC_TYPES:
        return ROLE_TIME
    if words & {"date", "datetime", "timestamp", "period"} and upper not in _NUMERIC_TYPES:
        # A textual date: only treat it as time if it parses as a date.
        probe = " ".join(samples[:3])
        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", probe):
            return ROLE_TIME
        return ROLE_ENTITY

    if upper in _NUMERIC_TYPES:
        # Numeric identifiers (customer_id = 1001) are not metrics.
        if words & _IDENTITY_WORDS or label.endswith(" id"):
            return ROLE_ENTITY
        if upper in {"TINYINT", "SMALLINT", "INTEGER", "BIGINT"} and distinct == row_count:
            # A unique integer key stored without the word "id".
            return ROLE_ENTITY
        return ROLE_METRIC

    # Textual columns.
    if words & _IDENTITY_WORDS or label.endswith(" id") or label.endswith(" name"):
        return ROLE_ENTITY
    if words & _DIMENSION_WORDS:
        return ROLE_DIMENSION
    if words & _TIME_WORDS:
        return ROLE_TIME
    # A text column with a distinct value on almost every row is not a category
    # you can meaningfully group by - it is an identifier wearing a text type.
    # Above a few dozen rows a near-unique text column is free text; below it,
    # treat uniqueness as ordinary (a 3-row file has 3 distinct values).
    if (
        row_count >= 50
        and base_type(dtype) in _TEXT_TYPES
        and distinct > max(30, row_count * 0.5)
    ):
        return ROLE_ENTITY

    # Fall back on cardinality: few distinct values group well, many do not.
    ratio = (distinct / row_count) if row_count else 0.0
    if distinct and distinct <= max(2, int(row_count * 0.05)) and distinct <= 60:
        return ROLE_DIMENSION
    if distinct <= 3:
        return ROLE_DIMENSION
    if ratio > 0.5:
        return ROLE_ENTITY
    return ROLE_ENTITY


# --------------------------------------------------------------------------- #
# Profiling a live DuckDB table
# --------------------------------------------------------------------------- #


def profile_table(con: Any, table: str) -> TableProfile:
    """Describe one table and profile every column."""
    described = con.execute(f"DESCRIBE {quote_ident(table)}").fetchall()
    row_count = int(con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])

    columns: List[ColumnInfo] = []
    for col_name, col_type, *_ in described:
        name = str(col_name)
        dtype = str(col_type)
        quoted = quote_ident(name)

        stats = con.execute(
            f"SELECT COUNT(DISTINCT {quoted}), "
            f"COUNT(*) FILTER (WHERE {quoted} IS NULL) "
            f"FROM {quote_ident(table)}"
        ).fetchone()
        distinct = int(stats[0] or 0)
        nulls = int(stats[1] or 0)

        samples: List[str] = []
        try:
            raw_samples = con.execute(
                f"SELECT {quoted} FROM {quote_ident(table)} "
                f"WHERE {quoted} IS NOT NULL GROUP BY 1 LIMIT 4"
            ).fetchall()
            samples = [str(r[0]) for r in raw_samples]
        except Exception:  # pragma: no cover - exotic column types
            samples = []

        role = infer_role(name, dtype, distinct, row_count, samples)
        columns.append(
            ColumnInfo(
                name=name,
                label=humanise(name),
                dtype=dtype,
                role=role,
                distinct=distinct,
                null_ratio=(nulls / row_count) if row_count else 0.0,
                samples=tuple(samples),
                unit=infer_unit(name, dtype),
            )
        )

    return TableProfile(
        name=table,
        label=humanise(table),
        row_count=row_count,
        columns=tuple(columns),
    )


# --------------------------------------------------------------------------- #
# Question -> column binding
# --------------------------------------------------------------------------- #


@dataclass
class Binding:
    """The columns a question resolved to."""

    table: Optional[str] = None
    metric: Optional[ColumnInfo] = None
    dimension: Optional[ColumnInfo] = None
    second_dimension: Optional[ColumnInfo] = None
    time: Optional[ColumnInfo] = None
    entity: Optional[ColumnInfo] = None
    second_metric: Optional[ColumnInfo] = None
    scores: Dict[str, float] = field(default_factory=dict)
    # Terms in the question that name something this dataset does not have.
    unmatched: List[str] = field(default_factory=list)
    # True when at least one column was named explicitly in the question.
    explicit: bool = False

    def describe(self) -> str:
        bits = []
        for label, col in (
            ("metric", self.metric),
            ("dimension", self.dimension),
            ("time", self.time),
            ("entity", self.entity),
        ):
            if col is not None:
                bits.append(f"{label}={col.name}")
        return ", ".join(bits) or "nothing"


def _lexical_score(question_tokens: Sequence[str], column: ColumnInfo) -> float:
    """Token overlap between the question and a column name."""
    if not question_tokens:
        return 0.0
    col_tokens = set(tokenize(column.name)) | set(tokenize(column.label))
    if not col_tokens:
        return 0.0

    score = 0.0
    for qt in question_tokens:
        if qt in col_tokens:
            score += 3.0
            continue
        for ct in col_tokens:
            if len(qt) >= 4 and len(ct) >= 4 and (qt.startswith(ct) or ct.startswith(qt)):
                score += 1.5
                break
        else:
            # Synonym expansion: a question word that maps onto this column.
            for ct in col_tokens:
                if qt in COLUMN_SYNONYMS.get(ct, set()):
                    score += 2.0
                    break
    return score


class SchemaBinder:
    """Resolves a natural-language question to concrete columns of a dataset.

    Lexical and semantic scores are combined. The semantic half uses the same
    local sentence-transformer that drives routing, so a dataset with a column
    called ``net_sales`` still matches the question "what was our revenue".
    """

    def __init__(self, model: Any, profile: DatasetProfile) -> None:
        self._model = model
        self._profile = profile
        self._descriptions: Dict[str, str] = {}
        self._matrix: Any = None
        self._names: List[str] = []
        self._prepare()

    # -- semantic index --------------------------------------------------- #

    def _describe(self, column: ColumnInfo) -> str:
        if column.role == ROLE_METRIC:
            return f"{column.label} {column.name} numeric {column.unit}"
        if column.role == ROLE_TIME:
            return f"{column.label} {column.name} date time period"
        if column.role == ROLE_DIMENSION:
            samples = ", ".join(column.samples[:4])
            return f"{column.label} {column.name} category group {samples}".strip()
        samples = ", ".join(column.samples[:3])
        return f"{column.label} {column.name} {samples}".strip()

    def _prepare(self) -> None:
        import numpy as np  # local import keeps module import cheap

        columns = self._profile.all_columns()
        if not columns or self._model is None:
            return
        self._names = [c.name for c in columns]
        self._descriptions = {c.name: self._describe(c) for c in columns}
        vectors = self._model.encode(
            [self._descriptions[c.name] for c in columns],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._matrix = np.asarray(vectors, dtype=np.float32)

    def _semantic_scores(self, question: str) -> Dict[str, float]:
        import numpy as np

        if self._matrix is None or not self._names:
            return {}
        vector = self._model.encode(
            [question],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]
        sims = self._matrix @ vector
        return {name: float(score) for name, score in zip(self._names, sims)}

    # -- binding ---------------------------------------------------------- #

    def bind(self, question: str) -> Binding:
        tokens = content_tokens(question)
        semantic = self._semantic_scores(question)

        scored: Dict[str, List[Tuple[float, ColumnInfo]]] = {}
        explicit = False
        for column in self._profile.all_columns():
            lex = _lexical_score(tokens, column)
            if lex > 0.0:
                explicit = True
            sem = semantic.get(column.name, 0.0)
            # Lexical dominates: an exact column-name hit should always beat a
            # fuzzy semantic match. Semantic is the tie-breaker.
            combined = lex + 3.0 * max(0.0, sem)
            scored.setdefault(column.role, []).append((combined, column))

        binding = Binding(scores={name: round(v, 4) for name, v in semantic.items()})
        binding.explicit = explicit

        metric = self._best(scored.get(ROLE_METRIC, []))
        dimension = self._best(scored.get(ROLE_DIMENSION, []))
        entity = self._best(scored.get(ROLE_ENTITY, []))
        time_col = self._best(scored.get(ROLE_TIME, []))

        # A dimension or entity the question never mentions should not win.
        if dimension is not None and _lexical_score(tokens, dimension) <= 0.0:
            if dimension.role == ROLE_DIMENSION and self._is_weak_match(dimension, tokens):
                dimension = None
        if entity is not None and _lexical_score(tokens, entity) <= 0.0:
            entity = None

        # Default fallbacks keep the app useful for a vague question.
        if metric is None:
            metric = self._default_metric()
        if dimension is None:
            dimension = self._default_dimension()
        if time_col is None:
            time_col = self._default_time()

        binding.metric = metric
        binding.dimension = dimension
        binding.entity = entity
        binding.time = time_col
        binding.second_dimension = self._second_dimension(dimension, tokens)
        binding.second_metric = self._second_metric(metric, tokens)

        if metric is not None:
            table = self._profile.table_of(metric)
            binding.table = table.name if table else None
        elif binding.table is None:
            primary = self._profile.primary_table
            binding.table = primary.name if primary else None

        binding.unmatched = self._unmatched_terms(tokens, explicit)
        return binding

    # -- helpers ---------------------------------------------------------- #

    def _unmatched_terms(self, tokens: Sequence[str], explicit: bool) -> List[str]:
        """Words that look like a column reference we cannot satisfy.

        Only consulted when the question named *no* column at all. If the user
        said "revenue by region" we trust that, even if they also said "sales".
        """
        if explicit:
            return []

        known: set = set()
        for column in self._profile.all_columns():
            known.update(tokenize(column.name))
            known.update(tokenize(column.label))
        known_singular = {singular(k) for k in known}

        unmatched: List[str] = []
        for token in tokens:
            if token in GENERIC_QUERY_WORDS:
                continue
            if token in known or singular(token) in known_singular:
                continue
            if any(
                token in COLUMN_SYNONYMS.get(key, ())
                or singular(token) in {singular(s) for s in COLUMN_SYNONYMS.get(key, ())}
                for key in known
            ):
                continue
            unmatched.append(token)
        return unmatched

    @staticmethod
    def _best(entries: List[Tuple[float, ColumnInfo]]) -> Optional[ColumnInfo]:
        if not entries:
            return None
        entries.sort(key=lambda kv: kv[0], reverse=True)
        return entries[0][1]

    def _is_weak_match(self, column: ColumnInfo, tokens: Sequence[str]) -> bool:
        """True when a dimension is a plausible-but-unmentioned default."""
        return len(tokens) > 0

    def _default_metric(self) -> Optional[ColumnInfo]:
        return default_metric(self._profile)

    def _default_dimension(self) -> Optional[ColumnInfo]:
        return default_dimension(self._profile)

    def _default_time(self) -> Optional[ColumnInfo]:
        return default_time(self._profile)

    def _second_dimension(
        self, primary: Optional[ColumnInfo], tokens: Sequence[str]
    ) -> Optional[ColumnInfo]:
        candidates = [
            c
            for c in self._profile.columns_with_role(ROLE_DIMENSION)
            if primary is None or c.name != primary.name
        ]
        if not candidates:
            return None
        scored = sorted(
            ((_lexical_score(tokens, c), c) for c in candidates),
            key=lambda kv: kv[0],
            reverse=True,
        )
        if scored[0][0] <= 0.0:
            return None
        return scored[0][1]

    def _second_metric(
        self, primary: Optional[ColumnInfo], tokens: Sequence[str]
    ) -> Optional[ColumnInfo]:
        candidates = [
            c
            for c in self._profile.columns_with_role(ROLE_METRIC)
            if primary is None or c.name != primary.name
        ]
        if not candidates:
            return None
        scored = sorted(
            ((_lexical_score(tokens, c), c) for c in candidates),
            key=lambda kv: kv[0],
            reverse=True,
        )
        if scored[0][0] <= 0.0:
            return None
        return scored[0][1]


def _metric_priority(column: ColumnInfo) -> int:
    """Higher is a more natural default metric."""
    words = set(tokenize(column.name))
    score = 0
    if words & _MONEY_WORDS:
        score += 5
    if words & _PERCENT_WORDS:
        score += 3
    if words & {"count", "quantity", "qty", "units", "volume"}:
        score += 2
    if words & _IDENTITY_WORDS:
        score -= 8
    return score


# --------------------------------------------------------------------------- #
# Schema defaults
# --------------------------------------------------------------------------- #


def default_metric(profile: DatasetProfile) -> Optional[ColumnInfo]:
    """The column that most naturally answers "how much?" for this dataset."""
    candidates = profile.columns_with_role(ROLE_METRIC)
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-_metric_priority(c), c.name))
    return candidates[0]


def default_dimension(profile: DatasetProfile) -> Optional[ColumnInfo]:
    """The column that most naturally answers "by what?" for this dataset."""
    candidates = profile.columns_with_role(ROLE_DIMENSION)
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.distinct, c.name))
    return candidates[0]


def default_time(profile: DatasetProfile) -> Optional[ColumnInfo]:
    """The column that most naturally answers "when?" for this dataset."""
    candidates = profile.columns_with_role(ROLE_TIME)
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.distinct, c.name))
    return candidates[0]


_ENTITY_PRIORITY = {
    "customer": 10, "client": 10, "account": 9, "buyer": 9, "shopper": 9,
    "user": 8, "product": 8, "item": 8, "sku": 8, "company": 8,
    "vendor": 7, "supplier": 7, "order": 6, "invoice": 6, "transaction": 6,
    "ticket": 6, "campaign": 6, "employee": 6, "name": 4, "id": 2, "code": 1,
}


def default_entity(profile: DatasetProfile) -> Optional[ColumnInfo]:
    """The column that most naturally answers "who or what?" for this dataset."""
    candidates = profile.columns_with_role(ROLE_ENTITY)
    if not candidates:
        return None

    def rank(column: ColumnInfo) -> tuple:
        words = set(tokenize(column.name))
        priority = max((_ENTITY_PRIORITY.get(w, 0) for w in words), default=0)
        return (-priority, column.distinct, column.name)

    candidates.sort(key=rank)
    return candidates[0]
