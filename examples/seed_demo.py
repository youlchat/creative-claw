"""Backward-compatible alias for the repository-contained demo bootstrap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bootstrap_demo import REPOSITORY_ROOT, bootstrap


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Creative Claw demo")
    parser.add_argument("--db", default=str(REPOSITORY_ROOT / ".creative-claw" / "demo.db"))
    parser.add_argument(
        "--project-root",
        default=str(REPOSITORY_ROOT / ".creative-claw" / "projects" / "demo"),
    )
    parser.add_argument("--project-id", default="demo")
    parser.add_argument(
        "--approve-writes",
        action="store_true",
        help="Deprecated compatibility option; the bootstrap does not write Office files.",
    )
    args = parser.parse_args()
    result = bootstrap(Path(args.db), Path(args.project_root), args.project_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
