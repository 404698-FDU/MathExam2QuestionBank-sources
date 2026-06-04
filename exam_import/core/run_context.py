from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


VALID_ALIGNMENT_MODES = {
    "pure_paper",
    "mixed",
    "paper_plus_answer",
    "answer_patch",
}


@dataclass(frozen=True)
class RunContext:
    run_id: str
    alignment_mode: str
    source_runs_root: Path
    step2_root: Path
    question_bank_root: Path
    review_root: Path
    render_root: Path

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.alignment_mode not in VALID_ALIGNMENT_MODES:
            raise ValueError(f"Unsupported alignment_mode: {self.alignment_mode}")

    @property
    def source_run_dir(self) -> Path:
        return self.source_runs_root / self.run_id

    @property
    def step2_run_dir(self) -> Path:
        return self.step2_root / f"{self.run_id}_raw_units"

    @property
    def question_bank_dir(self) -> Path:
        return self.question_bank_root / self.run_id

    @property
    def review_dir(self) -> Path:
        return self.review_root / self.run_id

    @property
    def render_dir(self) -> Path:
        return self.render_root / self.run_id
