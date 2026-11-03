#!/usr/bin/env python3
"""Portable RefCraft inference entry point; see docs/inference.md."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.inference.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
