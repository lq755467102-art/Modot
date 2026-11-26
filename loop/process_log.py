"""Bounded diagnostic stdout. The event journal remains the audit authority."""

# 主要引用方包括 `bin/server.py`。

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
from .filelock import atomic_json, read_text

LIMIT = 32 * 1024 * 1024


def status(log):
    marker = Path(str(log) + ".capacity.json")
    try:
        return json.loads(read_text(marker))
    except FileNotFoundError:
        return dict(truncated=False)
    except (OSError, ValueError):
        return dict(truncated=True, notice="诊断日志容量回执不可读，完整性未知")


def spawn(argv, *, cwd, log, env=None):
    path = Path(log)
    # Open before spawning: failure must not create an unrecorded process.
    stream = path.open("ab")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except BaseException:
        stream.close()
        raise

    def drain():
        try:
            size = stream.tell()
            marked = Path(str(path) + ".capacity.json").exists()
            while True:
                data = process.stdout.read1(65536)
                if not data:
                    break
                room = max(0, LIMIT - size)
                if len(data) > room and not marked:
                    atomic_json(
                        Path(str(path) + ".capacity.json"),
                        dict(
                            truncated=True,
                            limitBytes=LIMIT,
                            notice="诊断日志达到32MiB上限，后续stdout未保留；请以机器事件及工具输出核对实际状态",
                        ),
                    )
                    marked = True
                if room:
                    retained = data[:room]
                    stream.write(retained)
                    stream.flush()
                    size += len(retained)
        except Exception:
            # Only the original Popen handle is controlled on a logging failure.
            # Closing the pipe without stopping could leave a blocked child.
            try:
                process.kill()
            except OSError:
                pass
            try:
                atomic_json(
                    Path(str(path) + ".capacity.json"),
                    dict(
                        truncated=True,
                        notice="诊断日志写入失败，已终止自有进程；实际工具影响需核对",
                    ),
                )
            except OSError:
                pass
        finally:
            stream.close()
            process.stdout.close()

    thread = threading.Thread(target=drain, daemon=True, name="bounded-process-log")
    process._harness_log_thread = thread
    thread.start()
    return process
