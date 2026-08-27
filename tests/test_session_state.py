"""Exhaustive lifecycle tests for the pure session state machine."""

from __future__ import annotations

import pytest

from kubelab.session_state import (
    ACTIVE_SESSION_STATUSES,
    InvalidSessionTransition,
    NewLabSession,
    SessionStateMachine,
    SessionStatus,
    VerificationPurpose,
    VerificationRunInput,
)

EXPECTED_TRANSITIONS = {
    SessionStatus.PROVISIONING: {
        SessionStatus.READY,
        SessionStatus.ERROR,
        SessionStatus.CLEANING,
    },
    SessionStatus.READY: {
        SessionStatus.IN_PROGRESS,
        SessionStatus.RESETTING,
        SessionStatus.CLEANING,
        SessionStatus.ERROR,
    },
    SessionStatus.IN_PROGRESS: {
        SessionStatus.PASSED,
        SessionStatus.RESETTING,
        SessionStatus.CLEANING,
        SessionStatus.ERROR,
    },
    SessionStatus.PASSED: {
        SessionStatus.RESETTING,
        SessionStatus.CLEANING,
        SessionStatus.ERROR,
    },
    SessionStatus.RESETTING: {
        SessionStatus.READY,
        SessionStatus.ERROR,
        SessionStatus.CLEANING,
    },
    SessionStatus.CLEANING: {SessionStatus.COMPLETED, SessionStatus.ERROR},
    SessionStatus.ERROR: {SessionStatus.RESETTING, SessionStatus.CLEANING},
    SessionStatus.COMPLETED: set(),
}


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in EXPECTED_TRANSITIONS.items() for target in targets],
)
def test_every_documented_transition_is_allowed(
    current: SessionStatus, target: SessionStatus
) -> None:
    assert SessionStateMachine.can_transition(current, target) is True
    SessionStateMachine.require_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in SessionStatus
        for target in SessionStatus
        if target not in EXPECTED_TRANSITIONS[current]
    ],
)
def test_every_other_transition_is_rejected(current: SessionStatus, target: SessionStatus) -> None:
    with pytest.raises(InvalidSessionTransition) as caught:
        SessionStateMachine.require_transition(current, target)

    assert caught.value.code == "INVALID_SESSION_STATE"
    assert caught.value.current is current
    assert caught.value.target is target


def test_only_completed_is_not_active() -> None:
    assert ACTIVE_SESSION_STATUSES == set(SessionStatus) - {SessionStatus.COMPLETED}


@pytest.mark.parametrize("invalid_id", ["not-a-uuid", "00000000-0000-1000-8000-000000000001"])
def test_persisted_identifiers_must_be_uuid4(invalid_id: str) -> None:
    with pytest.raises(ValueError, match="UUID4"):
        NewLabSession(
            id=invalid_id,
            lab_id="test-lab",
            namespace="kubelab-test-lab",
            context_name="minikube",
            context_fingerprint="a" * 64,
        )
    with pytest.raises(ValueError, match="UUID4"):
        VerificationRunInput(
            id=invalid_id,
            session_id="session",
            purpose=VerificationPurpose.MANUAL,
            status="passed",
            reset_sequence=0,
            duration_ms=1,
            results=(
                {
                    "check_id": "check",
                    "check_type": "resource_exists",
                    "status": "passed",
                    "expected": {},
                    "actual": {},
                    "message": "passed",
                    "retryable": False,
                    "duration_ms": 1,
                },
            ),
        )
