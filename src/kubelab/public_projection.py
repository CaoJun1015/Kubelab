"""Pure disclosure rules shared by learner and author-facing projections."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kubelab.lab_registry import LoadedVariant


class ScenarioDisclosure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revealed: bool
    scenario_name: str | None = None
    scenario_description: str | None = None
    key_evidence: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    prevention: str | None = None


def project_variant_disclosure(variant: LoadedVariant, *, revealed: bool) -> ScenarioDisclosure:
    """Return no scenario identity or answer until the success contract passes."""
    if not revealed:
        return ScenarioDisclosure(revealed=False)
    metadata = variant.definition.metadata
    reveal = variant.definition.reveal
    return ScenarioDisclosure(
        revealed=True,
        scenario_name=metadata.name,
        scenario_description=metadata.description,
        key_evidence=reveal.key_evidence,
        root_cause=reveal.root_cause,
        resolution=reveal.resolution,
        prevention=reveal.prevention,
    )


__all__ = ["ScenarioDisclosure", "project_variant_disclosure"]
