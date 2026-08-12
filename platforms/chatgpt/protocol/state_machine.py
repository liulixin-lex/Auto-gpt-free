from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from domain.registration_runtime import RegistrationStage


class InvalidProtocolTransition(RuntimeError):
    pass


_ALLOWED: dict[RegistrationStage, set[RegistrationStage]] = {
    RegistrationStage.PREPARE: {RegistrationStage.PREFLIGHT},
    RegistrationStage.PREFLIGHT: {RegistrationStage.AUTH_BEGIN},
    RegistrationStage.AUTH_BEGIN: {RegistrationStage.EMAIL_SUBMIT},
    RegistrationStage.EMAIL_SUBMIT: {RegistrationStage.OTP_TRIGGER},
    RegistrationStage.OTP_TRIGGER: {RegistrationStage.OTP_WAIT},
    RegistrationStage.OTP_WAIT: {RegistrationStage.OTP_SUBMIT},
    RegistrationStage.OTP_SUBMIT: {
        RegistrationStage.PROFILE_CREATE,
        RegistrationStage.CALLBACK,
        RegistrationStage.SESSION_VALIDATE,
    },
    RegistrationStage.PROFILE_CREATE: {
        RegistrationStage.CALLBACK,
        RegistrationStage.SESSION_VALIDATE,
    },
    RegistrationStage.CALLBACK: {RegistrationStage.SESSION_VALIDATE},
    RegistrationStage.SESSION_VALIDATE: {RegistrationStage.DONE},
    RegistrationStage.DONE: set(),
}


@dataclass(slots=True)
class ProtocolCheckpoint:
    stage: RegistrationStage
    continue_url: str = ""
    side_effect_committed: bool = False


class ProtocolStateMachine:
    def __init__(
        self,
        *,
        emit: Callable[[RegistrationStage, str, str], None] | None = None,
    ) -> None:
        self.checkpoint = ProtocolCheckpoint(RegistrationStage.PREPARE)
        self._emit = emit or (lambda _stage, _message, _action: None)

    @property
    def stage(self) -> RegistrationStage:
        return self.checkpoint.stage

    def transition(
        self,
        stage: RegistrationStage,
        message: str,
        *,
        action: str = "enter",
        continue_url: str = "",
        side_effect_committed: bool | None = None,
    ) -> None:
        current = self.checkpoint.stage
        if stage != current and stage not in _ALLOWED.get(current, set()):
            raise InvalidProtocolTransition(f"invalid protocol transition: {current.value} -> {stage.value}")
        self.checkpoint.stage = stage
        if continue_url:
            self.checkpoint.continue_url = continue_url
        if side_effect_committed is not None:
            self.checkpoint.side_effect_committed = bool(side_effect_committed)
        self._emit(stage, message, action)

    def recover_to_session(self, message: str) -> None:
        if self.stage not in {
            RegistrationStage.OTP_SUBMIT,
            RegistrationStage.PROFILE_CREATE,
            RegistrationStage.CALLBACK,
        }:
            raise InvalidProtocolTransition(f"session recovery is invalid at {self.stage.value}")
        self.transition(RegistrationStage.SESSION_VALIDATE, message, action="recover")
