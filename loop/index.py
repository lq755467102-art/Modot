#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index.py —— 代码库索引：别再靠一趟趟翻文件

为什么要有这一层
    现在的"找东西"是：子 agent 一个目录一个目录看、一个文件一个文件读。
    同一件事问三遍就是翻三遍，而且**每次翻都花掉一整轮的上下文**。
    索引不是"更聪明的搜索"，是**把翻的动作摊到一次**：建一次，之后按词查。

它存什么
    · 文件：路径、大小、行数、mtime、内容哈希
    · 片段：按行切块（每块 ~40 行），**入库前先过 `leaks.redact`**
    · 符号：`.py` 用 `ast` 取函数/类；`.md` 取标题；其它用一层保守的正则

它存在哪
    分离的数据根使用 `.harness/code-index/`。从源码运行且程序根与数据根相同时，
    使用当前用户的 Modot 缓存目录。`HARNESS_INDEX_DIR` 可另行指定缓存位置。
    理由：索引是"这台机器对这个项目的看法"，写进项目等于往别人的仓库里扔一个
    二进制、还会被提交上去；而它随时可以重建，所以不该跟项目走。

★ 为什么入库前要脱敏
    索引会把文件内容**复制一份**到数据库里。不脱敏的话，等于把 key 又抄了一遍，
    抄进一个谁都想不到的地方（`.harness/code-index/xxx.db`）。
    而且这一层比读文件更隐蔽：读文件至少有 note 提示"这里遮过"。

★ 它明确不做
    · 不做语义搜索（没有向量、没有 embedding）—— 那是另一个量级的依赖，
      而"按词找代码"已经解决 80% 的"东西在哪"。
    · 不索引二进制、不索引 `.git` / `node_modules` / `dist` / `runs`。
    · 不保证实时：改了文件要重新 `build`（增量，只重算变过的）。
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from . import paths as P
from . import leaks as _LKS

# 别往里走：这些目录里的人不会想被索引，而且量极大
SKIP_DIRS = {".git", ".hg", ".svn", ".harness", ".scratch", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", "runs", ".idea", ".vscode",
             "site-packages", ".mypy_cache", ".pytest_cache", "vendor"}
# 二进制 / 生成物：按扩展名挡（读进来也是乱码，还会把库撑大）
SKIP_EXT = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".gz", ".tar",
            ".7z", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
            ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".otf", ".pak", ".db",
            ".sqlite", ".lock", ".map", ".min.js", ".min.css"}
MAX_FILE = 512 * 1024        # 单文件上限：超过就不索引（多半是生成物或数据）
CHUNK_LINES = 40             # 每块多少行
MAX_FILES = 20000            # 整个库的上限，防止误指到 C:\ 把磁盘写满
PRIVATE_FILES = {"api-token", "credentials.json", "models.json", ".env", "habits.md", "memory.md"}


def _root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]


def db_path(root) -> Path:
    """索引库落在哪。`HARNESS_INDEX_DIR` 可覆盖（探针用）。"""
    env = os.environ.get("HARNESS_INDEX_DIR")
    if env:
        base = Path(env)
    elif P.data_root().resolve() == P.app_root().resolve():
        cache = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
        base = (Path(cache) if cache else Path.home() / ".cache") / "Modot" / "code-index"
    else:
        base = P.data_root() / ".harness" / "code-index"
    return base / ("v2-%s.db" % _root_key(Path(root).resolve()))


def _conn(root) -> sqlite3.Connection:
    p = db_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS files("
              "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, lines INTEGER,"
              " sha TEXT, at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS chunks("
              "path TEXT, start INTEGER, text TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS symbols("
              "path TEXT, name TEXT, kind TEXT, line INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_symbols_path ON symbols(path)")
    # ★ meta 建在 FTS 之前（2026-10-30）：下面要用它记「这个库是给哪个目录建的」。
    #   原来 meta 只在 FTS5 可用时才建，没有 FTS 的机器上就没有这张表，
    #   于是「这个库是谁的」无从查起 —— 回收也就无从做起。
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    try:
        if not c.execute("SELECT 1 FROM meta WHERE k='root'").fetchone():
            c.execute("INSERT INTO meta(k, v) VALUES('root', ?)",
                      (str(Path(root).resolve()),))
            c.commit()
    except sqlite3.Error:
        pass                            # 记不上不影响索引本身，别因此把搜索弄挂
    # FTS5：有就用（快且能出高亮片段），没有就退回 LIKE
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                  "text, path UNINDEXED, start UNINDEXED, tokenize='unicode61')")
    except sqlite3.OperationalError:
        pass                         # 这个 python 编进去的 sqlite 没有 FTS5
    return c


_PRUNE_EVERY = 1800.0          # 两次回收之间至少隔这么久（秒）
_PRUNE_AT = [0.0]


def prune(keep=None, max_age: float = 3600.0) -> dict:
    """删掉「建它的那个目录已经不在了」的索引库。

    ★ 为什么需要（2026-10-30 千行工程实测）
      库名是**工作区绝对路径**的哈希，所以每来一个临时副本（复核副本、子 agent 副本）
      就多一个库；副本用完就删，库却永远留着。实测这台机器上已经攒了
      **25 个库 / 230MB**，而一轮 12 分钟的任务自己就能新增十几个。
      索引库本来就是"由源码重建"的东西（`backup.py` 里也是这么记的），删掉最坏只是下次重建。

    ★ 边界（这条会删文件，所以写清楚）
      ① 只动 `db_path()` 那个目录下的 `*.db`，别处一个文件都不碰；
      ② 只删**自己记着 root、而那个 root 确实不在了**的库 —— 没有记录的旧库不动
        （认不出来就留着，不猜）；
      ③ 刚建不到 `max_age` 的一律不动，避免和正在跑的会话抢；
      ④ `keep` 那个库（当前工作区）永远跳过。
    """
    now = time.time()
    if now - _PRUNE_AT[0] < _PRUNE_EVERY:
        return {"ok": True, "skipped": "刚回收过"}
    _PRUNE_AT[0] = now
    base = db_path(".").parent
    removed, kept, unknown = [], 0, 0
    try:
        files = list(base.glob("*.db"))
    except OSError:
        return {"ok": True, "removed": []}
    _keep = str(Path(keep).resolve()) if keep else ""
    for f in files:
        try:
            if f.stat().st_mtime > now - max_age:
                kept += 1
                continue
            c = sqlite3.connect("file:%s?mode=ro" % f.as_posix(), uri=True, timeout=5)
            try:
                row = c.execute("SELECT v FROM meta WHERE k='root'").fetchone()
            finally:
                c.close()
            if not row or not row[0]:
                unknown += 1            # 旧版库没记 root：认不出来就不动
                continue
            if _keep and str(Path(row[0]).resolve()) == _keep:
                kept += 1
                continue
            if Path(row[0]).exists():
                kept += 1
                continue
            f.unlink()
            removed.append(str(f))
        except Exception:
            continue
    return {"ok": True, "removed": removed, "kept": kept, "unknown": unknown}


def _has_fts(c: sqlite3.Connection) -> bool:
    try:
        c.execute("SELECT count(*) FROM chunks_fts").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def _skip(p: Path) -> bool:
    name = p.name.lower()
    if name in PRIVATE_FILES or name.endswith((".env", ".jsonl")) or name.startswith(".env."):
        return True
    for e in SKIP_EXT:
        if name.endswith(e):
            return True
    return False


def _plain_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS
                       and not d.startswith(".git") and (Path(dirpath) / d).resolve() == Path(dirpath) / d]
        for fn in filenames:
            yield Path(dirpath, fn).relative_to(root)


def _project_files(root: Path):
    in_repo = any((p / ".git").exists() for p in (root, *root.parents))
    if not in_repo:
        paths = list(_plain_files(root))
        if not any(p.name == ".gitignore" for p in paths):
            return paths
    exe = os.environ.get("HARNESS_GIT") or shutil.which("git")
    if not exe:
        raise OSError("无法读取 Git 忽略规则，已跳过索引。")
    routing = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
               "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in routing}
    def git(args, git_dir=None):
        base = [exe, "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
        if git_dir:
            base.extend(["--git-dir=" + git_dir, "--work-tree=" + str(root)])
        return subprocess.run(base + args, env=env, capture_output=True, check=True, timeout=60,
                              creationflags=0x08000000 if os.name == "nt" else 0).stdout
    listing = ["ls-files", "-z", "--exclude-standard"]
    if in_repo:
        files = git(listing + ["--cached", "--others", "--", "."]).split(b"\0")
        ignored_tracked = set(git(listing + ["--cached", "--ignored", "--", "."]).split(b"\0"))
        return (Path(os.fsdecode(p)) for p in files if p and p not in ignored_tracked)
    # Use Git's own nested ignore/negation semantics without changing a plain project.
    with tempfile.TemporaryDirectory(prefix="modot-index-rules-") as metadata:
        git(["-c", "init.templateDir=", "init", "--quiet", "--bare", metadata])
        files = git(listing + ["--others", "--", "."], metadata).split(b"\0")
    return (Path(os.fsdecode(p)) for p in files if p)


def _walk(root: Path):
    from .baseline import read_snapshot_guard
    protected = read_snapshot_guard()
    count = 0
    for rel in _project_files(root):
        parts = [part.casefold() for part in rel.parts]
        if rel.is_absolute() or ".." in parts or any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        if any(a == "memory" and b in {"inbox", "tree"} for a, b in zip(parts, parts[1:])):
            continue
        p = root / rel
        try:
            if _skip(p) or p.resolve() != p or not p.is_file() or protected(p) or p.stat().st_size > MAX_FILE:
                continue
        except (OSError, RuntimeError):
            continue
        yield p
        count += 1
        if count >= MAX_FILES:
            break


def _symbols(path: Path, text: str) -> list[tuple[str, str, int]]:
    """"这里有什么"：函数 / 类 / 标题 / 导出。拿不准就少给，别乱猜。"""
    out: list[tuple[str, str, int]] = []
    suf = path.suffix.lower()
    if suf == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((node.name, "func", node.lineno))
            elif isinstance(node, ast.ClassDef):
                out.append((node.name, "class", node.lineno))
        return out
    if suf in (".md", ".markdown"):
        for i, ln in enumerate(text.splitlines(), 1):
            m = re.match(r"^(#{1,4})\s+(.+?)\s*$", ln)
            if m:
                out.append((m.group(2)[:80], "h%d" % len(m.group(1)), i))
        return out
    if suf in (".js", ".ts", ".jsx", ".tsx", ".cjs", ".mjs"):
        for i, ln in enumerate(text.splitlines(), 1):
            m = re.match(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)", ln)
            if m:
                out.append((m.group(1), "func", i))
                continue
            m = re.match(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)", ln)
            if m:
                out.append((m.group(1), "class", i))
        return out
    return out


def _read_text(p: Path) -> str:
    """按 utf-8 读；读不出来就当二进制跳过（抛出去由调用方处理）。"""
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError("binary")
    return data.decode("utf-8", errors="replace")


_BUILD_LOCK = __import__("threading").Lock()


def build(root, force: bool = False) -> dict:
    """建/更新索引。只重算变过的文件（按 size + mtime 判）。

    ★ 为什么用 size+mtime 而不是每次算 sha：算 sha 要把每个文件读一遍，
      那和"直接搜一遍"没区别，索引就白建了。sha 只在判定"变了"之后才算。
    """
    r = Path(root).resolve()
    if not r.exists():
        return {"ok": False, "error": "目录不存在：%s" % r}
    prune(keep=r)
    # ★ 两个 `code_search` 同时第一次跑会同时建库（只读工具是**并行**的）。
    #   串行化之后，第二个进来看到"已经有文件了"，就不重建。
    with _BUILD_LOCK:
        try:
            files = list(_walk(r))
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": "无法核对索引的文件范围：" + str(exc)}
        return _build_locked(r, force, files)


def _build_locked(r: Path, force: bool, files) -> dict:
    t0 = time.time()
    c = _conn(r)
    fts = _has_fts(c)
    known = {row[0]: (row[1], row[2]) for row in
             c.execute("SELECT path, mtime, size FROM files")}
    seen = set()
    added = changed = removed = skipped = 0
    nfiles = 0
    for p in files:
        nfiles += 1
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(r)).replace("\\", "/")
        seen.add(rel)
        old = known.get(rel)
        if old and not force and abs(old[0] - st.st_mtime) < 1e-6 and old[1] == st.st_size:
            continue
        try:
            text = _read_text(p)
        except (ValueError, OSError):
            skipped += 1
            continue
        # ★ 入库前脱敏：索引是文件的**副本**，不遮就等于把 key 抄进另一个地方
        safe, _hits = _LKS.redact(text)
        lines = safe.splitlines()
        sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        c.execute("DELETE FROM chunks WHERE path=?", (rel,))
        c.execute("DELETE FROM symbols WHERE path=?", (rel,))
        if fts:
            c.execute("DELETE FROM chunks_fts WHERE path=?", (rel,))
        for i in range(0, len(lines), CHUNK_LINES):
            seg = "\n".join(lines[i:i + CHUNK_LINES])
            if not seg.strip():
                continue
            c.execute("INSERT INTO chunks(path, start, text) VALUES(?,?,?)",
                      (rel, i + 1, seg))
            if fts:
                c.execute("INSERT INTO chunks_fts(text, path, start) VALUES(?,?,?)",
                          (seg, rel, i + 1))
        for name, kind, line in _symbols(p, text):
            c.execute("INSERT INTO symbols(path, name, kind, line) VALUES(?,?,?,?)",
                      (rel, name, kind, line))
        c.execute("INSERT OR REPLACE INTO files(path, mtime, size, lines, sha, at)"
                  " VALUES(?,?,?,?,?,?)", (rel, st.st_mtime, st.st_size,
                                           len(lines), sha, time.time()))
        if old:
            changed += 1
        else:
            added += 1
    for rel in set(known) - seen:
        c.execute("DELETE FROM files WHERE path=?", (rel,))
        c.execute("DELETE FROM chunks WHERE path=?", (rel,))
        c.execute("DELETE FROM symbols WHERE path=?", (rel,))
        if fts:
            c.execute("DELETE FROM chunks_fts WHERE path=?", (rel,))
        removed += 1
    c.commit()
    total = c.execute("SELECT count(*) FROM files").fetchone()[0]
    c.close()
    return {"ok": True, "root": str(r), "db": str(db_path(r)),
            "files": total, "scanned": nfiles, "added": added,
            "changed": changed, "removed": removed, "skipped": skipped,
            "fts": fts, "ms": int((time.time() - t0) * 1000)}


def _q(raw: str) -> str:
    """把人的查询变成 FTS5 的安全查询串。

    ★ 不消毒的话，`a OR b`、`"`、`*` 这些会被当成语法，用户只会看到
      "fts5: syntax error near ..." —— 而那句话对找东西毫无帮助。
      这里统一拆成词、每词加引号、用 AND 连：**宁可搜得窄，也不要报错**。
    """
    toks = [t for t in re.split(r"[\s,;]+", str(raw or "")) if t][:8]
    if not toks:
        return ""
    return " AND ".join('"%s"' % t.replace('"', "").replace("*", "") for t in toks)


def search(query: str, limit: int = 8, root=None) -> dict:
    """按词找代码。返回文件 + 起始行 + 片段（片段已脱敏）。"""
    r = Path(root or Path.cwd()).resolve()
    # ★ 索引库**可能写不进去**（2026-10-31 千行工程实测）：只读副本、或沙箱不许
    #   在工作区外落盘时，`_conn` 里那句 CREATE TABLE 就会抛
    #   "attempt to write a readonly database" —— 整条工具调用就此失败，
    #   模型只看到一句异常，不知道还能怎么办。
    #   索引只是加速；读不到现成的库，就该告诉它「改用 grep」而不是崩在门口。
    try:
        c = _conn(r)
    except sqlite3.OperationalError as e:
        p = db_path(r)
        if p.exists():
            try:
                c = sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True, timeout=5)
            except sqlite3.Error:
                return {"ok": False,
                        "error": "索引库打不开（%s）。改用 run_check 跑 grep 搜，别在这里重试。" % e}
        else:
            return {"ok": False,
                    "error": "这个目录的索引库建不了（%s）。改用 run_check 跑 grep 搜，别在这里重试。" % e}
    try:
        have = c.execute("SELECT count(*) FROM files").fetchone()[0]
    except sqlite3.OperationalError:
        have = 0                      # 只读库还没建表 / 库是空的，都当"没索引"
    if not have:
        c.close()
        try:
            b = build(r)
        except sqlite3.OperationalError as e:
            return {"ok": False,
                    "error": "索引库建不了（%s）。改用 run_check 跑 grep 搜，别在这里重试。" % e}
        if not b.get("ok"):
            return b
        c = _conn(r)
    fts = _has_fts(c)
    lim = max(1, min(int(limit or 8), 30))
    rows = []
    if fts:
        q = _q(query)
        if not q:
            c.close()
            return {"ok": True, "query": query, "hits": [], "total": 0,
                    "note": "查询是空的"}
        try:
            cur = c.execute(
                "SELECT path, start, snippet(chunks_fts, 0, '«', '»', '…', 10) "
                "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, lim))
            rows = cur.fetchall()
        except sqlite3.OperationalError as e:
            c.close()
            return {"ok": False, "error": "查询写法有问题：%s" % e}
    else:
        pat = "%%%s%%" % str(query or "").replace("%", "")
        cur = c.execute("SELECT path, start, substr(text,1,300) FROM chunks "
                        "WHERE text LIKE ? LIMIT ?", (pat, lim))
        rows = cur.fetchall()
    # ★ FTS 是按**词**切，中文整段是一个词 —— 所以搜「已遮」这种子串会零命中。
    #   零命中时再按**子串**找一次（LIKE）：慢一点，但"搜不到"和"没有"必须能分清。
    #   实测：不补这一步，`«凭据已遮:…»` 这种标记只能整段搜到，人根本猜不到该打什么。
    if not rows:
        _q_raw = str(query or "").strip()
        if _q_raw:
            like = "%%%s%%" % _q_raw.replace("%", "").replace("_", "")
            cur = c.execute("SELECT path, start, substr(text,1,400) FROM chunks "
                            "WHERE text LIKE ? LIMIT ?", (like, lim))
            rows = cur.fetchall()
    hits = [{"path": a, "line": b, "snippet": (d or "").strip()[:700]}
            for a, b, d in rows]
    total = c.execute("SELECT count(*) FROM files").fetchone()[0]
    c.close()
    return {"ok": True, "query": query, "hits": hits, "total": total,
            "files": total, "fts": fts,
            "note": ("" if hits else
                     "没搜到。索引里目前有 %d 个文件；换几个关键词，"
                     "或者先 code_reindex 重建一次。" % total)}


def outline(path=None, root=None, limit: int = 200) -> dict:
    """这个文件（或整个库）里有什么：函数、类、标题。"""
    r = Path(root or Path.cwd()).resolve()
    c = _conn(r)
    if c.execute("SELECT count(*) FROM files").fetchone()[0] == 0:
        c.close()
        build(r)
        c = _conn(r)
    if path:
        rel = str(Path(path))
        try:
            rel = str(Path(path).resolve().relative_to(r)).replace("\\", "/")
        except (ValueError, OSError):
            rel = str(path).replace("\\", "/")
        rows = c.execute("SELECT name, kind, line FROM symbols WHERE path=? "
                         "ORDER BY line LIMIT ?", (rel, limit)).fetchall()
        c.close()
        return {"ok": True, "path": rel,
                "symbols": [{"name": a, "kind": b, "line": d} for a, b, d in rows],
                "note": "" if rows else "这个文件里没取到符号（可能是数据或纯文本）"}
    rows = c.execute("SELECT path, count(*) c FROM symbols GROUP BY path "
                     "ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return {"ok": True, "files": [{"path": a, "symbols": b} for a, b in rows]}


def stats(root=None) -> dict:
    """库有多大、什么时候建的（给界面/诊断包）。"""
    r = Path(root or Path.cwd()).resolve()
    p = db_path(r)
    if not p.exists():
        return {"ok": True, "exists": False, "db": str(p)}
    c = _conn(r)
    files = c.execute("SELECT count(*) FROM files").fetchone()[0]
    lines = c.execute("SELECT coalesce(sum(lines),0) FROM files").fetchone()[0]
    syms = c.execute("SELECT count(*) FROM symbols").fetchone()[0]
    last = c.execute("SELECT coalesce(max(at),0) FROM files").fetchone()[0]
    fts = _has_fts(c)
    c.close()
    return {"ok": True, "exists": True, "db": str(p), "files": files,
            "lines": lines, "symbols": syms, "fts": fts,
            "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else "",
            "bytes": p.stat().st_size if p.exists() else 0}


def describe(root=None) -> str:
    s = stats(root)
    if not s.get("exists"):
        return "代码索引：还没建（第一次 code_search 会自动建）"
    return ("代码索引：%d 个文件 / %d 行 / %d 个符号，上次更新 %s，%.1f MB"
            % (s["files"], s["lines"], s["symbols"], s.get("at") or "?",
               s.get("bytes", 0) / 1048576.0))
