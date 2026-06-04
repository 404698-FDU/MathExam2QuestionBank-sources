from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepName(str, Enum):
    SOURCE_IMPORT = "source_import"
    STEP2 = "step2"
    STEP2_CROP = "step2_crop"
    STEP3 = "step3"
    STEP35 = "step3_5"
    STEP4 = "step4"
    STEP45 = "step4_5"
    STEP5 = "step5"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class StepState:
    name: StepName
    status: StepStatus = StepStatus.PENDING
    message: str = ""
