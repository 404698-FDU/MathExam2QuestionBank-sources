from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .pipeline_state import StepName


@dataclass(frozen=True)
class StepEvidence:
    step: StepName
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceReport:
    run_id: str
    alignment_mode: str
    steps: list[StepEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "alignment_mode": self.alignment_mode,
            "steps": [item.to_dict() for item in self.steps],
        }
