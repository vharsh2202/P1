"""Runtime configuration for the P1 analytics service.

Every knob is an environment variable with a safe default, so the app runs
with no setup at all. See ``backend/.env.example`` for the documented list.
"""

from __future__ import annotations

import os
from pathlib import Path

# backend/analytics/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

# Where uploaded datasets are persisted (CSV copies + manifest).
STORE_DIR = Path(os.getenv("P1_STORE_DIR", BACKEND_DIR / ".data"))

# Local embedding model used for semantic routing. CPU by default: the app is
# designed to run entirely offline on a laptop.
MODEL_NAME = os.getenv("P1_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
DEVICE = os.getenv("P1_EMBEDDING_DEVICE", "cpu")

# Cosine similarity floor for the router. Below this the question is refused
# rather than guessed at.
SIMILARITY_THRESHOLD = float(os.getenv("P1_INTENT_THRESHOLD", "0.42"))

# If the runner-up analysis scores within this margin of the winner, the
# question is treated as ambiguous and the user is offered the candidates.
AMBIGUITY_MARGIN = float(os.getenv("P1_INTENT_AMBIGUITY_MARGIN", "0.05"))

# If the out-of-scope class scores within this margin of the best analysis,
# the question is treated as out of scope rather than answered.
SCOPE_MARGIN = float(os.getenv("P1_SCOPE_MARGIN", "0.03"))

# Hard cap on rows returned to the browser.
MAX_ROWS = int(os.getenv("P1_RESULT_ROW_CAP", "500"))

# Upload ceiling (bytes). Uploads are read fully into memory, so this guards
# against a stray multi-gigabyte file.
MAX_UPLOAD_BYTES = int(os.getenv("P1_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))

# Maximum number of files accepted in a single upload.
MAX_UPLOAD_FILES = int(os.getenv("P1_MAX_UPLOAD_FILES", "20"))

CORS_ORIGINS = os.getenv(
    "P1_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

# File extensions the ingester understands.
SUPPORTED_EXTENSIONS = frozenset({".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson", ".xlsx", ".xlsm"})

# Maximum rows sampled when profiling a newly ingested table.
PROFILE_SAMPLE_LIMIT = 200
