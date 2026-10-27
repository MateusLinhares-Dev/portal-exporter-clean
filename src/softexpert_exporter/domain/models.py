from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from softexpert_exporter.domain.enums import JobStatus

ExportMode = Literal['test', 'historical', 'incremental']

@dataclass(frozen=True, slots=True)
class ReportDescriptor:
    url: str
    document_id: str
    portal_path: tuple[str, ...]
    tab: str
    code: str
    unit: str
    date: str = ''

@dataclass(frozen=True, slots=True)
class StoredFile:
    relative_path: str
    absolute_path: Path
    sha256: str
    size_bytes: int

@dataclass(slots=True)
class SyncJob:
    id: str
    status: JobStatus
    requested_by: str
    mode: ExportMode
    options: dict[str, Any]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    total_records: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    error_summary: str | None = None
