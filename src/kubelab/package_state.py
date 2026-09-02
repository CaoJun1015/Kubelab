"""Persistence DTOs for trusted local package inventory and provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PackageStatus(StrEnum):
    STAGED = "staged"
    ENABLED = "enabled"
    DISABLED = "disabled"
    PENDING_REMOVAL = "pending_removal"
    REMOVED = "removed"


class LabSource(StrEnum):
    BUILTIN = "builtin"
    LOCAL_PACKAGE = "local_package"


class PackageStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewLabPackage(PackageStateModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lab_id: str = Field(min_length=1, max_length=64)
    package_version: str = Field(min_length=1, max_length=64)
    publisher_id: str = Field(min_length=1, max_length=63)
    publisher_name: str = Field(min_length=1, max_length=80)
    requires_kubelab: str = Field(min_length=1, max_length=120)
    format_version: int = Field(ge=2, le=2)
    archive_size: int = Field(ge=0, le=5 * 1024 * 1024)
    imported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LabPackageSnapshot(NewLabPackage):
    status: PackageStatus
    enabled_at: datetime | None
    disabled_at: datetime | None
    pending_removal_at: datetime | None
    removed_at: datetime | None


class LabPackageEventSnapshot(PackageStateModel):
    id: int
    package_sha256: str
    event_type: str
    context: dict[str, Any] | None
    created_at: datetime


__all__ = [
    "LabPackageEventSnapshot",
    "LabPackageSnapshot",
    "LabSource",
    "NewLabPackage",
    "PackageStatus",
]
