"""Current clean-checkout offline regression. Never reads personal model config.

Run with the project's Python dependencies installed, or pass the trusted staging
directories in HARNESS_TEST_DEPENDENCIES (os.pathsep-separated). Each module gets
its own interpreter; reports are outside the source tree by default.
"""

from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def worker(module):
    sys.path.insert(0, str(ROOT))
    for directory in os.environ.get("HARNESS_TEST_DEPENDENCIES", "").split(os.pathsep):
        if directory:
            sys.path.append(str(Path(directory).resolve(strict=True)))
    import ipaddress

    # Test-side guard only, not an OS sandbox or a product capability.
    def audit(event, args):
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, tuple):
                host = address[0]
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host == "localhost"
                if not local:
                    raise RuntimeError(
                        "Offline regression refused a non-loopback connection"
                    )

    sys.addaudithook(audit)
    import unittest

    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("SKIPPED_TESTS=" + json.dumps(result.skipped))
    return 0 if result.wasSuccessful() and not result.skipped else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = (args.out or ROOT.parent / "acceptance" / ("full-" + stamp)).resolve()
    out.mkdir(parents=True, exist_ok=False)
    fixture_home = out / "isolated-home"
    fixture_home.mkdir()
    empty = fixture_home / "empty-gitconfig"
    empty.write_text("", encoding="utf8")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HARNESS_") and not k.startswith("PYTHON")
    }
    deps = os.environ.get("HARNESS_TEST_DEPENDENCIES", "")
    env.update(
        HARNESS_TEST_DEPENDENCIES=deps,
        PYTHONNOUSERSITE="1",
        PYTHONPATH=os.pathsep.join([str(ROOT), deps]) if deps else str(ROOT),
        PYTHONIOENCODING="utf-8",
        HARNESS_DATA=str(fixture_home / "data"),
        HARNESS_MODELS=str(fixture_home / "no-models.json"),
        HARNESS_CREDS=str(fixture_home / "no-credentials.env"),
        HARNESS_HOOKS=str(fixture_home / "no-hooks.json"),
        HARNESS_BASELINE_HITS=str(fixture_home / "hits.jsonl"),
        HARNESS_BASE_URL="http://127.0.0.1:9/v1",
        HARNESS_API_KEY="",
        HARNESS_MODEL="offline-unconfigured",
        HARNESS_PROXY="",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=str(empty),
    )
    for name in ("HARNESS_GIT", "HARNESS_PYTHON"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    # Fail rather than silently skipping native/image/document capabilities.
    check = "import winpty,debugpy,PIL,pypdf;print('all-required-dependencies-present')"
    dependency_env = {**env, "PYTHONPATH": os.pathsep.join([str(ROOT), deps])}
    subprocess.run([sys.executable, "-c", check], env=dependency_env, check=True)
    modules = ["tools." + p.stem for p in sorted((ROOT / "tools").glob("test_*.py"))]
    results = []
    for module in modules:
        start = time.monotonic()
        log = out / (module.split(".")[-1] + ".log")
        with log.open("w", encoding="utf8") as stream:
            try:
                run = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--worker", module],
                    cwd=ROOT,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=1200,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
                code = run.returncode
            except subprocess.TimeoutExpired:
                code = 124
        result = dict(
            module=module,
            exitCode=code,
            seconds=round(time.monotonic() - start, 3),
            log=log.name,
        )
        results.append(result)
        (out / "results.json").write_text(
            json.dumps(
                dict(
                    kind="offline-regression",
                    modules=results,
                    realModelCalls=0,
                    humanExperience="not-assessed",
                ),
                indent=2,
            ),
            encoding="utf8",
        )
        print(json.dumps(result), flush=True)
    return 1 if any(r["exitCode"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
