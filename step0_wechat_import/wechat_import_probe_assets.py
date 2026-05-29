from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import requests
from PIL import Image

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from common_io import read_json, write_json


DEFAULT_ROOT = Path("code/worktrees/v11_worktree/step0_wechat_import/wechat_import")
DEFAULT_MANIFEST = DEFAULT_ROOT / "manifests" / "all_fetch_manifests.json"
DEFAULT_SEQUENCE = DEFAULT_ROOT / "manifests" / "all_dom_sequence_manifests.json"


def safe_slug(value: str, default: str) -> str:
    text = re.sub(r"\s+", "_", value.strip())
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text)
    text = text.strip("._")
    return (text or default)[:80]


def image_url(item: dict[str, Any]) -> str:
    return str(item.get("dataSrc") or item.get("currentSrc") or item.get("src") or "")


def ext_for_image(item: dict[str, Any], content_type: str | None) -> str:
    declared = str(item.get("dataType") or "").lower().strip(".")
    if declared in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if declared == "jpeg" else declared
    if content_type:
        lower = content_type.lower()
        if "png" in lower:
            return "png"
        if "webp" in lower:
            return "webp"
        if "gif" in lower:
            return "gif"
    return "jpg"


def download_image(url: str, referer: str, out_path: Path) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(response.content)
    return {
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "bytes": len(response.content),
    }


def probe_image(path: Path) -> dict[str, Any]:
    with Image.open(path) as img:
        return {"width": img.width, "height": img.height, "mode": img.mode, "format": img.format}


def pdf_ready_image(path: Path) -> Image.Image:
    img = Image.open(path)
    if img.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.getchannel("A"))
        img.close()
        return background
    if img.mode != "RGB":
        converted = img.convert("RGB")
        img.close()
        return converted
    return img


def build_pdf(image_paths: list[Path], out_path: Path) -> str | None:
    if not image_paths:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    images = [pdf_ready_image(path) for path in image_paths]
    first, rest = images[0], images[1:]
    first.save(out_path, save_all=True, append_images=rest, resolution=150.0)
    for img in images:
        img.close()
    return str(out_path)


def sequence_phase_map(sequence_item: dict[str, Any] | None) -> dict[int, str]:
    if not sequence_item:
        return {}
    result: dict[int, str] = {}
    for item in sequence_item.get("sequence", []) or []:
        if item.get("kind") == "image":
            try:
                result[int(item["imageIndex"])] = str(item.get("phase") or "unknown")
            except (KeyError, TypeError, ValueError):
                continue
    return result


def classify_images(article: dict[str, Any], phase_map: dict[int, str]) -> tuple[dict[int, str], str]:
    title = str(article.get("title") or "")
    image_count = int(article.get("imageCount") or 0)
    phases = [phase_map.get(index, "unknown") for index in range(image_count)]
    if "paper" in phases and "answer" in phases:
        return {index: phase_map.get(index, "unknown") for index in range(image_count)}, "dom_heading_split"

    title_has_answer = bool(re.search(r"答案|解析|评分", title))
    title_has_paper = bool(re.search(r"试题|试卷|练习|模拟", title))
    if title_has_paper and not title_has_answer:
        return {index: "paper" for index in range(image_count)}, "title_paper_only"

    return {index: "unknown" for index in range(image_count)}, "needs_page_classifier"


def build_assets(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    manifest = read_json(Path(args.manifest))
    sequence_manifest = read_json(Path(args.sequence)) if Path(args.sequence).exists() else {"results": []}
    sequence_by_url = {item.get("url"): item for item in sequence_manifest.get("results", [])}

    articles: list[dict[str, Any]] = []
    for article_index, article in enumerate(manifest.get("results", []), start=1):
        title = str(article.get("title") or f"article_{article_index}")
        slug = f"link{article_index:02d}_{safe_slug(title, f'article_{article_index}')}"
        article_dir = root / "articles" / slug
        image_dir = article_dir / "images"
        phase_map = sequence_phase_map(sequence_by_url.get(article.get("url")))
        classification, classification_method = classify_images(article, phase_map)

        image_records: list[dict[str, Any]] = []
        for image in article.get("images", []) or []:
            index = int(image.get("index", len(image_records)))
            url = image_url(image)
            if not url.startswith("http"):
                image_records.append({"index": index, "url": url, "status": "skipped_no_url"})
                continue
            ext = ext_for_image(image, None)
            out_path = image_dir / f"image_{index:02d}.{ext}"
            download_meta = {"status": "existing"}
            if args.force or not out_path.exists():
                preliminary = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": str(article.get("url") or "")},
                    stream=True,
                    timeout=60,
                )
                preliminary.raise_for_status()
                ext = ext_for_image(image, preliminary.headers.get("content-type"))
                out_path = image_dir / f"image_{index:02d}.{ext}"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(preliminary.content)
                download_meta = {
                    "status": "downloaded",
                    "status_code": preliminary.status_code,
                    "content_type": preliminary.headers.get("content-type"),
                    "bytes": len(preliminary.content),
                }
            image_records.append(
                {
                    "index": index,
                    "phase": classification.get(index, "unknown"),
                    "source_phase": phase_map.get(index, "unknown"),
                    "url": url,
                    "path": str(out_path),
                    "declared": {
                        "dataType": image.get("dataType"),
                        "dataW": image.get("dataW"),
                        "dataRatio": image.get("dataRation") or image.get("dataRatio"),
                    },
                    "download": download_meta,
                    "probe": probe_image(out_path),
                }
            )

        all_paths = [Path(item["path"]) for item in image_records if item.get("path")]
        paper_paths = [Path(item["path"]) for item in image_records if item.get("phase") == "paper"]
        answer_paths = [Path(item["path"]) for item in image_records if item.get("phase") == "answer"]
        pdfs = {
            "all_images": build_pdf(all_paths, article_dir / "all_images.pdf"),
            "paper_heuristic": build_pdf(paper_paths, article_dir / "paper_heuristic.pdf"),
            "answer_heuristic": build_pdf(answer_paths, article_dir / "answer_heuristic.pdf"),
        }
        summary = {
            "index": article_index,
            "slug": slug,
            "url": article.get("url"),
            "title": title,
            "author": article.get("author"),
            "image_count": len(image_records),
            "classification_method": classification_method,
            "phase_counts": {
                phase: sum(1 for item in image_records if item.get("phase") == phase)
                for phase in ["paper", "answer", "unknown"]
            },
            "pdfs": pdfs,
            "images": image_records,
        }
        write_json(article_dir / "asset_manifest.json", summary)
        articles.append(summary)
        print(f"[{article_index}] {title}: images={len(image_records)} phases={summary['phase_counts']}")

    output = {
        "root": str(root),
        "article_count": len(articles),
        "articles": articles,
    }
    write_json(root / "wechat_assets_manifest.json", output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download WeChat exam images and build PDF inputs.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--sequence", default=str(DEFAULT_SEQUENCE))
    parser.add_argument("--force", action="store_true")
    return parser


if __name__ == "__main__":
    print(json.dumps(build_assets(build_parser().parse_args()), ensure_ascii=False, indent=2))
