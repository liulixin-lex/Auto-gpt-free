from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(slots=True)
class TaskProgress:
    current: int = 0
    total: int = 0
    label: str = "0/0"


@dataclass(slots=True)
class TaskSummary:
    id: str
    type: str
    platform: str
    status: str
    progress: TaskProgress
    success: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    cashier_urls: list[str] = field(default_factory=list)
    error: str = ""
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskEvent:
    id: int
    task_id: str
    type: str
    level: str
    message: str
    line: str
    kind: str = "log"
    attempt_id: str = ""
    seq: int = 0
    stage: str = ""
    action: str = ""
    event_code: str = ""
    error_code: str = ""
    retryable: bool = False
    retry_index: int = 0
    duration_ms: int = 0
    schema_version: int = 1
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
