"""Production composition root shared by CLI commands and the future Web API."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from types import TracebackType

from kubelab.config import TrustedContext, load_config, resolve_kubeconfig_path
from kubelab.context_trust import build_context_trust_service
from kubelab.database import Database
from kubelab.doctor import build_doctor_service
from kubelab.guided_learning import EnvironmentReadinessService
from kubelab.kubernetes_gateway import KubernetesGateway
from kubelab.lab_manager import ClusterGateway, LabManager
from kubelab.lab_registry import LabRegistry
from kubelab.learning_paths import LearningPathRegistry
from kubelab.operation_lock import OperationLock
from kubelab.package_catalog import PackageCatalogRegistry
from kubelab.package_manager import (
    PackageManager,
    PackageManagerError,
    is_package_platform_supported,
)
from kubelab.validation_engine import ValidationEngine


class ApplicationRuntime:
    """Own process-local resources used by one CLI invocation."""

    def __init__(
        self,
        database: Database,
        manager: LabManager,
        kubeconfig_path: Path,
        readiness: EnvironmentReadinessService | None = None,
        packages: PackageManager | None = None,
    ) -> None:
        self.database = database
        self.manager = manager
        self.kubeconfig_path = kubeconfig_path
        self.readiness = readiness
        self.packages = packages

    def close(self) -> None:
        self.database.dispose()

    def __enter__(self) -> ApplicationRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class PackageApplicationRuntime:
    """Own only persistence and PackageManager for local inventory CLI commands."""

    def __init__(self, database: Database, packages: PackageManager) -> None:
        self.database = database
        self.packages = packages

    def close(self) -> None:
        self.database.dispose()

    def __enter__(self) -> PackageApplicationRuntime:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class RuntimeEnvironmentError(RuntimeError):
    """Reject experiment commands outside the supported WSL2 process boundary."""

    code = "RUNTIME_PLATFORM_UNSUPPORTED"
    retryable = False


def build_application_runtime() -> ApplicationRuntime:  # pragma: no cover - composition root
    """Build the WSL production runtime using explicit kubeconfig and Context identity."""
    if platform.system() != "Linux" or not os.environ.get("WSL_DISTRO_NAME"):
        raise RuntimeEnvironmentError("KubeLab experiment commands must run inside WSL2 Ubuntu.")
    local_config = load_config()
    kubeconfig_path = resolve_kubeconfig_path(local_config)
    database = Database()
    try:
        database.initialize()
        builtin_registry = LabRegistry()
        operation_lock = OperationLock(database.lock_path)
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=database.path.parent / "packages",
            builtin_registry=builtin_registry,
            operation_lock=operation_lock,
        )
        registry = PackageCatalogRegistry(
            builtin_registry=builtin_registry,
            unit_of_work=database.unit_of_work,
            state_root=database.path.parent / "packages",
        )
        validation = ValidationEngine(database.unit_of_work)
        context_trust = build_context_trust_service()
        readiness = EnvironmentReadinessService(
            doctor=build_doctor_service(),
            context_trust=context_trust,
            unit_of_work=database.unit_of_work,
        )

        def gateway_factory(trusted: TrustedContext, context_fingerprint: str) -> ClusterGateway:
            return KubernetesGateway.from_kubeconfig(
                kubeconfig_path=kubeconfig_path,
                context_name=trusted.name,
                context_fingerprint=context_fingerprint,
            )

        manager = LabManager(
            registry=registry,
            unit_of_work=database.unit_of_work,
            operation_lock=operation_lock,
            context_trust=context_trust,
            gateway_factory=gateway_factory,
            validation=validation,
            readiness=readiness,
            learning_paths=LearningPathRegistry(),
            session_completed_hook=packages.sweep_pending_removals,
        )
        return ApplicationRuntime(database, manager, kubeconfig_path, readiness, packages)
    except Exception:
        database.dispose()
        raise


def build_package_runtime() -> PackageApplicationRuntime:  # pragma: no cover - composition root
    """Build package inventory services without kubeconfig, Context, or Kubernetes clients."""
    if not is_package_platform_supported():
        raise PackageManagerError(
            "PACKAGE_PLATFORM_UNSUPPORTED",
            "Package inventory changes are supported only inside WSL2 Ubuntu.",
            exit_code=5,
        )
    database = Database()
    try:
        database.initialize()
        operation_lock = OperationLock(database.lock_path)
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=database.path.parent / "packages",
            builtin_registry=LabRegistry(),
            operation_lock=operation_lock,
        )
        return PackageApplicationRuntime(database, packages)
    except Exception:
        database.dispose()
        raise


__all__ = [
    "ApplicationRuntime",
    "PackageApplicationRuntime",
    "RuntimeEnvironmentError",
    "build_application_runtime",
    "build_package_runtime",
]
