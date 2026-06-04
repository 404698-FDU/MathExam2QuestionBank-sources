from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path


IMAGE_SUFFIX_ORDER = [".png", ".jpg", ".jpeg", ".webp"]
TABLE_SUFFIX_ORDER = [".html", ".htm", ".png", ".jpg", ".jpeg", ".webp"]


@dataclass(frozen=True)
class AssetResolver:
    asset_root: Path

    def resolve_path(self, label: str, asset_tag: str) -> Path | None:
        if not self.asset_root.exists():
            return None
        candidates = [candidate for candidate in self.asset_root.glob(f"{label}.*") if candidate.is_file()]
        if not candidates:
            return None
        suffix_order = TABLE_SUFFIX_ORDER if asset_tag == "table" else IMAGE_SUFFIX_ORDER

        def sort_key(candidate: Path) -> tuple[int, str]:
            try:
                rank = suffix_order.index(candidate.suffix.lower())
            except ValueError:
                rank = len(suffix_order)
            return rank, candidate.name

        return sorted(candidates, key=sort_key)[0]

    def render_asset_html(self, asset_tag: str, label: str) -> str:
        asset_path = self.resolve_path(label, asset_tag)
        safe_label = escape(label)
        if asset_path is None:
            return (
                f'<figure class="asset asset-missing" data-label="{safe_label}">'
                f'<div class="asset-missing-label">Missing asset: {safe_label}</div>'
                "</figure>"
            )
        relative = asset_path.name
        if asset_tag in {"img", "chart"}:
            return (
                f'<figure class="asset asset-{asset_tag}" data-label="{safe_label}">'
                f'<img src="assets/{escape(relative)}" alt="{safe_label}">'
                "</figure>"
            )
        if asset_tag == "table" and asset_path.suffix.lower() == ".html":
            return (
                f'<figure class="asset asset-table" data-label="{safe_label}">'
                f"{asset_path.read_text(encoding='utf-8')}"
                "</figure>"
            )
        if asset_tag == "table":
            return (
                f'<figure class="asset asset-table" data-label="{safe_label}">'
                f'<img src="assets/{escape(relative)}" alt="{safe_label}">'
                "</figure>"
            )
        return f'<span class="asset asset-inline" data-label="{safe_label}">{safe_label}</span>'
