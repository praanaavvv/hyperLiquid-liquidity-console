"""Run: uv run --python 3.11 collector/test_crude.py"""
import tempfile
from datetime import date
from pathlib import Path

from crude import prune

with tempfile.TemporaryDirectory() as t:
    root = Path(t)
    for name in ["2026-09-10", "2026-09-17", "2026-09-18", "2026-09-25", "junk"]:
        (root / "coin=BTC" / f"date={name}").mkdir(parents=True)
    gone = prune(root, 7, today=date(2026, 9, 25))
    left = sorted(p.name for p in root.glob("coin=*/date=*"))
    assert [p.name for p in gone] == ["date=2026-09-10", "date=2026-09-17"], gone
    assert left == ["date=2026-09-18", "date=2026-09-25", "date=junk"], left
print("ok")
