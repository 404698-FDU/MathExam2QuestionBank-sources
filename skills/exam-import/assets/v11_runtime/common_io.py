from __future__ import annotations

import base64
import io
import json
import mimetypes
from pathlib import Path
from typing import Any


_MISSING = object()


def read_json(path: Path, default: Any = _MISSING) -> Any:
    if not path.exists():
        if default is _MISSING:
            raise FileNotFoundError(path)
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def data_uri(path: Path, max_bytes: int | None = None, cache_dir: Path | None = None) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = path.read_bytes()
    uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    if max_bytes is None or len(uri.encode("utf-8")) <= max_bytes:
        return uri
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Image data-uri exceeds {max_bytes} bytes and Pillow is unavailable: {path}") from exc

    cache_root = cache_dir or (path.parent / ".llm_data_uri_cache")
    cache_root.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        for quality in (85, 75, 65, 55, 45):
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=quality, optimize=True)
            jpeg_data = out.getvalue()
            jpeg_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg_data).decode('ascii')}"
            if len(jpeg_uri.encode("utf-8")) <= max_bytes:
                cached = cache_root / f"{path.stem}.max{max_bytes}.q{quality}.jpg"
                if not cached.exists():
                    cached.write_bytes(jpeg_data)
                return jpeg_uri
    raise RuntimeError(f"Image data-uri exceeds {max_bytes} bytes after JPEG compression: {path}")


def text_excerpt(text: str, limit: int = 220) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
