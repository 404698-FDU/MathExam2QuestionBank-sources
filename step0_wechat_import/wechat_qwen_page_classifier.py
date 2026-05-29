from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image

CODE_ROOT = Path(__file__).resolve().parents[3]
WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import question_bank_app as qba
import wechat_import_probe_assets as assets
from common_io import data_uri, read_json, write_json


DEFAULT_ASSET_MANIFEST = Path("code/worktrees/v11_worktree/step0_wechat_import/wechat_import/wechat_assets_manifest.json")


def image_probe(path: Path) -> dict[str, Any]:
    with Image.open(path) as img:
        return {"width": img.width, "height": img.height, "mode": img.mode, "format": img.format}


def build_messages(article: dict[str, Any], image: dict[str, Any], image_path: Path) -> list[dict[str, Any]]:
    instruction = {
        "article_title": article.get("title"),
        "article_url": article.get("url"),
        "image_index": image.get("index"),
        "task": "判断这张微信公众号图片在数学试卷导入中的页面角色。",
        "allowed_page_role": ["paper", "answer", "mixed", "cover_or_intro", "noise"],
        "role_definitions": {
            "paper": "试题页，主要包含题干、选择题、填空题、解答题。",
            "answer": "答案页、解析页、评分标准页，主要包含答案、解法、评分说明。",
            "mixed": "同一张图明显同时包含试题和答案。",
            "cover_or_intro": "封面、标题、宣传说明、非题目正文。",
            "noise": "二维码、公众号底图、装饰图、不可用于入库的图片。",
        },
        "return_json_schema": {
            "page_role": "paper|answer|mixed|cover_or_intro|noise",
            "confidence": 0.0,
            "evidence": ["图中可见的关键词或版面证据"],
            "detected_question_numbers": [1, 2],
            "detected_answer_numbers": [1, 2],
            "notes": "",
        },
    }
    return [
        {
            "role": "system",
            "content": "你是数学试卷图片入库的页面分类器。只能根据图片本身判断，不要推测文章缺失内容。",
        },
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri(image_path)}},
                {"type": "text", "text": "/no_think\n只返回 JSON，不要 Markdown。\n" + json.dumps(instruction, ensure_ascii=False, indent=2)},
            ],
        },
    ]


def classify_image(article: dict[str, Any], image: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    image_path = Path(image["path"])
    raw = qba.call_bailian_chat(build_messages(article, image, image_path), args.model, timeout=args.timeout)
    payload = qba.extract_json_object(raw)
    role = str(payload.get("page_role", "noise")).strip().lower()
    if role not in {"paper", "answer", "mixed", "cover_or_intro", "noise"}:
        role = "noise"
    return {
        "index": int(image["index"]),
        "path": str(image_path),
        "probe": image_probe(image_path),
        "page_role": role,
        "confidence": payload.get("confidence"),
        "evidence": payload.get("evidence", []),
        "detected_question_numbers": payload.get("detected_question_numbers", []),
        "detected_answer_numbers": payload.get("detected_answer_numbers", []),
        "notes": str(payload.get("notes", "")),
    }


def build_pdf_for_roles(article_dir: Path, records: list[dict[str, Any]], roles: set[str], name: str) -> str | None:
    paths = [Path(record["path"]) for record in records if record.get("page_role") in roles]
    return assets.build_pdf(paths, article_dir / name) if paths else None


def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = read_json(Path(args.asset_manifest))
    selected_articles = [
        article
        for article in manifest.get("articles", [])
        if (args.article_index is None or int(article["index"]) == args.article_index)
        and (not args.only_needs_classifier or article.get("classification_method") == "needs_page_classifier")
    ]
    results: list[dict[str, Any]] = []
    for article in selected_articles:
        article_dir = Path(manifest["root"]) / "articles" / article["slug"]
        existing_path = article_dir / "qwen_page_classification.json"
        if existing_path.exists() and not args.force:
            results.append(read_json(existing_path))
            continue
        image_results = [classify_image(article, image, args) for image in article.get("images", [])]
        pdfs = {
            "paper_qwen": build_pdf_for_roles(article_dir, image_results, {"paper", "mixed"}, "paper_qwen.pdf"),
            "answer_qwen": build_pdf_for_roles(article_dir, image_results, {"answer", "mixed"}, "answer_qwen.pdf"),
        }
        article_result = {
            "article_index": article["index"],
            "title": article["title"],
            "url": article["url"],
            "model": args.model,
            "image_count": len(image_results),
            "role_counts": {
                role: sum(1 for item in image_results if item["page_role"] == role)
                for role in ["paper", "answer", "mixed", "cover_or_intro", "noise"]
            },
            "pdfs": pdfs,
            "images": image_results,
        }
        write_json(existing_path, article_result)
        results.append(article_result)
        print(json.dumps({key: article_result[key] for key in ["article_index", "title", "role_counts", "pdfs"]}, ensure_ascii=False))
    summary = {"classified_articles": len(results), "results": results}
    out_path = Path(args.output) if args.output else Path(manifest["root"]) / "qwen_page_classification_summary.json"
    write_json(out_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify WeChat exam images with Bailian Qwen VLM.")
    parser.add_argument("--asset-manifest", default=str(DEFAULT_ASSET_MANIFEST))
    parser.add_argument("--article-index", type=int)
    parser.add_argument("--only-needs-classifier", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--model", default=qba.DEFAULT_BAILIAN_MODEL)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output")
    return parser


if __name__ == "__main__":
    print(json.dumps(run(build_parser().parse_args()), ensure_ascii=False, indent=2))
