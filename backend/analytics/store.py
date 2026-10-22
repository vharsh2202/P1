"""The dataset registry.

Datasets live on disk as a folder of normalised CSVs plus a manifest entry, so
they survive a server restart. Each dataset gets its own in-memory DuckDB
connection, rebuilt from those CSVs on demand.
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import STORE_DIR
from .ingest import IngestResult, ingest_upload
from .profile import DatasetProfile, TableProfile, profile_table

MANIFEST_NAME = "manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Dataset:
    """One uploaded dataset plus its live DuckDB connection."""

    id: str
    name: str
    created_at: str
    source_files: List[str]
    folder: Path
    tables: List[TableProfile] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    con: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    # -- schema ----------------------------------------------------------- #

    def profile(self) -> DatasetProfile:
        return DatasetProfile(
            id=self.id,
            name=self.name,
            created_at=self.created_at,
            source_files=tuple(self.source_files),
            tables=tuple(self.tables),
        )

    def table(self, name: str) -> Optional[TableProfile]:
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def to_summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "source_files": list(self.source_files),
            "tables": [
                {
                    "name": t.name,
                    "label": t.label,
                    "row_count": t.row_count,
                    "column_count": len(t.columns),
                }
                for t in self.tables
            ],
            "row_count": sum(t.row_count for t in self.tables),
            "column_count": sum(len(t.columns) for t in self.tables),
        }


class DatasetStore:
    """Registry of every dataset the user has uploaded."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or STORE_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self._datasets: Dict[str, Dataset] = {}
        self._active_id: Optional[str] = None
        self._lock = threading.RLock()

    # -- manifest --------------------------------------------------------- #

    @property
    def _manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def _read_manifest(self) -> Dict[str, Any]:
        if not self._manifest_path.exists():
            return {"datasets": [], "active": None}
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"datasets": [], "active": None}

    def _write_manifest(self) -> None:
        payload = {
            "active": self._active_id,
            "datasets": [
                {
                    "id": d.id,
                    "name": d.name,
                    "created_at": d.created_at,
                    "source_files": d.source_files,
                    "tables": [
                        {"name": t.name, "csv": f"{t.name}.csv"}
                        for t in d.tables
                    ],
                }
                for d in self._datasets.values()
            ],
        }
        self._manifest_path.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    # -- lifecycle -------------------------------------------------------- #

    def restore(self) -> None:
        """Rebuild every persisted dataset. Called once at startup."""
        manifest = self._read_manifest()
        for entry in manifest.get("datasets", []):
            dataset_id = entry.get("id")
            folder = self.root / str(dataset_id)
            if not dataset_id or not folder.is_dir():
                continue
            try:
                tables: List[TableProfile] = []
                for table_entry in entry.get("tables", []):
                    table_name = str(table_entry.get("name"))
                    csv_path = folder / str(table_entry.get("csv", f"{table_name}.csv"))
                    if not csv_path.exists():
                        continue
                    con = self._connect(folder, [table_name])
                    tables.append(profile_table(con, table_name))
                    con.close()
                if not tables:
                    continue
                dataset = Dataset(
                    id=dataset_id,
                    name=entry.get("name", "Dataset"),
                    created_at=entry.get("created_at", _now()),
                    source_files=list(entry.get("source_files", [])),
                    folder=folder,
                    tables=tables,
                )
                dataset.con = self._connect(folder, [t.name for t in tables])
                with self._lock:
                    self._datasets[dataset_id] = dataset
            except Exception:  # pragma: no cover - a broken folder must not kill boot
                continue

        active = manifest.get("active")
        with self._lock:
            if active and active in self._datasets:
                self._active_id = active
            elif self._datasets:
                self._active_id = next(iter(self._datasets))

    @staticmethod
    def _connect(folder: Path, table_names: List[str]) -> Any:
        import duckdb

        con = duckdb.connect(":memory:")
        for table_name in table_names:
            csv_path = folder / f"{table_name}.csv"
            if not csv_path.exists():
                continue
            posix = csv_path.resolve().as_posix()
            con.execute(
                f'CREATE TABLE "{table_name}" AS '
                f"SELECT * FROM read_csv_auto('{posix}', sample_size=-1)"
            )
        return con

    # -- accessors -------------------------------------------------------- #

    def list_datasets(self) -> List[Dict[str, Any]]:
        with self._lock:
            summaries = [d.to_summary() for d in self._datasets.values()]
        for summary in summaries:
            summary["active"] = summary["id"] == self._active_id
        return sorted(summaries, key=lambda s: s["created_at"], reverse=True)

    def get(self, dataset_id: str) -> Optional[Dataset]:
        with self._lock:
            return self._datasets.get(dataset_id)

    def active(self) -> Optional[Dataset]:
        with self._lock:
            if not self._active_id:
                return None
            return self._datasets.get(self._active_id)

    def activate(self, dataset_id: str) -> Optional[Dataset]:
        with self._lock:
            dataset = self._datasets.get(dataset_id)
            if dataset is None:
                return None
            self._active_id = dataset_id
            self._write_manifest()
            return dataset

    # -- mutations -------------------------------------------------------- #

    def create(
        self,
        name: str,
        files: List[Tuple[str, bytes]],
    ) -> Tuple[Optional[Dataset], List[str]]:
        """Ingest ``files`` into a brand new dataset and activate it."""
        dataset_id = uuid.uuid4().hex[:12]
        folder = self.root / dataset_id
        folder.mkdir(parents=True, exist_ok=True)

        result: IngestResult = ingest_upload(files, folder)
        if not result.tables:
            shutil.rmtree(folder, ignore_errors=True)
            return None, result.warnings

        dataset = Dataset(
            id=dataset_id,
            name=name or (result.tables[0].label if result.tables else "Dataset"),
            created_at=_now(),
            source_files=[t.source for t in result.tables],
            folder=folder,
            warnings=list(result.warnings),
        )
        dataset.con = self._connect(folder, [t.name for t in result.tables])
        for table in result.tables:
            dataset.tables.append(profile_table(dataset.con, table.name))

        with self._lock:
            self._datasets[dataset_id] = dataset
            self._active_id = dataset_id
            self._write_manifest()
        return dataset, result.warnings

    def delete(self, dataset_id: str) -> bool:
        with self._lock:
            dataset = self._datasets.pop(dataset_id, None)
            if dataset is None:
                return False
            try:
                if dataset.con is not None:
                    dataset.con.close()
            except Exception:  # pragma: no cover - best effort
                pass
            shutil.rmtree(dataset.folder, ignore_errors=True)
            if self._active_id == dataset_id:
                self._active_id = next(iter(self._datasets), None)
            self._write_manifest()
            return True

    def rename(self, dataset_id: str, name: str) -> Optional[Dataset]:
        with self._lock:
            dataset = self._datasets.get(dataset_id)
            if dataset is None or not name.strip():
                return None
            dataset.name = name.strip()[:120]
            self._write_manifest()
            return dataset
