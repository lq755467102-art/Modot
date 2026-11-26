"""Choose the runtime for Harness commands and user Python scripts separately."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import os
from pathlib import Path
import sys

from . import paths


def command(script, *args, isolated=False, unbuffered=False, python=None):
    core = os.environ.get("HARNESS_COMPILED_CORE")
    if core:
        return [core, Path(script).stem, *args]
    flags = (["-I"] if isolated else []) + (["-u"] if unbuffered else [])
    return [python or sys.executable, *flags, str(paths.app_root() / "bin" / script), *args]


def tool_python():
    if os.environ.get("HARNESS_COMPILED_CORE"):
        return os.environ.get("HARNESS_PYTHON") or str(paths.app_root().parent / "python" / "python.exe")
    return sys.executable
