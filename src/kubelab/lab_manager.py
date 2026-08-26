"""Application service coordinating KubeLab session lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from kubelab.config import TrustedContext
from kubelab.context_trust import ContextTrustService, trusted_context_fingerprint
from kubelab.kubernetes_gateway import (
    NamespaceDeleteResult,
    SessionScope,
)
from kubelab.lab_registry import LabRegistry, LoadedLab, RegistryError
from kubelab.operation_lock import OperationLock
from kubelab.repositories import (
    ActiveSessionConflict,
    SessionNotFoundError,
    SqlAlchemyUnitOfWork,
)
from kubelab.session_state import (
    LabSessionSnapshot,
    NewLabSession,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
)
from kubelab.validation_engine import (
    InitialContractResult,
    ValidationGateway,
    ValidationRunResult,
)


class ManagerErrorCode(StrEnum):
    LAB_NOT_FOUND = "LAB_NOT_FOUND"
    LAB_INVALID = "LAB_INVALID"
    INITIAL_CONTRACT_FAILED = "INITIAL_CONTRACT_FAILED"
    CONTEXT_DRIFT = "CONTEXT_DRIFT"
    CLUSTER_OPERATION_FAILED = "CLUSTER_OPERATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"


class LabManagerError(RuntimeError):
    """Sanitized application-service error with stable public context."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.context = context or {}


class ManagerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionStatusResult(ManagerModel):
    session: LabSessionSnapshot
    namespace_exists: bool
    namespace_owned: bool


class ValidationService(Protocol):
    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
    ) -> InitialContractResult: ...

    def validate_success_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
        purpose: VerificationPurpose = VerificationPurpose.MANUAL,
    ) -> ValidationRunResult: ...


class ClusterGateway(ValidationGateway, Protocol):
    def create_environment(self, scope: SessionScope) -> None: ...

    def apply_lab(self, scope: SessionScope, loaded: LoadedLab, registry: LabRegistry) -> None: ...

    def namespace_exists(self, scope: SessionScope) -> bool: ...

    def assert_namespace_owned(self, scope: SessionScope) -> None: ...

    def delete_environment(
        self, scope: SessionScope, *, wait_timeout_seconds: float = 120
    ) -> NamespaceDeleteResult: ...

    def close(self) -> None: ...


class GatewayFactory(Protocol):
    def __call__(self, trusted: TrustedContext, context_fingerprint: str) -> ClusterGateway: ...


class LabManager:
    """Coordinate short transactions around potentially slow cluster operations."""

    def __init__(
        self,
        *,
        registry: LabRegistry,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
        operation_lock: OperationLock,
        context_trust: ContextTrustService,
        gateway_factory: GatewayFactory,
        validation: ValidationService,
    ) -> None:
        self._registry = registry
        self._unit_of_work = unit_of_work
        self._operation_lock = operation_lock
        self._context_trust = context_trust
        self._gateway_factory = gateway_factory
        self._validation = validation

    def start(self, lab_id: str) -> LabSessionSnapshot:
        """Provision one lab and prove its initial fault contract before returning ready."""
        with self._operation_lock:
            lab = self._require_lab(lab_id)
            trusted = self._context_trust.assert_trusted_context()
            fingerprint = trusted_context_fingerprint(trusted)
            with self._unit_of_work() as uow:
                active = uow.sessions.get_active()
                if active is not None:
                    raise ActiveSessionConflict(active)
                session = uow.sessions.create(
                    NewLabSession(
                        id=str(uuid4()),
                        lab_id=lab_id,
                        namespace=lab.definition.environment.namespace,
                        context_name=trusted.name,
                        context_fingerprint=fingerprint,
                    )
                )
                uow.commit()

            scope = self._scope(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                gateway.create_environment(scope)
                gateway.apply_lab(scope, lab, self._registry)
                result = self._validation.validate_initial_contract(
                    scope, lab, gateway, reset_sequence=0
                )
                if result.status is not ValidationStatus.PASSED:
                    raise LabManagerError(
                        result.error_code or ManagerErrorCode.INITIAL_CONTRACT_FAILED,
                        "The lab initial fault contract was not satisfied.",
                        retryable=result.retryable,
                    )
                return self._transition(
                    session.id,
                    SessionStatus.READY,
                    event_type="environment_ready",
                    context={"namespace": session.namespace},
                )
            except Exception as exc:
                self._rollback_failed_environment(session, gateway, exc, operation="start")
                raise self._manager_error(exc, operation="start") from exc
            finally:
                gateway.close()

    def status(self, session_id: str | None = None) -> SessionStatusResult:
        """Reconcile one Session with its owned Namespace without taking over orphans."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status is SessionStatus.COMPLETED:
                return SessionStatusResult(
                    session=session, namespace_exists=False, namespace_owned=False
                )
            trusted, fingerprint = self._trusted_for_session(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                if not gateway.namespace_exists(self._scope(session)):
                    completed = self._complete_removed_environment(session)
                    return SessionStatusResult(
                        session=completed, namespace_exists=False, namespace_owned=False
                    )
                try:
                    gateway.assert_namespace_owned(self._scope(session))
                except Exception as exc:
                    self._mark_error(
                        session,
                        exc,
                        event_type="namespace_identity_mismatch",
                        operation="status",
                    )
                    raise self._manager_error(exc, operation="status") from exc
                if session.status is SessionStatus.READY:
                    session = self._transition(
                        session.id,
                        SessionStatus.IN_PROGRESS,
                        event_type="session_observed",
                    )
                return SessionStatusResult(
                    session=session, namespace_exists=True, namespace_owned=True
                )
            finally:
                gateway.close()

    def reset(self, session_id: str | None = None) -> LabSessionSnapshot:
        """Safely delete and recreate a Session environment while preserving its ID."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status is SessionStatus.COMPLETED:
                raise LabManagerError(
                    "INVALID_SESSION_STATE", "A completed Session cannot be reset."
                )
            lab = self._require_lab(session.lab_id)
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is not SessionStatus.RESETTING:
                session = self._transition(
                    session.id, SessionStatus.RESETTING, event_type="reset_started"
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                gateway.delete_environment(self._scope(session))
                gateway.create_environment(self._scope(session))
                gateway.apply_lab(self._scope(session), lab, self._registry)
                result = self._validation.validate_initial_contract(
                    self._scope(session),
                    lab,
                    gateway,
                    reset_sequence=session.reset_count + 1,
                )
                if result.status is not ValidationStatus.PASSED:
                    raise LabManagerError(
                        result.error_code or ManagerErrorCode.INITIAL_CONTRACT_FAILED,
                        "The reset environment did not satisfy its initial fault contract.",
                        retryable=result.retryable,
                    )
                with self._unit_of_work() as uow:
                    uow.sessions.increment_reset_count(session.id)
                    ready = uow.sessions.transition(
                        session.id,
                        SessionStatus.READY,
                        event_type="reset_completed",
                    )
                    uow.commit()
                    return ready
            except Exception as exc:
                self._rollback_reset(session, gateway, exc)
                raise self._manager_error(exc, operation="reset") from exc
            finally:
                gateway.close()

    def cleanup(self, session_id: str | None = None) -> LabSessionSnapshot:
        """Idempotently clean any active Session through exact Namespace ownership checks."""
        with self._operation_lock:
            session = self._require_session(session_id, allow_completed=True)
            if session.status is SessionStatus.COMPLETED:
                return session
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is not SessionStatus.CLEANING:
                session = self._transition(
                    session.id, SessionStatus.CLEANING, event_type="cleanup_started"
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                result = gateway.delete_environment(self._scope(session))
                return self._transition(
                    session.id,
                    SessionStatus.COMPLETED,
                    event_type="cleanup_completed",
                    context={"already_absent": result.already_absent},
                )
            except Exception as exc:
                self._mark_error(
                    session,
                    exc,
                    event_type="cleanup_failed",
                    operation="cleanup",
                )
                raise self._manager_error(exc, operation="cleanup") from exc
            finally:
                gateway.close()

    def verify(self, session_id: str | None = None) -> ValidationRunResult:
        """Run successChecks and advance an in-progress Session only on success."""
        with self._operation_lock:
            session = self._require_session(session_id, allow_completed=True)
            if session.status not in {
                SessionStatus.READY,
                SessionStatus.IN_PROGRESS,
                SessionStatus.PASSED,
            }:
                raise LabManagerError(
                    "INVALID_SESSION_STATE",
                    "The Session cannot be verified in its current state.",
                )
            lab = self._require_lab(session.lab_id)
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is SessionStatus.READY:
                session = self._transition(
                    session.id,
                    SessionStatus.IN_PROGRESS,
                    event_type="verification_started",
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                result = self._validation.validate_success_contract(
                    self._scope(session),
                    lab,
                    gateway,
                    reset_sequence=session.reset_count,
                    purpose=VerificationPurpose.MANUAL,
                )
                if (
                    result.status is ValidationStatus.PASSED
                    and session.status is SessionStatus.IN_PROGRESS
                ):
                    self._transition(
                        session.id,
                        SessionStatus.PASSED,
                        event_type="success_contract_passed",
                        context={"verification_run_id": result.id},
                    )
                return result
            finally:
                gateway.close()

    def _require_lab(self, lab_id: str) -> LoadedLab:
        snapshot = self._registry.scan()
        match = next((lab for lab in snapshot.labs if lab.definition.metadata.id == lab_id), None)
        if match is not None:
            return match
        related = tuple(error for error in snapshot.errors if error.lab_id == lab_id)
        if related:
            raise LabManagerError(
                ManagerErrorCode.LAB_INVALID,
                "The requested lab is invalid.",
                context={"errors": tuple(_registry_error_context(error) for error in related)},
            )
        raise LabManagerError(ManagerErrorCode.LAB_NOT_FOUND, "The requested lab was not found.")

    def _require_session(
        self, session_id: str | None, *, allow_completed: bool = False
    ) -> LabSessionSnapshot:
        try:
            with self._unit_of_work() as uow:
                session = (
                    uow.sessions.require(session_id)
                    if session_id is not None
                    else uow.sessions.get_active()
                )
                if session is None:
                    raise SessionNotFoundError("There is no active Session.")
                if session.status is SessionStatus.COMPLETED and not allow_completed:
                    raise SessionNotFoundError("There is no active Session.")
                return session
        except SessionNotFoundError as exc:
            raise LabManagerError(
                ManagerErrorCode.SESSION_NOT_FOUND, "The requested Session was not found."
            ) from exc

    def _trusted_for_session(self, session: LabSessionSnapshot) -> tuple[TrustedContext, str]:
        try:
            trusted = self._context_trust.assert_trusted_context()
        except Exception as exc:
            raise LabManagerError(
                ManagerErrorCode.CONTEXT_DRIFT,
                "The current Context no longer matches the Session.",
            ) from exc
        fingerprint = trusted_context_fingerprint(trusted)
        if trusted.name != session.context_name or fingerprint != session.context_fingerprint:
            raise LabManagerError(
                ManagerErrorCode.CONTEXT_DRIFT,
                "The current Context no longer matches the Session.",
            )
        return trusted, fingerprint

    @staticmethod
    def _scope(session: LabSessionSnapshot) -> SessionScope:
        return SessionScope(
            lab_id=session.lab_id,
            session_id=session.id,
            namespace=session.namespace,
            context_fingerprint=session.context_fingerprint,
        )

    def _transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        event_type: str,
        context: dict[str, Any] | None = None,
    ) -> LabSessionSnapshot:
        with self._unit_of_work() as uow:
            result = uow.sessions.transition(
                session_id, target, event_type=event_type, context=context
            )
            uow.commit()
            return result

    def _complete_removed_environment(self, session: LabSessionSnapshot) -> LabSessionSnapshot:
        current = session
        if current.status is not SessionStatus.CLEANING:
            current = self._transition(
                current.id,
                SessionStatus.CLEANING,
                event_type="environment_removed_externally",
            )
        return self._transition(
            current.id,
            SessionStatus.COMPLETED,
            event_type="external_removal_reconciled",
        )

    def _rollback_failed_environment(
        self,
        session: LabSessionSnapshot,
        gateway: ClusterGateway,
        cause: Exception,
        *,
        operation: str,
    ) -> None:
        cleaning = self._transition(
            session.id,
            SessionStatus.CLEANING,
            event_type=f"{operation}_failed",
            context={"error_code": _error_code(cause)},
        )
        try:
            gateway.delete_environment(self._scope(cleaning))
        except Exception as cleanup_error:
            self._mark_error(
                cleaning,
                cleanup_error,
                event_type="rollback_cleanup_failed",
                operation=operation,
            )
            return
        self._transition(
            cleaning.id,
            SessionStatus.COMPLETED,
            event_type="failed_environment_removed",
            context={"error_code": _error_code(cause)},
        )

    def _rollback_reset(
        self, session: LabSessionSnapshot, gateway: ClusterGateway, cause: Exception
    ) -> None:
        cleaning = self._transition(
            session.id,
            SessionStatus.CLEANING,
            event_type="reset_failed",
            context={"error_code": _error_code(cause)},
        )
        try:
            gateway.delete_environment(self._scope(cleaning))
        except Exception as cleanup_error:
            self._mark_error(
                cleaning,
                cleanup_error,
                event_type="reset_cleanup_failed",
                operation="reset",
            )
            return
        self._mark_error(
            cleaning,
            cause,
            event_type="reset_environment_removed",
            operation="reset",
        )

    def _mark_error(
        self,
        session: LabSessionSnapshot,
        cause: Exception,
        *,
        event_type: str,
        operation: str,
    ) -> LabSessionSnapshot:
        if session.status is SessionStatus.ERROR:
            return session
        with self._unit_of_work() as uow:
            failed = uow.sessions.transition(
                session.id,
                SessionStatus.ERROR,
                event_type=event_type,
                error_code=_error_code(cause),
                error_context={"operation": operation},
            )
            uow.commit()
            return failed

    @staticmethod
    def _manager_error(error: Exception, *, operation: str) -> LabManagerError:
        if isinstance(error, LabManagerError):
            return error
        retryable = bool(getattr(error, "retryable", False))
        code = str(getattr(error, "code", ManagerErrorCode.CLUSTER_OPERATION_FAILED))
        return LabManagerError(
            code,
            f"The lab {operation} operation failed.",
            retryable=retryable,
            context={"operation": operation},
        )


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", ManagerErrorCode.CLUSTER_OPERATION_FAILED)
    return str(code)


def _registry_error_context(error: RegistryError) -> dict[str, Any]:
    return {
        "code": error.code.value,
        "lab_path": error.lab_path,
        "field_path": error.field_path,
        "retryable": error.retryable,
    }


__all__ = [
    "ClusterGateway",
    "GatewayFactory",
    "InitialContractResult",
    "LabManager",
    "LabManagerError",
    "ManagerErrorCode",
    "SessionStatusResult",
    "ValidationService",
]
