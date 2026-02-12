from __future__ import annotations

"""Generate regression baseline JSON from one or more CAM files.

Usage:
  python tools/generate_regression_baseline.py --out tests/regression/baseline.local.json <file1> <file2> ...
"""

import argparse
import json
from pathlib import Path

from app.core.regression import summarize_case


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="Gerber/Excellon input files")
    parser.add_argument("--out", required=True, help="Output baseline JSON path")
    args = parser.parse_args()

    cases = []
    for file_str in args.files:
        path = Path(file_str)
        cases.append(summarize_case(path))

    output = {"cases": cases}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote baseline: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

