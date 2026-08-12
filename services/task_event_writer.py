"""Single-writer batching for SQLite task events."""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Callable


BatchWriter = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


@dataclass(slots=True)
class _WriteRequest:
    payload: dict[str, Any]
    writer: BatchWriter
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class TaskEventWriter:
    def __init__(self, *, max_batch: int = 50, flush_interval: float = 0.075) -> None:
        self.max_batch = max(1, int(max_batch))
        self.flush_interval = max(float(flush_interval), 0.01)
        self._queue: queue.Queue[_WriteRequest | None] = queue.Queue(maxsize=20000)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = False

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="task-event-writer",
            )
            self._thread.start()

    def submit(
        self,
        payload: dict[str, Any],
        writer: BatchWriter,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        self._ensure_started()
        request = _WriteRequest(payload=dict(payload), writer=writer)
        self._queue.put(request, timeout=max(float(timeout), 0.1))
        if not request.done.wait(timeout=max(float(timeout), 0.1)):
            raise TimeoutError("task event writer did not flush in time")
        if request.error:
            raise request.error
        return dict(request.result or {})

    def flush(self, timeout: float = 10.0) -> bool:
        end = time.monotonic() + max(float(timeout), 0.1)
        while self._queue.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return
            self._stopping = True
            self._queue.put(None)
            thread = self._thread
        thread.join(timeout=max(float(timeout), 0.1))

    def _run(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                self._queue.task_done()
                return
            batch = [first]
            deadline = time.monotonic() + self.flush_interval
            while len(batch) < self.max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    self._queue.task_done()
                    self._stopping = True
                    break
                batch.append(item)

            try:
                results = first.writer([item.payload for item in batch])
                if len(results) != len(batch):
                    raise RuntimeError("task event batch writer returned an invalid result count")
                for request, result in zip(batch, results, strict=True):
                    request.result = result
            except BaseException as exc:
                for request in batch:
                    request.error = exc
            finally:
                for request in batch:
                    request.done.set()
                    self._queue.task_done()
            if self._stopping and self._queue.empty():
                return


task_event_writer = TaskEventWriter()
