from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class TokenEstimate:
    text_tokens: int
    image_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.text_tokens + self.image_tokens


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_image_tokens(image_count: int, per_image_tokens: int = 1024) -> int:
    if image_count <= 0:
        return 0
    return image_count * per_image_tokens


def estimate_request_tokens(text: str, image_count: int) -> TokenEstimate:
    return TokenEstimate(
        text_tokens=estimate_text_tokens(text),
        image_tokens=estimate_image_tokens(image_count),
    )
