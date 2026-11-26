#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""facts.py —— 事实区：这个项目里"是什么、在哪"，agent 自己攒的。

三层记忆的第三层（2026-10-26 用户定）
    习惯区（跨项目，人写）· 规则区（本项目，人 + distill）· **事实区（这里）**

★ 它和另外两层的根本区别：**写的人不一样**
    习惯区和规则区是人定的；事实区是那个子 agent 自己在跑的时候写的。
    所以这一层的风险不是"写少了"，是"写多了而且没人清理"。

★ 写入（2026-10-26 下半场开的）
    先只读跑了一版让人看见它会写成什么样，然后按 `memory_tiers` 的 `facts=edit`
    才开写。开之前定的三条：只写 `MEMORY.md` 一个文件、每条带日期且去重、
    到上限就拒收并明说（不悄悄截断）。细节见 `append()` 上面那段。

★ 三个作用域照 Claude 的分法（`memory:` 字段取 user / project / local）
    user     ~/.harness/agent-memory/<类型>/
    project  <repo>/.harness/agent-memory/<类型>/      ← 推荐默认，可进版本控制
    local    <repo>/.harness/agent-memory-local/<类型>/

★ 一个刻意的取舍：**只注入 MEMORY.md，不注入全目录**
    照 Claude 的口径（前 200 行 / 25KB）。其余文件只列**名字**，让 agent 自己 read_file。
    全量注入的后果是"记忆一多就把主语撑爆"，而撑爆之后它连任务本身都记不住了。
"""
# 主要引用方包括 `tools.py` 和 `memory_service.py`。
from __future__ import annotations

import os
import time
from pathlib import Path

from . import settings as SET

SCOPES = ("user", "project", "local")
ENTRY = "MEMORY.md"          # 只注入这一个文件的头部


def root_of(scope: str, kind: str, repo_root: Path | None = None) -> Path:
    scope = scope if scope in SCOPES else "project"
    if scope == "user":
        base = Path(os.environ.get("HARNESS_FACTS")
                    or (Path.home() / ".harness" / "agent-memory"))
        return base / kind
    name = "agent-memory" if scope == "project" else "agent-memory-local"
    return Path(repo_root or ".") / ".harness" / name / kind


def _lines_head(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """取前 N 行或前 M 字节，先到先算。"""
    out = []
    used = 0
    cut = False
    for i, ln in enumerate(text.splitlines()):
        if i >= max_lines or used + len(ln) + 1 > max_bytes:
            cut = True
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out), cut


def read(scope: str, kind: str, repo_root: Path | None = None,
         cfg: dict | None = None) -> dict:
    """读一个类型的记忆目录。返回 {path, exists, text, files, stale_days, cut, note}。

    ★ 过期用**文件时间**判断，不猜内容：我们不知道每一条是何时写的，
      但知道这个目录多久没动过。90 天没动 ⇒ 注入时挂一句"可能已过期"，
      而不是假装它还是新的。
    """
    cfg = SET.memory() if cfg is None else cfg
    cap_files = int(cfg.get("facts_max_files") or SET.DEFAULT_MEMORY["facts_max_files"])
    cap_chars = int(cfg.get("facts_max_chars") or SET.DEFAULT_MEMORY["facts_max_chars"])
    ttl = int(cfg.get("facts_ttl_days") or SET.DEFAULT_MEMORY["facts_ttl_days"])

    d = root_of(scope, kind, repo_root)
    out = {"path": str(d), "scope": scope, "kind": kind, "exists": d.is_dir(),
           "text": "", "files": [], "listed": 0, "cut": False,
           "stale_days": 0, "note": ""}
    if not out["exists"]:
        return out
    try:
        allf = sorted(p for p in d.iterdir() if p.is_file())
    except OSError:
        return out
    out["listed"] = len(allf)
    out["files"] = [p.name for p in allf[:cap_files]]
    if len(allf) > cap_files:
        out["cut"] = True
        out["note"] = ("这个目录有 %d 个文件，只列了前 %d 个 —— 该整理了"
                       % (len(allf), cap_files))

    entry = d / ENTRY
    if entry.exists():
        try:
            raw = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        body = raw.replace("\r\n", "\n").strip()
        # 行数上限按 200 行算，字节上限来自配置（默认 25KB）
        body, cut = _lines_head(body, 200, cap_chars)
        out["text"] = body
        if cut and not out["note"]:
            out["cut"] = True
            out["note"] = "MEMORY.md 太长，只注入了开头（该整理了）"
        try:
            age = int((time.time() - entry.stat().st_mtime) / 86400)
        except OSError:
            age = 0
        out["stale_days"] = age
        if ttl and age > ttl:
            out["note"] = (out["note"] + "；" if out["note"] else "") + \
                "这个目录 %d 天没动过，里面的事实可能已经过期" % age
    return out


def block(scope: str, kind: str, repo_root: Path | None = None, record=None) -> str:
    """拼成要注入系统提示的那一段。没有记忆就返回空串（不占预算）。"""
    r = read(scope, kind, repo_root) if record is None else record
    if not r["exists"]:
        return ""
    parts = ["## 你的记忆（这个项目里的事实）",
             "目录：%s" % r["path"]]
    if r["text"]:
        parts.append(r["text"])
    else:
        parts.append("（还没有 MEMORY.md。你看到的事实可以用 remember 记下来 ——"
                     " 记「在哪、是什么、哪一行」，不要记「应该怎么做」。）")
    if r["files"]:
        parts.append("目录里还有这些文件（要细节自己 read_file）：" +
                     "、".join(r["files"][:40]))
    if r["note"]:
        parts.append("⚠ " + r["note"])
    parts.append("★ 这是**事实**（在哪、是什么），不是规则。"
                 "规则由人定，别把「应该怎么做」写进这里。")
    return "\n".join(parts)


# ───────────────────── 写入（2026-10-26 开的 edit 档） ─────────────────────
# ★ 为什么现在能开：写入的风险不是"写少了"，是"写多了而且没人清理"。
#   所以开的时候同时定了三件事：
#     ① 只写 `MEMORY.md` 一个文件（不是随便往目录里扔文件）；
#     ② 每条带时间戳，并且**同一条不许写第二遍**（去重，防同一句事实刷屏）；
#     ③ 到上限就**拒收并明说**，不悄悄截断 —— 悄悄截断等于人以为记下了。

HEADER = """# 事实（agent 自己记的）

> 这一层写的是"这个项目里是什么、在哪"，不是"应该怎么做"。
> 每条带日期。过期的直接删 —— 留着比删掉更贵：它会被注入，然后被当成现在的事实。

"""


def _stamp() -> str:
    return time.strftime("%Y-%m-%d")


def append(scope: str, kind: str, text: str, repo_root: Path | None = None,
           cfg: dict | None = None) -> dict:
    """往这个类型的 `MEMORY.md` 末尾记一条事实。

    返回 {ok, path, note, dup, chars}。**不做隐式修正**：
    太长、重复、到上限，一律回 `ok=False` 并说清原因。
    """
    cfg = SET.memory() if cfg is None else cfg
    cap = int(cfg.get("facts_max_chars") or SET.DEFAULT_MEMORY["facts_max_chars"])
    t = " ".join(str(text or "").split()).strip()
    if len(t) < 4:
        return {"ok": False, "error": "这条太短了（%d 字）—— 写一条能当证据用的完整句子"
                                      % len(t)}
    if len(t) > 600:
        return {"ok": False, "error": "这条太长了（%d 字，上限 600）—— "
                                      "事实要一句话能读完，长的写成文件再记路径" % len(t)}
    d = root_of(scope, kind, repo_root)
    entry = d / ENTRY
    try:
        d.mkdir(parents=True, exist_ok=True)
        raw = entry.read_text(encoding="utf-8") if entry.exists() else HEADER
    except OSError as e:
        return {"ok": False, "error": "读不到这个记忆目录：%s" % e}
    body = raw.replace("\r\n", "\n")
    if t in body:
        return {"ok": True, "dup": True, "path": str(entry), "chars": len(body),
                "note": "这条已经在里面了，没重复写"}
    line = "- %s %s" % (_stamp(), t)
    # noqa: R4 —— 改的是 markdown 事实文件，不是 Python 源码：对它 ast.parse 没有意义。
    #   这一档的风险不是"改坏了编译不过"，是"写重了/写超了"，所以那两件事在这函数里
    #   各有自己的判据（`if t in body` 去重、`cap` 到顶拒收）。
    if len(body) + len(line) + 1 > cap:
        return {"ok": False,
                "error": "这个记忆文件已经 %d 字了（上限 %d）—— 先删掉过期的再记，"
                         "不然它长到撑爆上下文" % (len(body), cap)}
    try:
        entry.write_text(body.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": "写不进这个记忆文件：%s" % e}
    return {"ok": True, "path": str(entry), "chars": len(body) + len(line) + 1,
            "note": "记下了（%d 字 / 上限 %d）" % (len(body) + len(line), cap)}



# ───────────────────── 收口：看得见每一条，能删、能改、能清过期 ─────────────────────
# ★ 为什么必须有这一段（2026-10-26 用户：「先把新开的口子收口了」）
#   只追加的一层记忆，用两周就会变成"一堆没人敢删的句子"：它每条都进注入，
#   而注入里没有"这条是三个月前的"这种标记，模型会把它当**现在的事实**用。
#   所以收口不是"加个管理界面"，是三件事：**能看见每一条**（不是只有文件）、
#   **能一条一条删**（不是只能整文件清空）、**过期能被指出来**（不是靠人翻文件）。

_LINE = None


def _line_re():
    global _LINE
    if _LINE is None:
        import re
        _LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (.*)$")
    return _LINE


def _split(entry: Path):
    """把 MEMORY.md 切成人能处理的形状：[(行号, 日期, 正文, 距今几天)] + 原始行。"""
    try:
        raw = entry.read_text(encoding="utf-8") if entry.exists() else ""
    except OSError:
        return [], []
    lines = raw.replace("\r\n", "\n").split("\n")
    out = []
    now = time.time()
    n = 0
    for i, ln in enumerate(lines):
        m = _line_re().match(ln)
        if not m:
            continue
        n += 1
        try:
            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
            days = max(0, int((now - t) / 86400))
        except Exception:
            days = 0
        out.append({"n": n, "i": i, "date": m.group(1),
                    "text": m.group(2), "days": days})
    return out, lines


def entries(scope: str, kind: str, repo_root: Path | None = None) -> dict:
    """列出这个类型的每一条事实。`n` 是**从 1 开始的序号**（删/改都按它）。"""
    d = root_of(scope, kind, repo_root)
    entry = d / ENTRY
    items, _ = _split(entry)
    try:
        chars = entry.stat().st_size if entry.exists() else 0
    except OSError:
        chars = 0
    return {"path": str(entry), "exists": entry.exists(),
            "items": items, "count": len(items), "bytes": chars}


def _write(entry: Path, lines: list[str]) -> dict:
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": "写不进这个记忆文件：%s" % e}
    return {"ok": True, "path": str(entry)}


def _guard(entry: Path, n: int, repo_root):
    """公共前置：文件得在、第 n 条得在、改之前留一份备份。

    ★ 备份不成就不动（和 `edit` 档同一条取舍）：事实区能被删，就必须能捡回来。
      没有这一条，"删错一条"就是永久损失 —— 而它恰好是最容易发生的操作。
    """
    from . import memperms as MP

    items, lines = _split(entry)
    if not entry.exists():
        return [], [], "", {"ok": False, "error": "这个类型还没有记忆文件"}
    if not (1 <= int(n) <= len(items)):
        return [], [], "", {"ok": False,
                            "error": "没有第 %s 条（现在一共 %d 条）" % (n, len(items))}
    bak = MP.backup("facts", entry, repo_root)
    if not bak:
        return [], [], "", {"ok": False, "error": "没法先备份，所以没动它"}
    return items, lines, bak, None


def drop(scope: str, kind: str, n: int, repo_root: Path | None = None) -> dict:
    """删掉第 n 条。改前备份，改后记审计（`region=facts`）。"""
    from . import memperms as MP

    d = root_of(scope, kind, repo_root)
    entry = d / ENTRY
    items, lines, bak, err = _guard(entry, n, repo_root)
    if err:
        return err
    it = items[int(n) - 1]
    del lines[it["i"]]
    r = _write(entry, lines)
    if not r.get("ok"):
        return r
    MP.log("facts", "drop", it["text"][:60], "界面里删掉的一条事实",
           repo_root=repo_root, src=str(entry), bak=bak)
    return {"ok": True, "dropped": it["text"], "left": len(items) - 1,
            "note": "删了（备份和留痕都在右边）"}


def edit_entry(scope: str, kind: str, n: int, text: str,
               repo_root: Path | None = None) -> dict:
    """改第 n 条的正文。日期不动（它记的是"这条什么时候知道的"）。"""
    from . import memperms as MP

    t = " ".join(str(text or "").split()).strip()
    if len(t) < 4:
        return {"ok": False, "error": "改完太短了（%d 字）" % len(t)}
    if len(t) > 600:
        return {"ok": False, "error": "改完太长了（%d 字，上限 600）" % len(t)}
    d = root_of(scope, kind, repo_root)
    entry = d / ENTRY
    items, lines, bak, err = _guard(entry, n, repo_root)
    if err:
        return err
    it = items[int(n) - 1]
    lines[it["i"]] = "- %s %s" % (it["date"], t)
    r = _write(entry, lines)
    if not r.get("ok"):
        return r
    MP.log("facts", "edit", t[:60], "界面里改的一条事实",
           repo_root=repo_root, src=str(entry), bak=bak)
    return {"ok": True, "text": t, "note": "改好了"}


def prune(scope: str, kind: str, days: int | None = None,
          repo_root: Path | None = None) -> dict:
    """删掉"超过 N 天"的那些条目。**不自动跑** —— 只有人按了那个按钮才跑。

    ★ 为什么默认阈值是配置里的 `facts_ttl_days`（90）：那是注入时挂"可能已过期"
      的同一个数。两个数不一样的话，会出现"界面说该清了、注入说还新"的荒唐局面。
    """
    from . import memperms as MP
    from . import settings as SET

    cfg = SET.memory()
    lim = int(days if days is not None else
              (cfg.get("facts_ttl_days") or SET.DEFAULT_MEMORY["facts_ttl_days"]))
    if lim < 1:
        return {"ok": False, "error": "天数得是正数（清掉超过 %s 天的）" % lim}
    d = root_of(scope, kind, repo_root)
    entry = d / ENTRY
    items, lines = _split(entry)
    if not entry.exists():
        return {"ok": False, "error": "这个类型还没有记忆文件"}
    old = [x for x in items if x["days"] > lim]
    if not old:
        return {"ok": True, "removed": 0, "day": lim,
                "note": "没有超过 %d 天的条目，什么都没动" % lim}
    bak = MP.backup("facts", entry, repo_root)
    if not bak:
        return {"ok": False, "error": "没法先备份，所以没清"}
    kill = {x["i"] for x in old}
    keep = [ln for i, ln in enumerate(lines) if i not in kill]
    r = _write(entry, keep)
    if not r.get("ok"):
        return r
    MP.log("facts", "prune", "清掉 %d 条（>%d 天）" % (len(old), lim),
           "界面里清的过期事实", repo_root=repo_root, src=str(entry), bak=bak)
    return {"ok": True, "removed": len(old), "day": lim, "left": len(items) - len(old),
            "note": "清掉 %d 条（超过 %d 天），备份在 mem-backups/" % (len(old), lim)}

