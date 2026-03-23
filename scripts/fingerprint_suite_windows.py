#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ["CAMOUFOX_DEFAULT_OS"] = "windows"

SCRIPT_PATH = Path(__file__).with_name("fingerprint_suite.py")
SPEC = importlib.util.spec_from_file_location("fingerprint_suite", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load fingerprint suite from {SCRIPT_PATH}")

MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


if __name__ == "__main__":
    raise SystemExit(MODULE.main())
