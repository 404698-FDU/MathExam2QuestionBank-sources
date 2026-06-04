from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib import request

from .call_spec_loader import ResolvedCallSpec


@dataclass(frozen=True)
class LLMResponse:
    status_code: int
    body: dict[str, Any]
    raw_text: str


class LLMClient:
    def send_chat(
        self,
        resolved: ResolvedCallSpec,
        payload: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> LLMResponse:
        token = resolved.provider.resolve_token(env=env)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **resolved.provider.request_headers,
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=resolved.provider.chat_endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=resolved.call_spec.timeout) as resp:
            raw_text = resp.read().decode("utf-8")
            parsed = json.loads(raw_text)
            return LLMResponse(status_code=resp.status, body=parsed, raw_text=raw_text)
