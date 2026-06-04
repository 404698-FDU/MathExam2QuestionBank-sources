from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from exam_import.schemas.import_spec import load_import_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a v12 import spec.")
    parser.add_argument("--spec", required=True, help="Path to import_spec.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = load_import_spec(Path(args.spec))
    print(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
