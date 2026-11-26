#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基线体检：开工前留的指纹，能不能准确回答「这一轮改了什么」。

为什么要它
    改动清单一旦不准，本我就不是在审代码，而是在审噪声。两种情况都会毁掉它：
    报了一堆没改的（`touch` 一下就报）；漏了真改的（大文件只比对大小）。
    所以这五条要一条条钉住。

判据
    B1 排除噪声：`.git` / `node_modules` / 会话所在的 `runs/` 都不进单子
    B2 真改动被检出（按内容哈希，不按时间戳）
    B3 只动 mtime、内容没变 ⇒ **不算改**
    B4 新增与删除各算对
    B5 超过 1MB 的文件只比大小，并在单子上标明「没比对内容」

用法：python bin/probe-baseline.py [-v]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import snapshot as SNAP      # noqa: E402

CASES = []


def ck(name, cond, detail=""):
    CASES.append((name, bool(cond)))
    # 只在失败时打细节：通过时也打，会让人以为那句在报错（自己踩过）
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("  —— " + detail) if (detail and not cond) else ""))


def build():
    root = Path(tempfile.mkdtemp(prefix="base_probe_"))
    sess = root / "runs" / "sessions" / "app-1"
    sess.mkdir(parents=True)
    (root / "code.py").write_text("v1", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("noise", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (root / "runs" / "locks").mkdir(parents=True)
    (root / "runs" / "locks" / "a.json").write_text("{}", encoding="utf-8")
    big = root / "big.bin"
    big.write_bytes(b"x" * (SNAP.BASE_HASH_MAX + 10))
    return root, sess


def main() -> int:
    verbose = "-v" in sys.argv
    root, sess = build()

    b = SNAP.baseline(root, sess)
    ck("B0 基线能建起来", b.get("ok") is True and b.get("files", 0) >= 3,
       "files=%s" % b.get("files"))

    # B1 噪声
    idx = (sess / "base" / "index.json").read_text(encoding="utf-8")
    ck("B1 噪声不进基线（.git / node_modules / runs）",
       "node_modules" not in idx and ".git/" not in idx and "runs/locks" not in idx,
       "基线里混进了噪声目录")

    # B2 真改动
    (root / "code.py").write_text("v2", encoding="utf-8")
    # B3 只动时间
    (root / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    os.utime(root / "sub" / "b.txt", (time.time() + 30, time.time() + 30))
    # B4 增 / 删
    (root / "new.md").write_text("hello", encoding="utf-8")
    (root / "sub" / "b.txt").unlink()
    # B5 大文件改大小
    (root / "big.bin").write_bytes(b"y" * (SNAP.BASE_HASH_MAX + 99))
    # 产物区继续变，但不该进单子
    (root / "runs" / "locks" / "a.json").write_text('{"x":1}', encoding="utf-8")

    c = SNAP.changes(root, sess)
    paths_c = [x["path"] for x in c.get("changed") or []]
    paths_a = [x["path"] for x in c.get("added") or []]
    paths_r = [x["path"] for x in c.get("removed") or []]

    ck("B2 真改动被检出", "code.py" in paths_c, str(paths_c))
    ck("B3 只动时间不算改", "sub/b.txt" not in paths_c, "把 touch 当成了改")
    ck("B4 新增/删除各算对", paths_a == ["new.md"] and "sub/b.txt" in paths_r,
       "added=%s removed=%s" % (paths_a, paths_r))
    big = next((x for x in c.get("changed") or [] if x["path"] == "big.bin"), None)
    ck("B5 大文件只比大小且标明 stat_only", bool(big and big.get("stat_only")),
       str(big))
    ck("B1b 产物区的变化不进单子", not any("runs/" in x for x in paths_c + paths_a),
       str(paths_c + paths_a))

    # ── L2：真 diff ──
    diffs = {d["path"]: d for d in (c.get("diffs") or [])}
    d1 = diffs.get("code.py") or {}
    ck("D1 改动的文件给出**真实差异**（含被改的那行）",
       "+v2" in (d1.get("text") or "") and "-v1" in (d1.get("text") or ""),
       str(d1)[:160])
    d2 = diffs.get("new.md") or {}
    ck("D2 新增的文件给全加（+n）", d2.get("add", 0) >= 1 and d2.get("del", 0) == 0,
       str(d2)[:120])
    d3 = diffs.get("sub/b.txt") or {}
    ck("D3 删除的文件给全减（-n）", d3.get("del", 0) >= 1,
       "删除的文件没进差异单（第一版就这样漏了）")
    ck("D4 大文件/二进制不出差异，但要**标明看不到**（不等于没改）",
       "big.bin" not in diffs and "big.bin" in (c.get("stat_only") or []),
       str(c.get("stat_only"))[:120])

    if verbose:
        print("\n完整单子：" + str(c))
    bad = [x for x in CASES if not x[1]]
    print("%d/%d" % (len(CASES) - len(bad), len(CASES)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
