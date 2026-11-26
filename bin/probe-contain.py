#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器档接进执行层之后，逐条验「它真的挡住/放行了什么」。

要证明的四件事（缺一条就说明接线是假的）：
  ① 命令确实**走容器这条路**跑起来的（看 `isolate._STATUS` 的计数，不看印象）；
  ② 容器里的命令能读到**已授权**的工作区；
  ③ 容器里的命令读不到**没授权**的目录（这是隔离的存在证明）；
  ④ 收回授权之后，连工作区也读不到（说明起作用的是那几条 ACL，不是别的东西）。

★ 只在临时目录里折腾。开工前把 ACL 授上、收尾一定收回 ——
  改的是用户磁盘上的权限，留下垃圾比不做还糟。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import contain as CN      # noqa: E402
from loop import isolate as ISO     # noqa: E402
import subprocess                   # noqa: E402

FAILS, PASSES = [], []


def say(ok, name, detail=""):
    (PASSES if ok else FAILS).append((name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  —— " + detail) if detail else ""),
          flush=True)


def run_cmd(argv, cwd):
    """走 isolate.popen（也就是**日常执行的那条路**），拿 (rc, out, err)。"""
    import time
    n0 = int(ISO._STATUS.get("container") or 0)
    p = ISO.popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  env=dict(os.environ))
    used = int(ISO._STATUS.get("container") or 0) > n0
    t0 = time.time()
    while True:
        try:
            out, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.time() - t0 > 30:
                try:
                    p.kill()
                except Exception:
                    pass
                return {"rc": 124, "out": "", "err": "超时", "container": used}
    def _txt(x):
        if isinstance(x, bytes):
            return x.decode("utf-8", "replace")
        return x or ""

    return {"rc": p.returncode, "out": _txt(out), "err": _txt(err), "container": used}


def main():
    if not CN.supported():
        print("这个平台没有 AppContainer，跳过")
        return 0

    # ★ 探针里把两处开关直接打开：真配置在 harness/charter.json，
    #   探针不该去改它（验收会读那份文件）。这里只验接线通不通。
    CN.enabled = lambda: True
    ISO.enabled = lambda: True
    ISO._cfg = lambda: {"enabled": True}

    work = Path(tempfile.mkdtemp(prefix="contain_work_"))
    outside = Path(tempfile.mkdtemp(prefix="contain_outside_"))
    (work / "inside.txt").write_text("inside-workspace", encoding="utf-8")
    (outside / "secret.txt").write_text("outside-workspace", encoding="utf-8")
    ok_cleanup = False
    try:
        print("\n[一] 授权 + 解释器", flush=True)
        g = CN.ensure_grants([str(work)])
        say(g.get("ok") is True, "工作区授权成功",
            "sid=%s 授了 %d 个目录" % (str(g.get("sid"))[:18] + "…", len(g.get("granted") or [])))
        ip = CN.ensure_interpreter(deep=False)
        # ★ 这条现在是**记录事实**，不是要求它成功：只授解释器目录时 python 仍起不来
        #   （0xC0000135）。于是定下的边界是「python 类命令不进容器」。
        #   哪天这条变成能起来（比如解释器随包放到浅路径），这条就会红 ——
        #   那正是该回来看的时候：把它变成能进，比留着一条静默的例外好。
        say(ip.get("rc") == -1073741515,
            "已知边界：只授解释器目录时 python 仍起不来（0xC0000135）",
            "rc=%s" % ip.get("rc"))
        print("        · 所以 python 类命令默认不进容器；其余命令照常进（下一条验）", flush=True)

        print("\n[二] 走日常执行那条路跑命令", flush=True)
        r1 = run_cmd(["whoami.exe"], cwd=str(work))
        say(r1["container"] is True, "命令确实走了容器这条路（不是静默降级）",
            "container 计数 +1")
        say(r1["rc"] == 0 and "administrator" in (r1["out"] or "").lower(),
            "容器里起进程、out 也被接回来了", "rc=%s out=%r" % (r1["rc"], (r1["out"] or "")[:30]))

        print("\n[三] 已授权的工作区：读得到", flush=True)
        r2 = run_cmd(["certutil.exe", "-hashfile", str(work / "inside.txt"), "MD5"],
                     cwd=str(work))
        say(r2["rc"] == 0, "容器里读得到工作区里的文件", "rc=%s" % r2["rc"])

        print("\n[四] 没授权的目录：读不到（隔离的存在证明）", flush=True)
        r3 = run_cmd(["certutil.exe", "-hashfile", str(outside / "secret.txt"), "MD5"],
                     cwd=str(work))
        say(r3["rc"] != 0, "容器里读不到没授权的目录",
            "rc=%s err=%r" % (r3["rc"], (r3["err"] or "")[:60]))

        print("\n[五] 收回授权之后，工作区也读不到", flush=True)
        rv = CN.revoke_grants()
        ok_cleanup = True
        say(rv.get("ok") is True, "授权收回成功", "收回 %d 个目录" % len(rv.get("removed") or []))
        r4 = run_cmd(["certutil.exe", "-hashfile", str(work / "inside.txt"), "MD5"],
                     cwd=str(work))
        say(r4["rc"] != 0, "收回之后读不到了 —— 起作用的是那几条 ACL",
            "rc=%s" % r4["rc"])

        print("\n[六] 边界：python 类命令应当**明确不进容器**，但仍能跑", flush=True)
        n_py = int(ISO._STATUS.get("py_skipped") or 0)
        r5 = run_cmd([sys.executable, "-c", "print('py-still-runs')"], cwd=str(work))
        say(int(ISO._STATUS.get("py_skipped") or 0) > n_py,
            "python 命令被识别出来、没有硬塞进容器（否则会 0xC0000135）",
            "py_skipped +1")
        say(r5["rc"] == 0 and "py-still-runs" in (r5["out"] or ""),
            "退回 job-only 之后照样跑得通", "rc=%s" % r5["rc"])
    finally:
        if not ok_cleanup:
            CN.revoke_grants()
        shutil.rmtree(str(work), ignore_errors=True)
        shutil.rmtree(str(outside), ignore_errors=True)

    print("\n" + "=" * 56, flush=True)
    print("通过 %d 条，失败 %d 条" % (len(PASSES), len(FAILS)), flush=True)
    for n, d in FAILS:
        print("  ✗ %s%s" % (n, ("（%s）" % d) if d else ""), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
