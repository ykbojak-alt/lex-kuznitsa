"""Run the model-refusal marker regression check against the selected source tree."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("LEX_TEST_SRC", ROOT / "src")).resolve()
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "demo"))

from refusal_marker_tests import run_refusal_marker_tests  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    count, failures = run_refusal_marker_tests()
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{count}/4 refusal-marker cases passed")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
