from __future__ import annotations

import base64
import io
import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


TOKEN_BUDGET_ENABLED_ENV = "LLM_TOKEN_BUDGET_ENABLED"
TOKEN_TPM_LIMIT_ENV = "LLM_TOKEN_TPM_LIMIT"
TOKEN_ESTIMATOR_MODEL_ENV = "LLM_TOKEN_ESTIMATOR_MODEL"
TOKEN_IMAGE_MODE_ENV = "LLM_TOKEN_IMAGE_MODE"
TOKEN_OUTPUT_RESERVE_ENV = "LLM_TOKEN_OUTPUT_RESERVE"
TOKEN_VERBOSE_ENV = "LLM_TOKEN_BUDGET_VERBOSE"
TOKEN_LOG_PATH_ENV = "LLM_TOKEN_BUDGET_LOG_PATH"
TOKEN_MODEL_TPM_LIMITS_ENV = "LLM_TOKEN_MODEL_TPM_LIMITS"
TOKEN_POOL_PATH_ENV = "LLM_TOKEN_BUDGET_POOL_PATH"

IMAGE_TOKEN_MODES = {"auto", "processor", "formula"}
DEFAULT_OUTPUT_RESERVE_TOKENS = 2048
DEFAULT_QWEN_VL_FACTOR = 32
DEFAULT_QWEN_VL_MIN_PIXELS = 65_536
DEFAULT_QWEN_VL_MAX_PIXELS = 16_777_216
TOKEN_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class TokenBudgetConfig:
    tpm_limit: int
    estimator_model: str
    image_token_mode: str = "auto"
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS
    verbose: bool = False
    log_path: str = ""
    model_tpm_limits: dict[str, int] = field(default_factory=dict)
    pool_path: str = ""


@dataclass(frozen=True)
class TokenEstimate:
    model: str
    text_tokens: int
    image_tokens: int
    image_boundary_tokens: int
    output_reserve_tokens: int
    total_tokens: int
    image_count: int
    text_method: str
    image_method: str


class TokenRateLimiter:
    def __init__(self, tpm_limit: int, verbose: bool = False) -> None:
        if tpm_limit <= 0:
            raise ValueError("tpm_limit must be positive")
        self.tpm_limit = int(tpm_limit)
        self.verbose = verbose
        self._events: list[tuple[float, int]] = []
        self._condition = threading.Condition()

    def acquire(self, requested_tokens: int, label: str = "") -> None:
        tokens = max(1, int(requested_tokens))
        if tokens > self.tpm_limit:
            if self.verbose:
                print(
                    f"[llm-token] estimate {tokens} exceeds tpm_limit {self.tpm_limit}; "
                    f"reserve one full window for {label or 'request'}",
                    flush=True,
                )
            tokens = self.tpm_limit
        with self._condition:
            while True:
                now = time.monotonic()
                self._events = [(ts, value) for ts, value in self._events if now - ts < 60.0]
                used = sum(value for _, value in self._events)
                if used + tokens <= self.tpm_limit:
                    self._events.append((now, tokens))
                    if self.verbose:
                        print(
                            f"[llm-token] acquired={tokens} used_window={used + tokens}/{self.tpm_limit} "
                            f"label={label}",
                            flush=True,
                        )
                    self._condition.notify_all()
                    return
                oldest = min((ts for ts, _ in self._events), default=now)
                wait_seconds = max(0.25, 60.0 - (now - oldest) + 0.05)
                print(
                    f"[llm-token] TPM wait {wait_seconds:.1f}s "
                    f"used_window={used}/{self.tpm_limit} need={tokens} label={label}",
                    flush=True,
                )
                self._condition.wait(wait_seconds)


class SharedTokenRateLimiter:
    def __init__(self, db_path: Path | str, verbose: bool = False) -> None:
        self.db_path = Path(db_path)
        self.verbose = verbose
        self._schema_lock = threading.Lock()
        self._initialized = False

    def acquire(self, requested_tokens: int, *, bucket: str, tpm_limit: int, label: str = "") -> dict[str, Any]:
        if tpm_limit <= 0:
            raise ValueError("tpm_limit must be positive")
        tokens = max(1, int(requested_tokens))
        if tokens > tpm_limit:
            if self.verbose:
                print(
                    f"[llm-token] estimate {tokens} exceeds tpm_limit {tpm_limit}; "
                    f"reserve one full window for {label or bucket}",
                    flush=True,
                )
            tokens = tpm_limit
        self._ensure_schema()
        while True:
            now = time.time()
            try:
                decision = self._try_reserve(now, tokens, bucket=bucket, tpm_limit=tpm_limit, label=label)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(0.5)
                continue
            if decision["reserved"]:
                if self.verbose:
                    print(
                        f"[llm-token] shared acquired={tokens} "
                        f"used_window={decision['used_after']}/{tpm_limit} bucket={bucket} label={label}",
                        flush=True,
                    )
                return decision
            wait_seconds = float(decision["wait_seconds"])
            print(
                f"[llm-token] shared TPM wait {wait_seconds:.1f}s "
                f"used_window={decision['used_before']}/{tpm_limit} need={tokens} bucket={bucket} label={label}",
                flush=True,
            )
            time.sleep(wait_seconds)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reservations (
                        bucket TEXT NOT NULL,
                        ts REAL NOT NULL,
                        tokens INTEGER NOT NULL,
                        label TEXT NOT NULL,
                        pid INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_reservations_bucket_ts ON reservations(bucket, ts)"
                )
            finally:
                conn.close()
            self._initialized = True

    def _try_reserve(
        self,
        now: float,
        tokens: int,
        *,
        bucket: str,
        tpm_limit: int,
        label: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cutoff = now - TOKEN_WINDOW_SECONDS
                conn.execute("DELETE FROM reservations WHERE ts < ?", (cutoff,))
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0), MIN(ts) FROM reservations WHERE bucket = ?",
                    (bucket,),
                ).fetchone()
                used = int(row[0] or 0)
                oldest = float(row[1] or now)
                if used + tokens <= tpm_limit:
                    conn.execute(
                        "INSERT INTO reservations(bucket, ts, tokens, label, pid) VALUES (?, ?, ?, ?, ?)",
                        (bucket, now, tokens, label, os.getpid()),
                    )
                    conn.execute("COMMIT")
                    return {
                        "reserved": True,
                        "pool_type": "sqlite",
                        "pool_path": str(self.db_path),
                        "pool_bucket": bucket,
                        "pool_tpm_limit": tpm_limit,
                        "reserved_tokens": tokens,
                        "used_before": used,
                        "used_after": used + tokens,
                        "wait_seconds": 0.0,
                    }
                conn.execute("ROLLBACK")
                return {
                    "reserved": False,
                    "pool_type": "sqlite",
                    "pool_path": str(self.db_path),
                    "pool_bucket": bucket,
                    "pool_tpm_limit": tpm_limit,
                    "reserved_tokens": tokens,
                    "used_before": used,
                    "used_after": used,
                    "wait_seconds": max(0.25, TOKEN_WINDOW_SECONDS - (now - oldest) + 0.05),
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


_LIMITER_LOCK = threading.Lock()
_LIMITERS: dict[tuple[Any, ...], TokenRateLimiter | SharedTokenRateLimiter] = {}
_LOG_LOCK = threading.Lock()
_MODEL_LOAD_LOCK = threading.Lock()


def configure_token_budget_env(
    env: dict[str, str] | os._Environ[str],
    *,
    tpm_limit: int | None,
    estimator_model: str | None,
    image_token_mode: str = "auto",
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS,
    verbose: bool = False,
    log_path: Path | str | None = None,
    model_tpm_limits: dict[str, int] | None = None,
    pool_path: Path | str | None = None,
) -> None:
    clean_model_limits = normalize_model_tpm_limits(model_tpm_limits or {})
    if tpm_limit is None and not clean_model_limits:
        return
    limit = int(tpm_limit or 0)
    if limit <= 0 and not clean_model_limits:
        env[TOKEN_BUDGET_ENABLED_ENV] = "0"
        env[TOKEN_TPM_LIMIT_ENV] = "0"
        env.pop(TOKEN_MODEL_TPM_LIMITS_ENV, None)
        env.pop(TOKEN_POOL_PATH_ENV, None)
        return
    mode = image_token_mode if image_token_mode in IMAGE_TOKEN_MODES else "auto"
    env[TOKEN_BUDGET_ENABLED_ENV] = "1"
    env[TOKEN_TPM_LIMIT_ENV] = str(max(0, limit))
    if estimator_model:
        env[TOKEN_ESTIMATOR_MODEL_ENV] = estimator_model
    env[TOKEN_IMAGE_MODE_ENV] = mode
    env[TOKEN_OUTPUT_RESERVE_ENV] = str(max(0, int(output_reserve_tokens)))
    env[TOKEN_VERBOSE_ENV] = "1" if verbose else "0"
    if log_path:
        env[TOKEN_LOG_PATH_ENV] = str(log_path)
    if clean_model_limits:
        env[TOKEN_MODEL_TPM_LIMITS_ENV] = json.dumps(clean_model_limits, ensure_ascii=False, sort_keys=True)
    else:
        env.pop(TOKEN_MODEL_TPM_LIMITS_ENV, None)
    if pool_path:
        env[TOKEN_POOL_PATH_ENV] = str(pool_path)
    else:
        env.pop(TOKEN_POOL_PATH_ENV, None)


def config_from_env(body_model: str) -> TokenBudgetConfig | None:
    enabled = os.environ.get(TOKEN_BUDGET_ENABLED_ENV, "").strip()
    limit_text = os.environ.get(TOKEN_TPM_LIMIT_ENV, "").strip()
    model_limits = parse_model_tpm_limits_text(os.environ.get(TOKEN_MODEL_TPM_LIMITS_ENV, "").strip())
    if enabled in {"", "0", "false", "False"} and not limit_text and not model_limits:
        return None
    try:
        limit = int(limit_text or "0")
    except ValueError:
        limit = 0
    if limit <= 0 and not model_limits:
        return None
    model = os.environ.get(TOKEN_ESTIMATOR_MODEL_ENV, "").strip() or body_model.strip()
    if not model:
        raise RuntimeError("Token budget is enabled but no estimator model is configured.")
    mode = os.environ.get(TOKEN_IMAGE_MODE_ENV, "auto").strip() or "auto"
    if mode not in IMAGE_TOKEN_MODES:
        raise RuntimeError(f"Unsupported image token mode: {mode}")
    try:
        reserve = int(os.environ.get(TOKEN_OUTPUT_RESERVE_ENV, str(DEFAULT_OUTPUT_RESERVE_TOKENS)))
    except ValueError:
        reserve = DEFAULT_OUTPUT_RESERVE_TOKENS
    verbose = os.environ.get(TOKEN_VERBOSE_ENV, "").strip() in {"1", "true", "True", "yes"}
    return TokenBudgetConfig(
        tpm_limit=limit,
        estimator_model=model,
        image_token_mode=mode,
        output_reserve_tokens=max(0, reserve),
        verbose=verbose,
        log_path=os.environ.get(TOKEN_LOG_PATH_ENV, "").strip(),
        model_tpm_limits=model_limits,
        pool_path=os.environ.get(TOKEN_POOL_PATH_ENV, "").strip(),
    )


def normalize_model_tpm_limits(model_tpm_limits: dict[str, int]) -> dict[str, int]:
    clean: dict[str, int] = {}
    for model, limit in model_tpm_limits.items():
        model_text = str(model).strip()
        if not model_text:
            continue
        limit_int = int(limit)
        if limit_int <= 0:
            raise ValueError(f"TPM limit for model {model_text!r} must be positive")
        clean[model_text] = limit_int
    return clean


def parse_model_tpm_limits_text(text: str) -> dict[str, int]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {TOKEN_MODEL_TPM_LIMITS_ENV}: expected JSON object") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid {TOKEN_MODEL_TPM_LIMITS_ENV}: expected JSON object")
    return normalize_model_tpm_limits({str(key): int(value) for key, value in payload.items()})


def token_bucket_for_model(config: TokenBudgetConfig, body_model: str) -> tuple[str, int]:
    request_model = body_model.strip()
    if config.model_tpm_limits:
        if not request_model:
            raise RuntimeError("Token budget model TPM limits are configured but request body has no model.")
        if request_model not in config.model_tpm_limits:
            configured = ", ".join(sorted(config.model_tpm_limits))
            raise RuntimeError(
                f"No TPM limit configured for request model {request_model!r}. "
                f"Configured models: {configured}"
            )
        return request_model, config.model_tpm_limits[request_model]
    if config.tpm_limit <= 0:
        raise RuntimeError("Token budget is enabled but no TPM limit is configured.")
    return "default", config.tpm_limit


def _limiter_for(config: TokenBudgetConfig, bucket: str, tpm_limit: int) -> TokenRateLimiter | SharedTokenRateLimiter:
    key = (
        "sqlite",
        str(Path(config.pool_path).resolve()),
        config.verbose,
    ) if config.pool_path else ("memory", bucket, int(tpm_limit), config.verbose)
    with _LIMITER_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = (
                SharedTokenRateLimiter(config.pool_path, verbose=config.verbose)
                if config.pool_path
                else TokenRateLimiter(tpm_limit, verbose=config.verbose)
            )
            _LIMITERS[key] = limiter
        return limiter


def maybe_acquire_token_budget(body: dict[str, Any]) -> dict[str, Any] | None:
    body_model = str(body.get("model") or "")
    config = config_from_env(body_model)
    if config is None:
        return None
    estimate = estimate_chat_completion_tokens(body, config)
    bucket, tpm_limit = token_bucket_for_model(config, body_model)
    limiter = _limiter_for(config, bucket, tpm_limit)
    if isinstance(limiter, SharedTokenRateLimiter):
        reservation = limiter.acquire(
            estimate.total_tokens,
            bucket=bucket,
            tpm_limit=tpm_limit,
            label=body_model or config.estimator_model,
        )
    else:
        limiter.acquire(estimate.total_tokens, label=body_model or config.estimator_model)
        reservation = {
            "pool_type": "memory",
            "pool_path": "",
            "pool_bucket": bucket,
            "pool_tpm_limit": tpm_limit,
            "reserved_tokens": min(estimate.total_tokens, tpm_limit),
        }
    payload = asdict(estimate)
    payload["request_model"] = body_model
    payload.update(reservation)
    _append_log(config.log_path, payload)
    return payload


def estimate_chat_completion_tokens(body: dict[str, Any], config: TokenBudgetConfig) -> TokenEstimate:
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    images = extract_image_infos(messages)
    text_tokens = estimate_body_text_tokens(body, messages, config.estimator_model)
    image_tokens, image_method = estimate_image_tokens(images, config.estimator_model, config.image_token_mode)
    image_boundary_tokens = 2 * len(images)
    output_reserve = response_token_reserve(body, config.output_reserve_tokens)
    total = text_tokens + image_tokens + image_boundary_tokens + output_reserve
    return TokenEstimate(
        model=config.estimator_model,
        text_tokens=text_tokens,
        image_tokens=image_tokens,
        image_boundary_tokens=image_boundary_tokens,
        output_reserve_tokens=output_reserve,
        total_tokens=max(1, total),
        image_count=len(images),
        text_method="transformers.AutoTokenizer.apply_chat_template",
        image_method=image_method,
    )


def response_token_reserve(body: dict[str, Any], default_reserve: int) -> int:
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return max(0, int(default_reserve))


def messages_for_text_tokenizer(messages: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        out.append({"role": role, "content": collect_text(message.get("content"))})
    return out


def collect_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


@lru_cache(maxsize=4)
def _tokenizer(model: str) -> Any:
    with _MODEL_LOAD_LOCK:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(model, trust_remote_code=True)


def estimate_body_text_tokens(body: dict[str, Any], messages: list[Any], model: str) -> int:
    tokenizer = _tokenizer(model)
    total = estimate_text_tokens(messages, model)
    extra_text = body_extra_prompt_text(body)
    if extra_text:
        total += len(tokenizer.encode(extra_text, add_special_tokens=False))
    return total


def estimate_text_tokens(messages: list[Any], model: str) -> int:
    tokenizer = _tokenizer(model)
    text_messages = messages_for_text_tokenizer(messages)
    if hasattr(tokenizer, "apply_chat_template") and getattr(tokenizer, "chat_template", None):
        tokenized = tokenizer.apply_chat_template(
            text_messages,
            tokenize=True,
            add_generation_prompt=True,
        )
        if isinstance(tokenized, dict) and "input_ids" in tokenized:
            return len(tokenized["input_ids"])
        return len(tokenized)
    flattened = "\n".join(f"{item['role']}: {item['content']}" for item in text_messages)
    return len(tokenizer.encode(flattened, add_special_tokens=True))


def body_extra_prompt_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("response_format", "tools", "tool_choice"):
        value = body.get(key)
        if value is not None:
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(parts)


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    data: bytes | None = None


def extract_image_infos(messages: list[Any]) -> list[ImageInfo]:
    infos: list[ImageInfo] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            image_url = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
            url = str(image_url.get("url") or "")
            info = image_info_from_url(url)
            if info is not None:
                infos.append(info)
    return infos


def image_info_from_url(url: str) -> ImageInfo | None:
    if url.startswith("data:"):
        try:
            _header, encoded = url.split(",", 1)
            data = base64.b64decode(encoded)
            width, height = image_size_from_bytes(data)
            return ImageInfo(width=width, height=height, data=data)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Failed to decode image data URI for token estimation.") from exc
    path = Path(url.removeprefix("file://"))
    if path.exists():
        data = path.read_bytes()
        width, height = image_size_from_bytes(data)
        return ImageInfo(width=width, height=height, data=data)
    return None


def image_size_from_bytes(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return int(image.width), int(image.height)


def estimate_image_tokens(images: list[ImageInfo], model: str, mode: str) -> tuple[int, str]:
    if not images:
        return 0, "none"
    if mode in {"auto", "processor"}:
        try:
            return estimate_image_tokens_with_processor(images, model), "transformers.AutoProcessor"
        except Exception:
            if mode == "processor":
                raise
    return estimate_image_tokens_with_formula(images), "qwen_vl_32px_formula"


@lru_cache(maxsize=2)
def _processor(model: str) -> Any:
    with _MODEL_LOAD_LOCK:
        from transformers import AutoProcessor

        return AutoProcessor.from_pretrained(model, trust_remote_code=True)


def estimate_image_tokens_with_processor(images: list[ImageInfo], model: str) -> int:
    from PIL import Image

    processor = _processor(model)
    if not images:
        return 0
    pil_images = []
    for info in images:
        with Image.open(io.BytesIO(info.data or b"")) as image:
            pil_images.append(image.convert("RGB"))
    try:
        placeholder = "<|vision_start|><|image_pad|><|vision_end|>"
        text = "\n".join(placeholder for _ in images)
        inputs = processor(text=[text], images=pil_images, return_tensors="pt")
        tokenizer = getattr(processor, "tokenizer", None) or _tokenizer(model)
        image_token_id = tokenizer.convert_tokens_to_ids("<|image_pad|>")
        input_ids = inputs["input_ids"]
        return int((input_ids == image_token_id).sum().item())
    finally:
        for image in pil_images:
            image.close()


def estimate_image_tokens_with_formula(images: list[ImageInfo]) -> int:
    return sum(qwen_vl_formula_tokens(info.width, info.height) for info in images)


def qwen_vl_formula_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return 0
    factor = DEFAULT_QWEN_VL_FACTOR
    pixels = width * height
    if pixels > DEFAULT_QWEN_VL_MAX_PIXELS:
        scale = math.sqrt(DEFAULT_QWEN_VL_MAX_PIXELS / pixels)
    elif pixels < DEFAULT_QWEN_VL_MIN_PIXELS:
        scale = math.sqrt(DEFAULT_QWEN_VL_MIN_PIXELS / pixels)
    else:
        scale = 1.0
    resized_w = max(factor, int(round(width * scale / factor)) * factor)
    resized_h = max(factor, int(round(height * scale / factor)) * factor)
    while resized_w * resized_h > DEFAULT_QWEN_VL_MAX_PIXELS:
        if resized_w >= resized_h and resized_w > factor:
            resized_w -= factor
        elif resized_h > factor:
            resized_h -= factor
        else:
            break
    while resized_w * resized_h < DEFAULT_QWEN_VL_MIN_PIXELS:
        if resized_w <= resized_h:
            resized_w += factor
        else:
            resized_h += factor
    return max(1, (resized_w // factor) * (resized_h // factor))


def _append_log(path_text: str, payload: dict[str, Any]) -> None:
    if not path_text:
        return
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(payload)
    row["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    row["pid"] = os.getpid()
    with _LOG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
