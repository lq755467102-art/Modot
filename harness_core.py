"""Fixed commands for the Windows standalone Harness backend."""

import importlib
import os
from pathlib import Path
import sys


ENTRIES = {
    "owned-process", "server", "run-task", "job", "plan", "review",
    "probe_judge", "render", "board", "contract", "crosscheck",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ENTRIES:
        print("Usage: harness-core <" + "|".join(sorted(ENTRIES)) + "> [args]", file=sys.stderr)
        return 2
    command = sys.argv.pop(1)
    if "__compiled__" in globals():
        executable = Path(sys.argv[0]).resolve()
        os.environ["HARNESS_COMPILED_CORE"] = str(executable)
        os.environ.setdefault("HARNESS_APP", str(executable.parent))
    sys.argv[0] = "bin/" + command + ".py"
    return importlib.import_module("bin." + command).main()


if __name__ == "__main__":
    sys.exit(main())
