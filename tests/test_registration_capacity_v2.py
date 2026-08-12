from __future__ import annotations

import threading
import time

import pytest

from domain.registration_runtime import (
    RegistrationErrorCode,
    RegistrationMode,
    classify_registration_error,
)
from services.registration_capacity import (
    AdaptiveModeController,
    CapacityTimeout,
    DIRECT_EGRESS_HARD_MAX,
    MODE_CAPACITY,
    RegistrationCapacityManager,
)


def test_shared_direct_health_survives_manager_restart():
    from infrastructure.registration_repository import registration_resource_health

    egress_ref = "direct:persisted-shared-capacity"
    key = f"shared:{egress_ref}"
    registration_resource_health.put(
        mode="direct_egress",
        egress_ref=key,
        state="closed",
        healthy_concurrency=2,
        success_streak=0,
        failure_streak=0,
        last_error_code="",
        cooldown_until=None,
        window=[],
    )

    manager = RegistrationCapacityManager()

    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        2,
        direct=True,
        count=4,
        egress_ref=egress_ref,
    ) == 2
    assert manager._direct_gate_for(egress_ref).capacity == 2


def test_direct_egress_gates_are_isolated_by_physical_exit():
    manager = RegistrationCapacityManager()
    healthy = "direct:healthy-exit"
    cold = "direct:cold-exit"
    for _ in range(5):
        manager.record_outcome(
            mode=RegistrationMode.PROTOCOL,
            egress_ref=healthy,
            direct=True,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            apply_cooldown=False,
        )

    manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 2, direct=True, count=4, egress_ref=healthy
    )
    manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 2, direct=True, count=4, egress_ref=cold
    )

    assert manager._direct_gate_for(healthy).capacity == 2
    assert manager._direct_gate_for(cold).capacity == 1


def test_shared_direct_circuit_breaker_survives_manager_restart():
    egress_ref = "direct:persisted-circuit"
    first = RegistrationCapacityManager()
    first.record_outcome(
        mode=RegistrationMode.PROTOCOL,
        egress_ref=egress_ref,
        direct=True,
        success=False,
        error_code=RegistrationErrorCode.CF_CHALLENGE,
        duration_seconds=1,
        apply_cooldown=False,
    )

    restarted = RegistrationCapacityManager()
    health = restarted.health(RegistrationMode.HEADLESS, egress_ref, direct=True)

    assert health["egress_state"] == "open"
    assert int(health["cooldown_seconds"] or 0) >= 595
    assert restarted.can_refill(RegistrationMode.HEADLESS, egress_ref, direct=True) is False
    assert restarted._direct_gate_for(egress_ref).capacity == 1


def test_effective_concurrency_obeys_mode_direct_and_resource_limits():
    manager = RegistrationCapacityManager()

    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        20,
        direct=True,
        count=50,
    ) == 1
    assert manager.effective_concurrency(
        RegistrationMode.HEADLESS,
        8,
        direct=True,
        count=50,
    ) == 1
    assert manager.effective_concurrency(
        RegistrationMode.HEADED,
        8,
        direct=False,
        count=50,
        proxy_count=10,
    ) == MODE_CAPACITY[RegistrationMode.HEADED].default
    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        20,
        direct=False,
        count=50,
        proxy_count=3,
    ) == 1


def test_same_proxy_slot_is_exclusive_across_concurrent_attempts():
    manager = RegistrationCapacityManager()
    entered = threading.Event()
    release = threading.Event()
    result: list[str] = []

    def holder():
        with manager.slot(
            mode=RegistrationMode.PROTOCOL,
            proxy_ref="sha256:shared",
            mail_provider="mail-a",
            timeout_seconds=1,
            cancel_check=lambda: False,
        ):
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)

    try:
        with manager.slot(
            mode=RegistrationMode.PROTOCOL,
            proxy_ref="sha256:shared",
            mail_provider="mail-a",
            timeout_seconds=0.05,
            cancel_check=lambda: False,
        ):
            result.append("entered")
    except CapacityTimeout as exc:
        result.append(classify_registration_error(exc).value)
    finally:
        release.set()
        thread.join(timeout=1)

    assert result == [RegistrationErrorCode.RESOURCE_EXHAUSTED.value]
    assert thread.is_alive() is False


def test_capacity_wait_honors_cancellation_without_leaking_gate():
    manager = RegistrationCapacityManager()

    with pytest.raises(CapacityTimeout):
        with manager.slot(
            mode=RegistrationMode.HEADLESS,
            proxy_ref="direct",
            mail_provider="mail-a",
            timeout_seconds=1,
            cancel_check=lambda: True,
        ):
            pass

    with manager.slot(
        mode=RegistrationMode.HEADLESS,
        proxy_ref="direct",
        mail_provider="mail-a",
        timeout_seconds=1,
        cancel_check=lambda: False,
    ):
        assert True


def test_aimd_halves_on_pressure_and_grows_one_step_after_success_window():
    pressure = AdaptiveModeController()
    initial = pressure.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False, egress_ref="pressure-egress"
    )
    for _ in range(4):
        pressure.record(
            RegistrationMode.PROTOCOL,
            success=True,
            error_code=None,
            duration_seconds=1,
            egress_ref="pressure-egress",
        )
    pressure.record(
        RegistrationMode.PROTOCOL,
        success=False,
        error_code=RegistrationErrorCode.HTTP_RATE_LIMIT,
        duration_seconds=1,
        egress_ref="pressure-egress",
    )
    assert pressure.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False, egress_ref="pressure-egress"
    ) == max(
        1, initial // 2
    )

    recovery = AdaptiveModeController()
    before = recovery.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False, egress_ref="recovery-egress"
    )
    for _ in range(5):
        recovery.record(
            RegistrationMode.PROTOCOL,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            egress_ref="recovery-egress",
        )
    assert recovery.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False, egress_ref="recovery-egress"
    ) == min(
        before + 1,
        MODE_CAPACITY[RegistrationMode.PROTOCOL].maximum,
    )

    memory_limited = AdaptiveModeController()
    for _ in range(10):
        memory_limited.record(
            RegistrationMode.PROTOCOL,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=75,
            egress_ref="memory-egress",
        )
    assert memory_limited.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False, egress_ref="memory-egress"
    ) == MODE_CAPACITY[RegistrationMode.PROTOCOL].default


@pytest.mark.parametrize(
    "error_code",
    [
        RegistrationErrorCode.SENTINEL_SDK_DRIFT,
        RegistrationErrorCode.SENTINEL_PROOF,
        RegistrationErrorCode.AUTH_INVALID_STEP,
        RegistrationErrorCode.BROWSER_STATE_UNKNOWN,
        RegistrationErrorCode.OTP_TIMEOUT,
    ],
)
def test_aimd_treats_runtime_and_state_failures_as_pressure(error_code):
    controller = AdaptiveModeController()
    initial = controller.effective_limit(RegistrationMode.PROTOCOL, 20, direct=False)
    for _ in range(4):
        controller.record(
            RegistrationMode.PROTOCOL,
            success=True,
            error_code=None,
            duration_seconds=1,
        )

    controller.record(
        RegistrationMode.PROTOCOL,
        success=False,
        error_code=error_code,
        duration_seconds=1,
    )

    assert controller.effective_limit(
        RegistrationMode.PROTOCOL, 20, direct=False
    ) == max(1, initial // 2)


def test_pacing_reserves_time_without_sleeping_inside_manager():
    manager = RegistrationCapacityManager()

    assert manager.pace("proxy-a", 0.05) == 0
    wait = manager.pace("proxy-a", 0.05)

    assert 0 < wait <= 0.05
    assert manager.pace("proxy-b", 0.05) == 0


def test_direct_exit_is_shared_across_modes():
    manager = RegistrationCapacityManager()
    entered = threading.Event()
    release = threading.Event()

    def protocol_holder():
        with manager.slot(
            mode=RegistrationMode.PROTOCOL,
            proxy_ref="direct:shared-egress",
            mail_provider="mail-a",
            timeout_seconds=1,
            cancel_check=lambda: False,
            direct=True,
            direct_capacity=1,
        ):
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=protocol_holder)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(CapacityTimeout):
            with manager.slot(
                mode=RegistrationMode.HEADLESS,
                proxy_ref="direct:shared-egress",
                mail_provider="mail-b",
                timeout_seconds=0.05,
                cancel_check=lambda: False,
                direct=True,
                direct_capacity=1,
            ):
                pass
    finally:
        release.set()
        thread.join(timeout=1)

    assert thread.is_alive() is False


def test_task_requested_capacity_cannot_raise_global_direct_gate():
    manager = RegistrationCapacityManager()
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with manager.slot(
            mode=RegistrationMode.PROTOCOL,
            proxy_ref="direct:global-health",
            mail_provider="mail-a",
            timeout_seconds=1,
            cancel_check=lambda: False,
            direct=True,
            direct_capacity=20,
        ):
            entered.set()
            release.wait(1)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(CapacityTimeout):
            with manager.slot(
                mode=RegistrationMode.HEADLESS,
                proxy_ref="direct:global-health",
                mail_provider="mail-b",
                timeout_seconds=0.05,
                cancel_check=lambda: False,
                direct=True,
                direct_capacity=20,
            ):
                pass
    finally:
        release.set()
        thread.join(timeout=1)

    assert manager._direct_gate_for("direct:global-health").capacity == 1


def test_user_request_one_does_not_lower_recorded_direct_health():
    manager = RegistrationCapacityManager()
    egress_ref = "direct:healthy"
    for _ in range(5):
        manager.record_outcome(
            mode=RegistrationMode.PROTOCOL,
            egress_ref=egress_ref,
            direct=True,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            apply_cooldown=False,
        )

    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        1,
        direct=True,
        count=10,
        egress_ref=egress_ref,
    ) == 1
    assert manager._direct_gate_for(egress_ref).capacity == 2


def test_effective_concurrency_is_capped_by_cross_mode_global_direct_gate():
    class FakeAdaptive:
        @staticmethod
        def effective_limit(mode, requested, **_kwargs):
            del requested
            return 4 if mode is RegistrationMode.PROTOCOL else 1

        @staticmethod
        def snapshot(mode, _egress_ref, **_kwargs):
            return {
                "healthy_concurrency": 4 if mode is RegistrationMode.PROTOCOL else 1,
                "cooldown_seconds": 0,
                "egress_state": "closed",
            }

    manager = RegistrationCapacityManager()
    manager.adaptive = FakeAdaptive()

    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        5,
        direct=True,
        count=10,
        egress_ref="direct:shared",
    ) == min(4, DIRECT_EGRESS_HARD_MAX)
    assert manager.effective_concurrency(
        RegistrationMode.HEADLESS,
        1,
        direct=True,
        count=10,
        egress_ref="direct:shared",
    ) == 1
    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL,
        5,
        direct=True,
        count=10,
        egress_ref="direct:shared",
    ) == min(4, DIRECT_EGRESS_HARD_MAX)
    assert manager._direct_gate_for("direct:shared").capacity == min(4, DIRECT_EGRESS_HARD_MAX)


def test_healthy_low_request_mode_does_not_lower_shared_direct_egress_capacity():
    class FakeAdaptive:
        @staticmethod
        def effective_limit(mode, requested, **_kwargs):
            del requested
            return 3 if mode is RegistrationMode.PROTOCOL else 1

        @staticmethod
        def snapshot(mode, _egress_ref, **_kwargs):
            return {
                "healthy_concurrency": 3 if mode is RegistrationMode.PROTOCOL else 1,
                "cooldown_seconds": 0,
                "egress_state": "closed",
                "last_error_code": "",
            }

    manager = RegistrationCapacityManager()
    manager.adaptive = FakeAdaptive()
    egress_ref = "direct:healthy-mode-isolation"

    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 3, direct=True, count=10, egress_ref=egress_ref
    ) == min(3, DIRECT_EGRESS_HARD_MAX)
    assert manager.effective_concurrency(
        RegistrationMode.HEADLESS, 1, direct=True, count=10, egress_ref=egress_ref
    ) == 1
    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 3, direct=True, count=10, egress_ref=egress_ref
    ) == min(3, DIRECT_EGRESS_HARD_MAX)
    assert manager._direct_gate_for(egress_ref).capacity == min(3, DIRECT_EGRESS_HARD_MAX)


@pytest.mark.parametrize(
    "error_code",
    [
        RegistrationErrorCode.CF_CHALLENGE,
        RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE,
        RegistrationErrorCode.HTTP_RATE_LIMIT,
        RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH,
    ],
)
def test_direct_pressure_from_one_mode_opens_shared_gate_for_all_modes(error_code):
    manager = RegistrationCapacityManager()
    egress_ref = f"direct:shared-pressure-{error_code.value}"

    for _ in range(5):
        manager.record_outcome(
            mode=RegistrationMode.PROTOCOL,
            egress_ref=egress_ref,
            direct=True,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            apply_cooldown=False,
        )
    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 3, direct=True, count=10, egress_ref=egress_ref
    ) == min(2, DIRECT_EGRESS_HARD_MAX)

    manager.record_outcome(
        mode=RegistrationMode.HEADLESS,
        egress_ref=egress_ref,
        direct=True,
        success=False,
        error_code=error_code,
        duration_seconds=1,
        apply_cooldown=False,
    )

    shared = manager.health(RegistrationMode.PROTOCOL, egress_ref, direct=True)
    assert manager.effective_concurrency(
        RegistrationMode.PROTOCOL, 3, direct=True, count=10, egress_ref=egress_ref
    ) == 1
    assert manager._direct_gate_for(egress_ref).capacity == 1
    assert shared["egress_state"] == "open"
    assert int(shared["cooldown_seconds"] or 0) > 0


def test_direct_success_cooldown_is_short_stagger_not_serial_minute_barrier():
    manager = RegistrationCapacityManager()
    manager.record_outcome(
        mode=RegistrationMode.PROTOCOL,
        egress_ref="direct:short-success-cooldown",
        direct=True,
        success=True,
        error_code=None,
        duration_seconds=1,
        pace_ref="direct:short-success-cooldown",
    )

    wait = manager.pace("direct:short-success-cooldown", 0.0)
    assert 0 < wait <= 8


def test_half_open_direct_probe_restores_shared_capacity_after_success():
    manager = RegistrationCapacityManager()
    egress_ref = "direct:half-open-recovery"
    for _ in range(5):
        manager.record_outcome(
            mode=RegistrationMode.PROTOCOL,
            egress_ref=egress_ref,
            direct=True,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            apply_cooldown=False,
        )
    manager.adaptive.extend_cooldown(
        RegistrationMode.PROTOCOL,
        egress_ref,
        direct=True,
        seconds=1,
        error_code=RegistrationErrorCode.CF_CHALLENGE,
    )
    with manager._direct_health_lock:
        manager._direct_health[egress_ref].cooldown_until = time.monotonic() - 1

    assert manager.health(RegistrationMode.PROTOCOL, egress_ref, direct=True)["egress_state"] == "half_open"
    manager.record_outcome(
        mode=RegistrationMode.PROTOCOL,
        egress_ref=egress_ref,
        direct=True,
        success=True,
        error_code=None,
        duration_seconds=1,
        memory_percent=20,
        apply_cooldown=False,
    )
    assert manager.health(RegistrationMode.PROTOCOL, egress_ref, direct=True)["egress_state"] == "closed"
    assert manager._direct_gate_for(egress_ref).capacity >= 2


def test_healthy_direct_exit_allows_two_live_slots_but_never_a_third():
    manager = RegistrationCapacityManager()
    egress_ref = "direct:two-live-slots"
    for _ in range(5):
        manager.record_outcome(
            mode=RegistrationMode.PROTOCOL,
            egress_ref=egress_ref,
            direct=True,
            success=True,
            error_code=None,
            duration_seconds=1,
            memory_percent=20,
            apply_cooldown=False,
        )

    entered = threading.Event()
    release = threading.Event()

    def hold_slot(mode):
        with manager.slot(
            mode=mode,
            proxy_ref=egress_ref,
            mail_provider=f"mail-{mode.value}",
            timeout_seconds=1,
            cancel_check=lambda: False,
            direct=True,
        ):
            entered.set()
            release.wait(1)

    first = threading.Thread(target=hold_slot, args=(RegistrationMode.PROTOCOL,))
    second = threading.Thread(target=hold_slot, args=(RegistrationMode.HEADLESS,))
    first.start()
    second.start()
    assert entered.wait(1)
    deadline = time.monotonic() + 1
    gate = manager._direct_gate_for(egress_ref)
    while gate.active < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.active == 2
    with pytest.raises(CapacityTimeout):
        with manager.slot(
            mode=RegistrationMode.HEADED,
            proxy_ref=egress_ref,
            mail_provider="mail-headed",
            timeout_seconds=0.05,
            cancel_check=lambda: False,
            direct=True,
        ):
            pass
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive()
    assert not second.is_alive()


def test_identity_mismatch_combo_escalates_from_pause_to_quarantine():
    manager = RegistrationCapacityManager()
    first = manager.record_identity_mismatch(
        mode=RegistrationMode.HEADLESS,
        egress_ref="direct:fixture",
        mailbox_domain_ref="sha256:mail-domain",
        direct=True,
    )
    second = manager.record_identity_mismatch(
        mode=RegistrationMode.HEADLESS,
        egress_ref="direct:fixture",
        mailbox_domain_ref="sha256:mail-domain",
        direct=True,
    )

    health = manager.health(
        RegistrationMode.HEADLESS,
        "direct:fixture",
        direct=True,
    )
    assert first == 120
    assert second == 900
    assert health["healthy_concurrency"] == 1
    assert health["egress_state"] == "open"
    assert int(health["cooldown_seconds"]) >= 895
