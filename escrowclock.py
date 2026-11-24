#!/usr/bin/env python3
"""Convenience wrapper: python escrowclock.py issue|take|audit ..."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from escrowclock.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
