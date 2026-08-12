"""Cross-mode registration capacity, egress cooldown and adaptive health."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import threading
import time
from typing import Callable, Iterator

from domain.registration_runtime import RegistrationErrorCode, RegistrationMode


def _memory_snapshot() -> tuple[float, float]:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return float(memory.available) / (1024 * 1024), float(memory.percent)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return float(status.available_physical) / (1024 * 1024), float(status.memory_load)
        except Exception:
            return 0.0, 0.0
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="ascii") as source:
            for line in source:
                key, _, raw = line.partition(":")
                values[key] = int((raw.strip().split() or ["0"])[0])
        total = float(values.get("MemTotal", 0))
        available = float(values.get("MemAvailable", 0))
        used_percent = 100.0 * (1.0 - available / total) if total else 0.0
        return available / 1024.0, used_percent
    except Exception:
        return 0.0, 0.0


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True, slots=True)
class ModeCapacity:
    default: int
    maximum: int
    direct_maximum: int


MODE_CAPACITY: dict[RegistrationMode, ModeCapacity] = {
    RegistrationMode.PROTOCOL: ModeCapacity(
        default=1,
        maximum=_env_int("REGISTER_PROTOCOL_MAX", 20, 1, 20),
        direct_maximum=_env_int("REGISTER_PROTOCOL_DIRECT_MAX", 20, 1, 20),
    ),
    RegistrationMode.HEADLESS: ModeCapacity(
        default=1,
        maximum=_env_int("REGISTER_HEADLESS_MAX", 4, 1, 8),
        direct_maximum=_env_int("REGISTER_HEADLESS_DIRECT_MAX", 4, 1, 8),
    ),
    RegistrationMode.HEADED: ModeCapacity(
        default=1,
        maximum=_env_int("REGISTER_HEADED_MAX", 2, 1, 4),
        direct_maximum=_env_int("REGISTER_HEADED_DIRECT_MAX", 3, 1, 4),
    ),
}

# A direct address is a single physical egress shared by protocol and browser
# attempts.  Mode health may grow independently, but the shared gate never
# exceeds this operational ceiling.
DIRECT_EGRESS_HARD_MAX = _env_int("REGISTER_DIRECT_EGRESS_MAX", 3, 1, 20)


class CapacityTimeout(RuntimeError):
    pass


class _CountingGate:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(int(capacity), 1)
        self.active = 0
        self._condition = threading.Condition()

    def set_capacity(self, capacity: int) -> None:
        with self._condition:
            self.capacity = max(int(capacity), 1)
            self._condition.notify_all()

    def acquire(self, *, timeout: float, cancel_check: Callable[[], bool]) -> bool:
        end = time.monotonic() + max(float(timeout), 0.0)
        with self._condition:
            if cancel_check():
                return False
            while self.active >= self.capacity:
                if cancel_check():
                    return False
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.25))
            if cancel_check():
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._condition:
            self.active = max(self.active - 1, 0)
            self._condition.notify_all()


@dataclass(slots=True)
class _HealthState:
    limit: int
    window: deque[tuple[bool, RegistrationErrorCode | None, float]]
    success_streak: int = 0
    failure_streak: int = 0
    cooldown_until: float = 0.0
    state: str = "closed"
    last_error_code: str = ""


@dataclass(slots=True)
class _DirectEgressState:
    """Health owned by the physical direct exit, rather than by a mode."""

    limit: int = 1
    cooldown_until: float = 0.0
    state: str = "closed"
    last_error_code: str = ""
    recovery_limit: int = 1


class AdaptiveModeController:
    """Persistent AIMD controller keyed by registration mode and egress."""

    _PRESSURE_CODES = {
        RegistrationErrorCode.HTTP_RATE_LIMIT,
        RegistrationErrorCode.CF_CHALLENGE,
        RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE,
        RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
        RegistrationErrorCode.AUTH_SESSION_DESYNC,
        RegistrationErrorCode.DEADLINE_EXCEEDED,
        RegistrationErrorCode.NET_PROXY,
        RegistrationErrorCode.SENTINEL_SDK_DRIFT,
        RegistrationErrorCode.SENTINEL_PROOF,
        RegistrationErrorCode.AUTH_INVALID_STEP,
        RegistrationErrorCode.BROWSER_STATE_UNKNOWN,
        RegistrationErrorCode.OTP_TIMEOUT,
    }
    _IMMEDIATE_CODES = {
        RegistrationErrorCode.HTTP_RATE_LIMIT,
        RegistrationErrorCode.CF_CHALLENGE,
        RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE,
        RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
        RegistrationErrorCode.AUTH_SESSION_DESYNC,
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[RegistrationMode, str], _HealthState] = {}

    @staticmethod
    def _cooldown_seconds(code: RegistrationErrorCode | None) -> int:
        if code in {
            RegistrationErrorCode.CF_CHALLENGE,
            RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE,
            RegistrationErrorCode.HTTP_RATE_LIMIT,
        }:
            return 600
        if code in {
            RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
            RegistrationErrorCode.AUTH_SESSION_DESYNC,
        }:
            return 120
        if code in {RegistrationErrorCode.NET_PROXY, RegistrationErrorCode.NET_DNS, RegistrationErrorCode.NET_TLS}:
            return 30
        return 300

    def _load(self, mode: RegistrationMode, egress_ref: str, *, direct: bool) -> _HealthState:
        key = (mode, egress_ref or "direct")
        state = self._states.get(key)
        if state is not None:
            return state
        initial = 1 if direct else MODE_CAPACITY[mode].default
        state = _HealthState(limit=initial, window=deque(maxlen=20))
        try:
            from infrastructure.registration_repository import registration_resource_health

            stored = registration_resource_health.get(mode=mode.value, egress_ref=key[1])
            if stored:
                state.limit = max(int(stored.get("healthy_concurrency") or initial), 1)
                state.success_streak = max(int(stored.get("success_streak") or 0), 0)
                state.failure_streak = max(int(stored.get("failure_streak") or 0), 0)
                state.state = str(stored.get("state") or "closed")
                state.last_error_code = str(stored.get("last_error_code") or "")
                raw_until = str(stored.get("cooldown_until") or "")
                if raw_until:
                    parsed = datetime.fromisoformat(raw_until)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    remaining = (parsed - datetime.now(timezone.utc)).total_seconds()
                    state.cooldown_until = time.monotonic() + max(remaining, 0.0)
                for item in stored.get("window") or []:
                    try:
                        code = RegistrationErrorCode(item.get("error_code")) if item.get("error_code") else None
                    except ValueError:
                        code = None
                    state.window.append((bool(item.get("success")), code, float(item.get("duration_seconds") or 0)))
        except Exception:
            pass
        self._states[key] = state
        return state

    def _persist(self, mode: RegistrationMode, egress_ref: str, state: _HealthState) -> None:
        try:
            from infrastructure.registration_repository import registration_resource_health

            remaining = max(state.cooldown_until - time.monotonic(), 0.0)
            cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=remaining) if remaining else None
            registration_resource_health.put(
                mode=mode.value,
                egress_ref=egress_ref,
                state=state.state,
                healthy_concurrency=state.limit,
                success_streak=state.success_streak,
                failure_streak=state.failure_streak,
                last_error_code=state.last_error_code,
                cooldown_until=cooldown_until,
                window=[
                    {
                        "success": ok,
                        "error_code": code.value if code else "",
                        "duration_seconds": duration,
                    }
                    for ok, code, duration in state.window
                ],
            )
        except Exception:
            pass

    def effective_limit(
        self,
        mode: RegistrationMode,
        requested: int,
        *,
        direct: bool,
        egress_ref: str = "direct",
    ) -> int:
        config = MODE_CAPACITY[mode]
        with self._lock:
            state = self._load(mode, egress_ref, direct=direct)
            adaptive = state.limit
            if state.cooldown_until and time.monotonic() >= state.cooldown_until:
                state.cooldown_until = 0.0
                state.state = "half_open"
                adaptive = min(adaptive, 1)
        hard_limit = config.direct_maximum if direct else config.maximum
        return max(1, min(int(requested or 1), adaptive, hard_limit))

    def cooldown_remaining(self, mode: RegistrationMode, egress_ref: str, *, direct: bool) -> float:
        with self._lock:
            state = self._load(mode, egress_ref, direct=direct)
            return max(state.cooldown_until - time.monotonic(), 0.0)

    def record(
        self,
        mode: RegistrationMode,
        *,
        success: bool,
        error_code: RegistrationErrorCode | None,
        duration_seconds: float,
        memory_percent: float | None = None,
        egress_ref: str = "direct",
        direct: bool = False,
    ) -> None:
        config = MODE_CAPACITY[mode]
        with self._lock:
            state = self._load(mode, egress_ref, direct=direct)
            state.window.append((bool(success), error_code, max(float(duration_seconds), 0.0)))
            if success:
                state.success_streak += 1
                state.failure_streak = 0
                state.last_error_code = ""
                if state.state == "half_open":
                    state.state = "closed"
                    state.cooldown_until = 0.0
                if state.success_streak >= 5 and (memory_percent is None or memory_percent < 75.0):
                    hard_limit = config.direct_maximum if direct else config.maximum
                    state.limit = min(state.limit + 1, hard_limit)
                    state.success_streak = 0
            else:
                state.success_streak = 0
                state.failure_streak += 1
                state.last_error_code = error_code.value if error_code else ""
                pressure = sum(1 for ok, code, _ in state.window if not ok and code in self._PRESSURE_CODES)
                immediate = error_code in self._IMMEDIATE_CODES
                if immediate or (len(state.window) >= 5 and pressure / len(state.window) >= 0.20):
                    state.limit = 1 if immediate else max(1, state.limit // 2)
                    state.cooldown_until = time.monotonic() + self._cooldown_seconds(error_code)
                    state.state = "open"
            self._persist(mode, egress_ref, state)

    def snapshot(self, mode: RegistrationMode, egress_ref: str, *, direct: bool) -> dict[str, object]:
        with self._lock:
            state = self._load(mode, egress_ref, direct=direct)
            remaining = max(state.cooldown_until - time.monotonic(), 0.0)
            return {
                "healthy_concurrency": state.limit,
                "egress_state": state.state,
                "cooldown_seconds": int(remaining),
                "last_error_code": state.last_error_code,
            }

    def extend_cooldown(
        self,
        mode: RegistrationMode,
        egress_ref: str,
        *,
        direct: bool,
        seconds: int,
        error_code: RegistrationErrorCode,
    ) -> None:
        with self._lock:
            state = self._load(mode, egress_ref, direct=direct)
            state.limit = 1
            state.state = "open"
            state.last_error_code = error_code.value
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + max(int(seconds), 1),
            )
            self._persist(mode, egress_ref, state)


class RegistrationCapacityManager:
    def __init__(self) -> None:
        self._mode_gates = {mode: _CountingGate(config.maximum) for mode, config in MODE_CAPACITY.items()}
        self._direct_health_lock = threading.RLock()
        self._direct_health: dict[str, _DirectEgressState] = {}
        self._direct_gates: dict[str, _CountingGate] = {}
        self._proxy_lock = threading.Lock()
        self._proxy_gates: dict[str, _CountingGate] = {}
        self._provider_lock = threading.Lock()
        self._provider_gates: dict[str, _CountingGate] = {}
        self._pace_lock = threading.Lock()
        self._next_start: dict[str, float] = {}
        self._finish_cooldown: dict[str, float] = defaultdict(float)
        self._identity_lock = threading.Lock()
        self.adaptive = AdaptiveModeController()

    @staticmethod
    def _shared_direct_health_key(egress_ref: str) -> str:
        return f"shared:{str(egress_ref or 'direct')}"

    def _load_direct_health(self, egress_ref: str) -> _DirectEgressState:
        state = _DirectEgressState()
        try:
            from infrastructure.registration_repository import registration_resource_health

            stored = registration_resource_health.get(
                mode="direct_egress",
                egress_ref=self._shared_direct_health_key(egress_ref),
            ) or {}
            state.limit = min(
                max(int(stored.get("healthy_concurrency") or 1), 1),
                DIRECT_EGRESS_HARD_MAX,
            )
            state.recovery_limit = state.limit
            state.state = str(stored.get("state") or "closed")
            state.last_error_code = str(stored.get("last_error_code") or "")
            raw_until = str(stored.get("cooldown_until") or "")
            if raw_until:
                parsed = datetime.fromisoformat(raw_until)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                remaining = (parsed - datetime.now(timezone.utc)).total_seconds()
                state.cooldown_until = time.monotonic() + max(remaining, 0.0)
        except Exception:
            pass
        return state

    def _direct_state(self, egress_ref: str) -> _DirectEgressState:
        key = str(egress_ref or "direct")
        state = self._direct_health.get(key)
        if state is None:
            state = self._load_direct_health(key)
            self._direct_health[key] = state
        return state

    def _direct_gate_for(self, egress_ref: str) -> _CountingGate:
        key = str(egress_ref or "direct")
        with self._direct_health_lock:
            gate = self._direct_gates.get(key)
            if gate is None:
                state = self._direct_state(key)
                gate = _CountingGate(state.limit)
                self._direct_gates[key] = gate
            return gate

    def _persist_direct_health(self, egress_ref: str, state: _DirectEgressState) -> None:
        try:
            from infrastructure.registration_repository import registration_resource_health

            remaining = max(state.cooldown_until - time.monotonic(), 0.0)
            registration_resource_health.put(
                mode="direct_egress",
                egress_ref=self._shared_direct_health_key(egress_ref),
                state=state.state,
                healthy_concurrency=state.limit,
                success_streak=0,
                failure_streak=0,
                last_error_code=state.last_error_code,
                cooldown_until=(
                    datetime.now(timezone.utc) + timedelta(seconds=remaining)
                    if remaining
                    else None
                ),
                window=[],
            )
        except Exception:
            pass

    def effective_concurrency(
        self,
        mode: RegistrationMode,
        requested: int,
        *,
        direct: bool,
        count: int,
        proxy_count: int | None = None,
        egress_ref: str = "direct",
    ) -> int:
        value = self.adaptive.effective_limit(mode, requested, direct=direct, egress_ref=egress_ref)
        value = min(value, max(int(count), 1))
        if not direct and proxy_count is not None and proxy_count > 0:
            value = min(value, int(proxy_count))
        if mode in {RegistrationMode.HEADLESS, RegistrationMode.HEADED}:
            available_mb, _ = _memory_snapshot()
            if available_mb > 0:
                reserve_mb = _env_int("REGISTER_MEMORY_RESERVE_MB", 768, 256, 8192)
                default_worker_mb = 900 if mode == RegistrationMode.HEADLESS else 1100
                worker_mb = _env_int(f"REGISTER_{mode.value.upper()}_WORKER_MB", default_worker_mb, 256, 4096)
                value = min(value, max(int((available_mb - reserve_mb) // worker_mb), 1))
        if direct:
            health = self.adaptive.snapshot(mode, egress_ref, direct=True)
            global_limit = self._update_direct_health(
                mode,
                egress_ref,
                int(health.get("healthy_concurrency") or 1),
            )
            shared = self._direct_snapshot(egress_ref)
            if (
                str(shared.get("egress_state") or "closed") == "closed"
                and int(shared.get("cooldown_seconds") or 0) <= 0
            ):
                # Shared egress capacity is persisted separately from each
                # executor's AIMD window.  A restart must not erase a proven
                # two-slot direct exit merely because this mode has not yet
                # rebuilt its in-memory history.
                value = max(
                    value,
                    min(int(requested or 1), int(global_limit), MODE_CAPACITY[mode].direct_maximum),
                )
            value = min(value, global_limit)
        return max(value, 1)

    def _update_direct_health(
        self,
        mode: RegistrationMode,
        egress_ref: str,
        healthy_limit: int,
    ) -> int:
        del mode
        egress_key = str(egress_ref or "direct")
        with self._direct_health_lock:
            state = self._direct_state(egress_key)
            now = time.monotonic()
            if state.cooldown_until and now >= state.cooldown_until:
                # Half-open admits one probe.  A successful outcome closes it;
                # we do not immediately restore a historical multi-slot limit.
                state.cooldown_until = 0.0
                state.state = "half_open"
                state.recovery_limit = max(state.recovery_limit, state.limit)
                state.limit = 1
                self._direct_gate_for(egress_key).set_capacity(1)
            if state.cooldown_until <= now and state.state != "half_open":
                # A fresh mode with only one requested worker is not evidence
                # that a shared egress previously proven healthy at two slots
                # has regressed.  Only pressure errors may lower this value.
                state.limit = max(
                    state.limit,
                    min(max(int(healthy_limit), 1), DIRECT_EGRESS_HARD_MAX),
                )
            global_limit = state.limit
            self._persist_direct_health(egress_key, state)
        self._direct_gate_for(egress_key).set_capacity(global_limit)
        return global_limit

    def _direct_snapshot(self, egress_ref: str) -> dict[str, object]:
        egress_key = str(egress_ref or "direct")
        with self._direct_health_lock:
            state = self._direct_state(egress_key)
            now = time.monotonic()
            if state.cooldown_until and now >= state.cooldown_until:
                state.recovery_limit = max(state.recovery_limit, state.limit)
                state.cooldown_until = 0.0
                state.state = "half_open"
                state.limit = 1
                self._direct_gate_for(egress_key).set_capacity(1)
                self._persist_direct_health(egress_key, state)
            remaining = max(state.cooldown_until - now, 0.0)
            return {
                "healthy_concurrency": state.limit,
                "egress_state": state.state,
                "cooldown_seconds": int(remaining),
                "last_error_code": state.last_error_code,
            }

    def _apply_direct_pressure(
        self,
        egress_ref: str,
        error_code: RegistrationErrorCode,
        *,
        cooldown_seconds: int | None = None,
    ) -> None:
        """Trip the shared direct circuit immediately for anti-abuse signals."""
        egress_key = str(egress_ref or "direct")
        cooldown = int(
            cooldown_seconds
            if cooldown_seconds is not None
            else AdaptiveModeController._cooldown_seconds(error_code)
        )
        with self._direct_health_lock:
            state = self._direct_state(egress_key)
            previous_limit = state.limit
            state.limit = 1
            state.state = "open"
            state.last_error_code = error_code.value
            state.recovery_limit = max(state.recovery_limit, previous_limit, 1)
            state.cooldown_until = max(
                state.cooldown_until,
                time.monotonic() + cooldown,
            )
            self._persist_direct_health(egress_key, state)
        self._direct_gate_for(egress_key).set_capacity(1)

    def _close_direct_probe(self, egress_ref: str, observed_limit: int) -> None:
        """Close a half-open direct circuit after a successful probe."""
        egress_key = str(egress_ref or "direct")
        with self._direct_health_lock:
            state = self._direct_state(egress_key)
            if state.state != "half_open":
                return
            state.state = "closed"
            state.cooldown_until = 0.0
            state.last_error_code = ""
            state.limit = min(
                max(int(observed_limit), state.recovery_limit, 2, 1),
                DIRECT_EGRESS_HARD_MAX,
            )
            state.recovery_limit = state.limit
            self._persist_direct_health(egress_key, state)
            self._direct_gate_for(egress_key).set_capacity(state.limit)

    @staticmethod
    def memory_percent() -> float | None:
        _available, used_percent = _memory_snapshot()
        return used_percent if used_percent > 0 else None

    def _proxy_gate(self, proxy_ref: str) -> _CountingGate:
        with self._proxy_lock:
            return self._proxy_gates.setdefault(proxy_ref, _CountingGate(1))

    def _provider_gate(self, provider: str) -> _CountingGate:
        key = provider or "default"
        with self._provider_lock:
            return self._provider_gates.setdefault(key, _CountingGate(5))

    @contextmanager
    def slot(
        self,
        *,
        mode: RegistrationMode,
        proxy_ref: str,
        mail_provider: str,
        timeout_seconds: float,
        cancel_check: Callable[[], bool],
        direct: bool = False,
        direct_capacity: int | None = None,
    ) -> Iterator[None]:
        acquired: list[_CountingGate] = []
        del direct_capacity
        egress_gate = self._direct_gate_for(proxy_ref) if direct else self._proxy_gate(proxy_ref)
        gates = (self._mode_gates[mode], egress_gate, self._provider_gate(mail_provider))
        try:
            for gate in gates:
                if not gate.acquire(timeout=timeout_seconds, cancel_check=cancel_check):
                    raise CapacityTimeout("registration capacity wait timed out")
                acquired.append(gate)
            yield
        finally:
            for gate in reversed(acquired):
                gate.release()

    def pace(
        self,
        key: str,
        gap_seconds: float,
        *,
        include_finish_cooldown: bool = True,
    ) -> float:
        """Reserve a start time after both start-gap and post-finish cooldown."""
        now = time.monotonic()
        with self._pace_lock:
            if float(gap_seconds) <= 0 and not include_finish_cooldown:
                self._next_start[key] = now
                return 0.0
            finish_cooldown = self._finish_cooldown.get(key, 0.0) if include_finish_cooldown else 0.0
            scheduled = max(now, self._next_start.get(key, now), finish_cooldown)
            self._next_start[key] = scheduled + max(float(gap_seconds), 0.0)
        return max(0.0, scheduled - now)

    @staticmethod
    def outcome_cooldown_seconds(
        *,
        direct: bool,
        success: bool,
        error_code: RegistrationErrorCode | None,
    ) -> int:
        """Return the only pacing cooldown policy used by registration work."""
        if success:
            # Preserve a small same-egress stagger without turning healthy
            # multi-slot capacity into a serial one-minute queue.
            return 8 if direct else 10
        if error_code in {
            RegistrationErrorCode.CF_CHALLENGE,
            RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE,
            RegistrationErrorCode.HTTP_RATE_LIMIT,
        }:
            return 600 if direct else 300
        if error_code in {
            RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
            RegistrationErrorCode.AUTH_SESSION_DESYNC,
        }:
            return 120
        if error_code in {
            RegistrationErrorCode.NET_DNS,
            RegistrationErrorCode.NET_PROXY,
            RegistrationErrorCode.NET_TLS,
        }:
            return 30
        return 0

    def record_outcome(
        self,
        *,
        mode: RegistrationMode,
        egress_ref: str,
        direct: bool,
        success: bool,
        error_code: RegistrationErrorCode | None,
        duration_seconds: float,
        memory_percent: float | None = None,
        pace_ref: str = "",
        apply_cooldown: bool = True,
    ) -> int:
        self.adaptive.record(
            mode,
            success=success,
            error_code=error_code,
            duration_seconds=duration_seconds,
            memory_percent=memory_percent,
            egress_ref=egress_ref,
            direct=direct,
        )
        if direct:
            health = self.adaptive.snapshot(mode, egress_ref, direct=True)
            self._update_direct_health(
                mode,
                egress_ref,
                int(health.get("healthy_concurrency") or 1),
            )
            if success:
                self._close_direct_probe(
                    egress_ref,
                    int(health.get("healthy_concurrency") or 1),
                )
            if error_code in AdaptiveModeController._IMMEDIATE_CODES:
                self._apply_direct_pressure(egress_ref, error_code)
        cooldown = self.outcome_cooldown_seconds(
            direct=direct,
            success=success,
            error_code=error_code,
        )
        if not apply_cooldown:
            return 0
        if cooldown:
            with self._pace_lock:
                cooldown_key = str(pace_ref or egress_ref)
                self._finish_cooldown[cooldown_key] = max(
                    self._finish_cooldown.get(cooldown_key, 0.0),
                    time.monotonic() + cooldown,
                )
        return cooldown

    def can_refill(self, mode: RegistrationMode, egress_ref: str, *, direct: bool) -> bool:
        if direct:
            shared = self._direct_snapshot(egress_ref)
            if int(shared["cooldown_seconds"] or 0) > 0:
                return False
            if str(shared.get("egress_state") or "") == "half_open":
                return True
        return self.adaptive.cooldown_remaining(mode, egress_ref, direct=direct) <= 0

    def health(self, mode: RegistrationMode, egress_ref: str, *, direct: bool) -> dict[str, object]:
        health = self.adaptive.snapshot(mode, egress_ref, direct=direct)
        if not direct:
            return health
        shared = self._direct_snapshot(egress_ref)
        shared_cooldown = int(shared.get("cooldown_seconds") or 0)
        shared_state = str(shared.get("egress_state") or "")
        if shared_state == "half_open":
            health["healthy_concurrency"] = 1
            health["egress_state"] = "half_open"
            health["cooldown_seconds"] = 0
            health["last_error_code"] = ""
        elif shared_cooldown > 0:
            health["healthy_concurrency"] = 1
            health["egress_state"] = str(shared.get("egress_state") or "open")
            health["cooldown_seconds"] = shared_cooldown
            health["last_error_code"] = str(shared.get("last_error_code") or "")
        else:
            health["healthy_concurrency"] = min(
                int(health.get("healthy_concurrency") or 1),
                int(shared.get("healthy_concurrency") or 1),
            )
            if str(shared.get("egress_state") or "") == "half_open":
                health["egress_state"] = "half_open"
            elif str(shared.get("egress_state") or "") == "closed" and not shared_cooldown:
                health["egress_state"] = "closed"
                health["cooldown_seconds"] = 0
        return health

    def record_identity_mismatch(
        self,
        *,
        mode: RegistrationMode,
        egress_ref: str,
        mailbox_domain_ref: str,
        direct: bool,
    ) -> int:
        """Persist the egress/domain mismatch streak and return its cooldown."""
        now = time.time()
        combo_ref = f"{egress_ref}:{mailbox_domain_ref or 'unknown-domain'}"
        with self._identity_lock:
            window: list[dict[str, object]] = []
            try:
                from infrastructure.registration_repository import registration_resource_health

                stored = registration_resource_health.get(
                    mode="identity_combo",
                    egress_ref=combo_ref,
                ) or {}
                window = [
                    item
                    for item in list(stored.get("window") or [])
                    if now - float(item.get("at") or 0) <= 900
                ]
            except Exception:
                stored = {}
            window.append({"at": now, "error_code": RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH.value})
            cooldown = 900 if len(window[-5:]) >= 2 else 120
            try:
                registration_resource_health.put(
                    mode="identity_combo",
                    egress_ref=combo_ref,
                    state="open",
                    healthy_concurrency=1,
                    success_streak=0,
                    failure_streak=len(window[-5:]),
                    last_error_code=RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH.value,
                    cooldown_until=datetime.now(timezone.utc) + timedelta(seconds=cooldown),
                    window=window[-5:],
                )
            except Exception:
                pass
        self.adaptive.extend_cooldown(
            mode,
            egress_ref,
            direct=direct,
            seconds=cooldown,
            error_code=RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
        )
        if direct:
            self._apply_direct_pressure(
                egress_ref,
                RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
                cooldown_seconds=cooldown,
            )
        with self._pace_lock:
            self._finish_cooldown[egress_ref] = max(
                self._finish_cooldown.get(egress_ref, 0.0),
                time.monotonic() + cooldown,
            )
        return cooldown


registration_capacity = RegistrationCapacityManager()
