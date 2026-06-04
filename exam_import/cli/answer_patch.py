from __future__ import annotations

import argparse
from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.cli.run_pipeline import run_pipeline
from exam_import.schemas.import_spec import load_import_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v12 answer_patch entrypoint.")
    parser.add_argument("--spec", required=True, help="Path to v12 import spec.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_import_spec(Path(args.spec))
    if spec.input_mode != "answer_patch":
        raise SystemExit(f"answer_patch entrypoint requires input_mode=answer_patch, got {spec.input_mode}")
    run_pipeline(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
