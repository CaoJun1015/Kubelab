"""Small lifecycle tests for the shared ApplicationRuntime owner."""

from __future__ import annotations

from pathlib import Path

import pytest

import kubelab.runtime as runtime_module
from kubelab.package_manager import PackageManagerError
from kubelab.runtime import ApplicationRuntime, PackageApplicationRuntime


class _Database:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_application_runtime_exposes_services_and_owns_database_lifecycle() -> None:
    database = _Database()
    manager = object()
    readiness = object()
    packages = object()
    runtime = ApplicationRuntime(  # type: ignore[arg-type]
        database,
        manager,
        Path("kubeconfig"),
        readiness,  # type: ignore[arg-type]
        packages,  # type: ignore[arg-type]
    )

    assert runtime.manager is manager
    assert runtime.readiness is readiness
    assert runtime.packages is packages
    assert runtime.kubeconfig_path == Path("kubeconfig")
    with runtime as entered:
        assert entered is runtime
    assert database.disposed


def test_package_runtime_owns_only_package_inventory_and_database() -> None:
    database = _Database()
    packages = object()
    runtime = PackageApplicationRuntime(database, packages)  # type: ignore[arg-type]

    assert runtime.packages is packages
    with runtime as entered:
        assert entered is runtime
    assert database.disposed


def test_package_runtime_rejects_unsupported_platform_before_opening_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "is_package_platform_supported", lambda: False)
    monkeypatch.setattr(
        runtime_module,
        "Database",
        lambda: (_ for _ in ()).throw(AssertionError("state must not be opened")),
    )

    with pytest.raises(PackageManagerError) as caught:
        runtime_module.build_package_runtime()

    assert caught.value.code == "PACKAGE_PLATFORM_UNSUPPORTED"
    assert caught.value.exit_code == 5
