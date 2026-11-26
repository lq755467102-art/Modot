"""Own a gated Windows job; no command runs before family ownership exists."""

# 主要引用或加载入口包括 `loop/workbench.py` 和 `harness_core.py`。

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from loop.process_family import Family
from loop import runtime as RT
from loop import core_runtime
from loop.filelock import atomic_json


def member(config):
    if sys.stdin.buffer.read(1) != b"G":
        return 125
    cfg = json.loads(Path(config).read_text(encoding="utf8"))
    if cfg["mode"] == "server":
        if os.environ.get("HARNESS_COMPILED_CORE"):
            from bin.server import main as serve

            return serve(cfg["args"])
        sys.path.extend(
            x for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x
        )
        sys.argv = [str(ROOT / "bin/server.py"), *cfg["args"]]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    console = (
        open("CONIN$", "rb", buffering=0)
        if cfg.get("tty") and os.name == "nt"
        else subprocess.DEVNULL
    )
    try:
        deadline = cfg.get("startDeadline")
        if deadline is not None and time.monotonic() >= deadline:
            return 124
        return subprocess.Popen(cfg["argv"], cwd=cfg["cwd"], stdin=console).wait()
    finally:
        if hasattr(console, "close"):
            console.close()


def controller(config, server=False):
    cfg = json.loads(Path(config).read_text(encoding="utf8"))
    if os.name != "nt":
        raise RuntimeError("当前进程树所有权控制器仅支持 Windows")
    stopped = threading.Event()
    if server:

        def control():
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
            stopped.set()

        threading.Thread(target=control, daemon=True).start()
    process = subprocess.Popen(
        core_runtime.command("owned-process.py", "--member", config, isolated=True),
        stdin=subprocess.PIPE,
    )
    family = None
    code = 125
    confirmed = False
    try:
        family = Family(process)
        ownership = dict(
            nonce=cfg["nonce"],
            controllerPid=os.getpid(),
            controllerIdentity=RT.identity(os.getpid()),
            memberPid=process.pid,
            memberIdentity=RT.identity(process.pid),
            family="windows_job",
        )
        atomic_json(cfg["readyFile"], ownership)
        if cfg["mode"] == "command" and "permissionRevision" in cfg:
            from loop import permissions
            selected = permissions.profile(cfg.get('sessionId'))
            if selected["revision"] != cfg["permissionRevision"]:
                raise PermissionError("宿主执行授权已变化，未放行命令")
            if cfg.get("hostGrant"):
                permissions.redeem_shell(cfg)
            elif selected["sandboxMode"] != "danger-full-access":
                raise PermissionError("未开启本机完全访问")
        if server:
            print("HARNESS_OWNERSHIP " + json.dumps(ownership), flush=True)
        process.stdin.write(b"G")
        process.stdin.flush()
        process.stdin.close()
        while process.poll() is None:
            if stopped.wait(0.05):
                break
            deadline = cfg.get("startDeadline")
            if deadline is not None and time.monotonic() >= deadline:
                code = 124
                break
            if (
                not server
                and cfg.get("ownerPid")
                and RT.identity(cfg["ownerPid"]) != cfg.get("ownerIdentity")
            ):
                break
            try:
                stop = json.loads(Path(cfg["stopFile"]).read_text(encoding="utf8"))
                if stop.get("stop") and stop.get("nonce") == cfg["nonce"]:
                    break
            except (OSError, ValueError):
                pass
        else:
            code = process.returncode
    finally:
        if family:
            family.close()
            confirmed = True
        elif process.poll() is None:
            process.kill()  # Original Popen handle; member has not received gate.
        process.wait(timeout=8)
        atomic_json(
            cfg["exitFile"],
            dict(
                nonce=cfg["nonce"],
                exitCode=code,
                familyTerminationConfirmed=confirmed,
                finishedAt=time.time(),
            ),
        )
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--member")
    parser.add_argument("--config")
    parser.add_argument("--server", action="store_true")
    args, rest = parser.parse_known_args()
    if args.member:
        return member(args.member)
    if args.server:
        from loop.paths import data_root

        directory = data_root() / "runs" / "desktop-owners" / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        args.config = str(directory / "config.json")
        atomic_json(
            args.config,
            dict(
                mode="server",
                args=rest,
                nonce=uuid.uuid4().hex,
                readyFile=str(directory / "ownership.json"),
                stopFile=str(directory / "stop.json"),
                exitFile=str(directory / "exit.json"),
            ),
        )
    return controller(args.config, args.server)


if __name__ == "__main__":
    sys.exit(main())
