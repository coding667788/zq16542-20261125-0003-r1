#!/usr/bin/env python3
"""Convenience wrapper: python take.py ..."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from escrowclock.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["take", *sys.argv[1:]]))
