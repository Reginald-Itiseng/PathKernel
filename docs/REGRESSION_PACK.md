# Regression Pack

## Purpose
The regression pack protects geometry correctness while renderer code evolves.
It captures deterministic summaries for known-good CAM files and compares
future runs against that baseline.

## Baseline File
- `tests/regression/lumacore_baseline.json`

Each case includes:
- file `sha256`
- parser-level counts and classes
- source bbox
- generated shape count
- generated geometry bounds
- unsupported primitive summary

## Generate a New Baseline
```bash
python tools/generate_regression_baseline.py --out tests/regression/my_board_baseline.json <file1> <file2> ...
```

## Run Regression Test
```bash
python -m pytest -q tests/test_regression_pack.py
```

Notes:
- Test skips if local files from baseline are not present.
- For team portability, prefer storing representative public sample files or
  maintaining per-developer local baselines.

