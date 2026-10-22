"""Turning user files into queryable DuckDB tables.

The app ships no data of its own. Whatever the user drags into the browser is
normalised here into CSV, loaded into DuckDB, and profiled. The normalised CSV
is also what gets persisted, so a restarted server rebuilds the exact same
tables without re-parsing the original upload.

Supported inputs: CSV, TSV, TXT, JSON, JSONL/NDJSON, XLSX, XLSM.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MAX_UPLOAD_BYTES, SUPPORTED_EXTENSIONS


class IngestError(Exception):
    """Raised when a file cannot be turned into a table."""


@dataclass
class IngestedTable:
    """One table produced from one uploaded file (or sheet)."""

    name: str
    label: str
    csv_path: Path
    row_count: int
    column_count: int
    source: str


@dataclass
class IngestResult:
    tables: List[IngestedTable] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

_IDENT_CLEAN = re.compile(r"[^a-z0-9_]+")


def normalise_table_name(stem: str, taken: Optional[set] = None) -> str:
    """Turn a filename stem into a safe, unique SQL identifier."""
    taken = taken or set()
    base = _IDENT_CLEAN.sub("_", str(stem or "table").strip().lower()).strip("_")
    if not base:
        base = "table"
    if base[0].isdigit():
        base = f"t_{base}"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    taken.add(candidate)
    return candidate


def humanise_table(stem: str) -> str:
    text = re.sub(r"[_\-.]+", " ", str(stem or "")).strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return text[:1].upper() + text[1:] if text else "Table"


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _read_csv_to_rows(path: Path) -> Tuple[List[str], List[List[Any]]]:
    """Read a CSV/TSV into a header plus rows, using DuckDB's type detection."""
    import duckdb

    posix = path.resolve().as_posix()
    con = duckdb.connect(":memory:")
    try:
        sample = -1 if path.stat().st_size < 20 * 1024 * 1024 else 20480
        try:
            result = con.execute(
                f"SELECT * FROM read_csv_auto('{posix}', sample_size={sample})"
            )
        except Exception as exc:  # pragma: no cover - depends on the file
            raise IngestError(f"Could not parse CSV: {exc}") from exc
        header = [str(d[0]) for d in result.description]
        rows = [list(r) for r in result.fetchall()]
        return header, rows
    finally:
        con.close()


def _read_json_to_rows(path: Path) -> Tuple[List[str], List[List[Any]]]:
    import duckdb

    posix = path.resolve().as_posix()
    con = duckdb.connect(":memory:")
    try:
        try:
            result = con.execute(f"SELECT * FROM read_json_auto('{posix}')")
        except Exception:
            # JSONL / NDJSON: read_json_auto handles arrays, not line-delimited.
            result = con.execute(f"SELECT * FROM read_ndjson_auto('{posix}')")
        header = [str(d[0]) for d in result.description]
        rows = [list(r) for r in result.fetchall()]
        return header, rows
    except Exception as exc:
        raise IngestError(f"Could not parse JSON: {exc}") from exc
    finally:
        con.close()


def _read_xlsx_sheets(path: Path) -> List[Tuple[str, List[List[Any]]]]:
    """Read every sheet of a workbook into plain rows."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise IngestError(
            "Excel support needs the 'openpyxl' package. Install it with: "
            "pip install openpyxl"
        ) from exc

    try:
        workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise IngestError(f"Could not open workbook: {exc}") from exc

    sheets: List[Tuple[str, List[List[Any]]]] = []
    try:
        for worksheet in workbook.worksheets:
            rows: List[List[Any]] = []
            for row in worksheet.iter_rows(values_only=True):
                rows.append(list(row))
            # Drop trailing fully-empty rows Excel likes to leave behind.
            while rows and all(cell is None or str(cell).strip() == "" for cell in rows[-1]):
                rows.pop()
            if rows:
                sheets.append((worksheet.title, rows))
    finally:
        workbook.close()

    if not sheets:
        raise IngestError("The workbook contains no readable sheets.")
    return sheets


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def _write_rows_to_csv(
    header: List[Any], rows: List[List[Any]], destination: Path
) -> None:
    """Write a header plus rows to a CSV."""
    if not header:
        raise IngestError("The file contains no rows.")
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _load_csv_into_duckdb(csv_path: Path, table_name: str) -> Tuple[int, int]:
    """Load a normalised CSV into a fresh DuckDB database and return its shape."""
    import duckdb

    posix = csv_path.resolve().as_posix()
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{posix}', sample_size=-1)"
        )
        row_count, column_count = con.execute(
            f"SELECT COUNT(*), (SELECT COUNT(*) FROM information_schema.columns "
            f"WHERE table_name = '{table_name}') FROM {table_name}"
        ).fetchone()
        return int(row_count), int(column_count)
    except Exception as exc:
        raise IngestError(f"Could not load the table: {exc}") from exc
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def ingest_upload(
    files: List[Tuple[str, bytes]],
    destination: Path,
) -> IngestResult:
    """Normalise every uploaded file into CSVs under ``destination``.

    ``files`` is a list of ``(filename, raw_bytes)`` pairs. Returns the tables
    that were produced plus any non-fatal warnings.
    """
    result = IngestResult()
    destination.mkdir(parents=True, exist_ok=True)
    taken: set = set()

    for filename, payload in files:
        name = str(filename or "upload").strip() or "upload"
        extension = Path(name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            result.warnings.append(
                f"Skipped **{name}** - unsupported file type `{extension or 'unknown'}`. "
                "Use CSV, TSV, JSON, XLSX or XLSM."
            )
            continue
        if len(payload) == 0:
            result.warnings.append(f"Skipped **{name}** - the file is empty.")
            continue
        if len(payload) > MAX_UPLOAD_BYTES:
            result.warnings.append(
                f"Skipped **{name}** - larger than the "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            )
            continue

        stem = Path(name).stem or "table"
        table_name = normalise_table_name(stem, taken)
        label = humanise_table(stem)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            try:
                if extension in {".csv", ".tsv", ".txt"}:
                    source = tmp_dir / Path(name).name
                    source.write_bytes(payload)
                    header, rows = _read_csv_to_rows(source)
                    csv_path = destination / f"{table_name}.csv"
                    _write_rows_to_csv(header, rows, csv_path)
                    row_count = len(rows)

                elif extension in {".json", ".jsonl", ".ndjson"}:
                    source = tmp_dir / Path(name).name
                    source.write_bytes(payload)
                    header, rows = _read_json_to_rows(source)
                    csv_path = destination / f"{table_name}.csv"
                    _write_rows_to_csv(header, rows, csv_path)
                    row_count = len(rows)

                elif extension in {".xlsx", ".xlsm"}:
                    source = tmp_dir / Path(name).name
                    source.write_bytes(payload)
                    sheets = _read_xlsx_sheets(source)
                    row_count = 0
                    csv_path = destination / f"{table_name}.csv"
                    for index, (sheet_title, sheet_rows) in enumerate(sheets):
                        if index == 0:
                            target_name, target_label = table_name, label
                        else:
                            target_name = normalise_table_name(
                                f"{stem}_{sheet_title}", taken
                            )
                            target_label = humanise_table(sheet_title)
                        sheet_csv = destination / f"{target_name}.csv"
                        header = sheet_rows[0]
                        body = sheet_rows[1:]
                        _write_rows_to_csv(header, body, sheet_csv)
                        row_count += len(body)
                        loaded_rows, column_count = _load_csv_into_duckdb(
                            sheet_csv, target_name
                        )
                        result.tables.append(
                            IngestedTable(
                                name=target_name,
                                label=target_label,
                                csv_path=sheet_csv,
                                row_count=loaded_rows,
                                column_count=column_count,
                                source=(
                                    f"{name} · {sheet_title}"
                                    if index > 0
                                    else name
                                ),
                            )
                        )
                    continue

                else:  # pragma: no cover - guarded above
                    continue

                loaded_rows, column_count = _load_csv_into_duckdb(csv_path, table_name)
                result.tables.append(
                    IngestedTable(
                        name=table_name,
                        label=label,
                        csv_path=csv_path,
                        row_count=loaded_rows,
                        column_count=column_count,
                        source=name,
                    )
                )
                if loaded_rows == 0:
                    result.warnings.append(f"**{name}** loaded but contains no data rows.")

            except IngestError as exc:
                result.warnings.append(f"Skipped **{name}** - {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                result.warnings.append(f"Skipped **{name}** - unexpected error: {exc}")

    if not result.tables and not result.warnings:
        result.warnings.append("No usable files were found in the upload.")
    return result


def copy_originals(files: List[Tuple[str, bytes]], destination: Path) -> List[str]:
    """Keep a copy of the raw uploads next to the normalised CSVs."""
    destination.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    for filename, payload in files:
        safe = Path(str(filename or "upload")).name
        safe = re.sub(r"[^A-Za-z0-9._\- ]+", "_", safe) or "upload"
        target = destination / f"original_{safe}"
        try:
            shutil.copyfileobj(_BytesReader(payload), target.open("wb"))
            saved.append(safe)
        except Exception:  # pragma: no cover - best effort only
            continue
    return saved


class _BytesReader:
    """Tiny file-like wrapper so the raw upload can be streamed to disk."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._payload[self._offset :]
            self._offset = len(self._payload)
            return chunk
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "_BytesReader":
        return self

    def __exit__(self, *args: Any) -> None:
        return None
