from __future__ import annotations

import os
import sys
import time
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


WORKTREE_ROOT = Path(__file__).resolve().parent
CODE_ROOT = WORKTREE_ROOT.parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import question_bank_app as qba  # noqa: E402
from common_token_budget import maybe_acquire_token_budget  # noqa: E402


SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_CHAT_ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"
BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BAILIAN_CHAT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
BAILIAN_BATCH_BASE_URL = "https://batch.dashscope.aliyuncs.com/compatible-mode/v1"
BAILIAN_BATCH_CHAT_ENDPOINT = "https://batch.dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
SILICONFLOW_TOKEN_FILES = (
    CODE_ROOT.parent / ".siliconflow_token",
    CODE_ROOT.parent / ".silliconflow_token",
)
REPO_ROOT = WORKTREE_ROOT.parents[3]
BAILIAN_TOKEN_FILES = (
    CODE_ROOT.parent / ".bailian_token",
    REPO_ROOT / ".bailian_token",
    REPO_ROOT / ".dashscope_token",
    REPO_ROOT / ".dashscope_api_key",
    REPO_ROOT / "api_key" / ".bailian_api_key",
)
DEFAULT_CHAT_ENDPOINT = SILICONFLOW_CHAT_ENDPOINT
DEFAULT_TOKEN_FILES = SILICONFLOW_TOKEN_FILES
LLM_ENDPOINT_ENV = "LLM_ENDPOINT"
LLM_BASE_URL_ENV = "LLM_BASE_URL"
LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_THINKING_TYPE_ENV = "LLM_THINKING_TYPE"
RATE_LIMIT_SLEEP_SECONDS = 60
MAX_RATE_LIMIT_RETRIES = 60
TRANSIENT_ERROR_SLEEP_SECONDS = 60
MAX_TRANSIENT_RETRIES = 8
RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "tpm",
    "tokens per minute",
    "throttl",
)


def first_token(paths: list[Path] | tuple[Path, ...]) -> str:
    for path in paths:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token.removeprefix("Bearer ").strip()
    return ""


def load_token(
    env_keys: tuple[str, ...] = (LLM_API_KEY_ENV, "SILICONFLOW_API_KEY"),
    token_files: tuple[Path, ...] = DEFAULT_TOKEN_FILES,
    missing_message: str = "missing LLM token",
) -> str:
    for key in env_keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value.removeprefix("Bearer ").strip()
    token = first_token(token_files)
    if token:
        return token
    raise RuntimeError(missing_message)


def configure_llm_provider_env(env: dict[str, str] | os._Environ[str], provider: str) -> None:
    if provider == "env":
        return
    if provider == "siliconflow":
        token = (
            env.get(LLM_API_KEY_ENV, "").strip()
            or env.get("SILICONFLOW_API_KEY", "").strip()
            or first_token(DEFAULT_TOKEN_FILES)
        )
        env[LLM_ENDPOINT_ENV] = SILICONFLOW_CHAT_ENDPOINT
        env[LLM_BASE_URL_ENV] = SILICONFLOW_BASE_URL
        if token:
            env[LLM_API_KEY_ENV] = token
            env["SILICONFLOW_API_KEY"] = token
        return
    if provider in {"bailian", "bailian_batch"}:
        token = (
            env.get(LLM_API_KEY_ENV, "").strip()
            or env.get("DASHSCOPE_API_KEY", "").strip()
            or env.get("BAILIAN_API_KEY", "").strip()
            or first_token(BAILIAN_TOKEN_FILES)
        )
        env[LLM_ENDPOINT_ENV] = BAILIAN_BATCH_CHAT_ENDPOINT if provider == "bailian_batch" else BAILIAN_CHAT_ENDPOINT
        env[LLM_BASE_URL_ENV] = BAILIAN_BATCH_BASE_URL if provider == "bailian_batch" else BAILIAN_BASE_URL
        if token:
            env["BAILIAN_API_KEY"] = token
            env["DASHSCOPE_API_KEY"] = token
            env[LLM_API_KEY_ENV] = token
        return
    raise ValueError(f"Unsupported LLM provider: {provider}")


def is_rate_limit_error(status_code: int | None, body_text: str) -> bool:
    if status_code == 429:
        return True
    lowered = body_text.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def is_transient_http_error(status_code: int | None) -> bool:
    return status_code in {500, 502, 503, 504}


def endpoint_token_from_env() -> tuple[str, str]:
    endpoint = os.environ.get(LLM_ENDPOINT_ENV, "").strip() or DEFAULT_CHAT_ENDPOINT
    token = (
        os.environ.get(LLM_API_KEY_ENV, "").strip()
        or os.environ.get("SILICONFLOW_API_KEY", "").strip()
        or os.environ.get("DASHSCOPE_API_KEY", "").strip()
        or os.environ.get("BAILIAN_API_KEY", "").strip()
        or first_token(DEFAULT_TOKEN_FILES)
    )
    if not token:
        raise RuntimeError("missing LLM token")
    return endpoint, token.removeprefix("Bearer ").strip()


def post_chat_completion(
    body: dict[str, Any],
    timeout: int,
    endpoint: str | None = None,
    token: str | None = None,
    max_rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES,
    max_transient_retries: int = MAX_TRANSIENT_RETRIES,
) -> dict[str, Any]:
    selected_endpoint, selected_token = (endpoint, token) if endpoint and token else endpoint_token_from_env()
    selected_endpoint = endpoint or selected_endpoint
    selected_token = (token or selected_token).removeprefix("Bearer ").strip()
    thinking_type = os.environ.get(LLM_THINKING_TYPE_ENV, "").strip()
    if thinking_type:
        body = {key: value for key, value in body.items() if key != "enable_thinking"}
        body["thinking"] = {"type": thinking_type}
    token_estimate = maybe_acquire_token_budget(body)
    attempts = 0
    transient_attempts = 0
    while True:
        request = urllib.request.Request(
            selected_endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {selected_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if token_estimate is not None and isinstance(payload, dict):
                    payload["_local_token_estimate"] = token_estimate
                return payload
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            if is_rate_limit_error(exc.code, body_text) and attempts < max_rate_limit_retries:
                attempts += 1
                print(
                    f"[llm] rate limit/TPM hit; sleep {RATE_LIMIT_SLEEP_SECONDS}s then retry "
                    f"({attempts}/{max_rate_limit_retries})",
                    flush=True,
                )
                time.sleep(RATE_LIMIT_SLEEP_SECONDS)
                continue
            if is_transient_http_error(exc.code) and transient_attempts < max_transient_retries:
                transient_attempts += 1
                print(
                    f"[llm] transient HTTP {exc.code}; sleep {TRANSIENT_ERROR_SLEEP_SECONDS}s then retry "
                    f"({transient_attempts}/{max_transient_retries})",
                    flush=True,
                )
                time.sleep(TRANSIENT_ERROR_SLEEP_SECONDS)
                continue
            raise RuntimeError(f"LLM API HTTP {exc.code}: {body_text[:1200]}") from exc
        except TimeoutError as exc:
            if transient_attempts < max_transient_retries:
                transient_attempts += 1
                print(
                    f"[llm] read timeout; sleep {TRANSIENT_ERROR_SLEEP_SECONDS}s then retry "
                    f"({transient_attempts}/{max_transient_retries})",
                    flush=True,
                )
                time.sleep(TRANSIENT_ERROR_SLEEP_SECONDS)
                continue
            raise RuntimeError(f"LLM API timeout: {exc}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError) and transient_attempts < max_transient_retries:
                transient_attempts += 1
                print(
                    f"[llm] URL timeout; sleep {TRANSIENT_ERROR_SLEEP_SECONDS}s then retry "
                    f"({transient_attempts}/{max_transient_retries})",
                    flush=True,
                )
                time.sleep(TRANSIENT_ERROR_SLEEP_SECONDS)
                continue
            raise RuntimeError(f"LLM API network error: {exc}") from exc


def chat_completion_content(
    messages: list[dict[str, Any]],
    model: str,
    timeout: int,
    temperature: float = 0.0,
    top_p: float = 0.8,
    enable_thinking: bool = False,
) -> str:
    payload = post_chat_completion(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "enable_thinking": enable_thinking,
        },
        timeout=timeout,
    )
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM API unexpected response: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc


def call_chat_json(messages: list[dict[str, Any]], model: str, timeout: int) -> tuple[dict[str, Any], str, float]:
    started = time.monotonic()
    raw = chat_completion_content(messages, model=model, timeout=timeout)
    elapsed = time.monotonic() - started
    parsed = qba.extract_json_object(raw)
    return parsed, raw, elapsed


def call_chat_tool_json(
    messages: list[dict[str, Any]],
    model: str,
    timeout: int,
    *,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    enable_thinking: bool = False,
) -> tuple[dict[str, Any], str, float]:
    started = time.monotonic()
    tool_instruction = (
        f"本次请求必须调用工具 {tool_name} 返回结构化参数。"
        "不要在 message.content 中输出 JSON、Markdown 或解释。"
    )
    if messages and messages[0].get("role") == "system":
        tool_messages = [dict(messages[0])]
        tool_messages[0]["content"] = tool_instruction + "\n" + str(tool_messages[0].get("content") or "")
        tool_messages.extend(messages[1:])
    else:
        tool_messages = [{"role": "system", "content": tool_instruction}, *messages]
    payload = post_chat_completion(
        {
            "model": model,
            "messages": tool_messages,
            "temperature": 0.0,
            "top_p": 0.8,
            "enable_thinking": enable_thinking,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool_description,
                        "parameters": tool_schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        },
        timeout=timeout,
    )
    elapsed = time.monotonic() - started
    try:
        tool_calls = payload["choices"][0]["message"].get("tool_calls") or []
        actual_name = tool_calls[0]["function"].get("name")
        if actual_name != tool_name:
            raise RuntimeError(f"LLM API called unexpected tool {actual_name!r}; expected {tool_name!r}")
        raw = tool_calls[0]["function"]["arguments"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM API returned no tool call: {json.dumps(payload, ensure_ascii=False)[:1200]}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM tool arguments must be a JSON object: {raw[:1200]}")
    return parsed, raw, elapsed


def call_chat_structured_json(
    messages: list[dict[str, Any]],
    model: str,
    timeout: int,
    *,
    structured_output: str = "json_object",
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    enable_thinking: bool = False,
) -> tuple[dict[str, Any], str, float]:
    if structured_output == "json_object":
        return call_chat_json(messages, model=model, timeout=timeout)
    if structured_output == "tool_calling":
        return call_chat_tool_json(
            messages,
            model=model,
            timeout=timeout,
            tool_name=tool_name,
            tool_description=tool_description,
            tool_schema=tool_schema,
            enable_thinking=enable_thinking,
        )
    raise ValueError(f"Unsupported structured_output: {structured_output}")
