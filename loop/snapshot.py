"""snapshot.py —— 改文件之前先留一份原件，改坏了能退回去。

★ 为什么必须有这个（2026-10-26）
    我们能改文件（`write_file` / `edit_file`），但改坏了只有一个退路：**指望有 git**。
    没 git 的目录、没提交的改动、被别人拉过的分支 —— 那一条都不成立。
    Claude Code / Cursor 都有 checkpoint，缺了它，"把权限交给它"这件事就没有安全垫。

★ 存哪
    `runs/<会话>/snap/` —— **跟着会话走**，不跟仓库走。
    理由：撤回是「我这一轮改坏了，退回去」，不是「回到上周」。跨会话没有意义，
    而放进仓库会被 git 看见、会被下一次提交带走。

★ 存什么
    整份原件（不是 diff）。因为撤回要的是"恢复成能跑的样子"，而重建一份文件
    需要原始字节；diff 在文件被外部工具改过之后不一定能反向应用。
    超过 2MB 的文件只记指纹不存内容 —— 那种文件改坏，本来也不该靠我们还原。

★ 一句话语义
    `restore(n)` = 撤销第 n 次及之后的全部修改。
    因为同一个文件可能被改三次，用"第 n 次那一份快照"（= 第 n 次之前的样子）恢复，
    而不是用最后一次 —— 否则会把中间那次的内容又装回去。
"""
# 主要引用方包括 `loop.py` 和 `legacy_entry.py`。
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import tempfile
from .filelock import FileLock, atomic_json
from pathlib import Path

# 单文件超过这个大小就不存内容（只记指纹 + 大小）
MAX_BYTES = 2 * 1024 * 1024

# 会话目录：由 init() 显式设一次（run-task 建会话时调）。
# ★ 不用再读一个环境变量：本我刚踩过"变量名对不上、另一处根本没被读取"那个坑。
_SESS: dict = {"dir": None}

# ★ 并行工具会同时调 take（两个 write_file 一起跑），而编号是"数一数已有几条"——
#   不加锁会撞号：两份快照写成同一个文件名，后一份把前一份盖掉，
#   而**盖掉的那一份正是撤回时要用的**。这类丢失不报错，只在撤回时才发现少了东西。
_LOCK = threading.Lock()


def init(sess_dir) -> None:
    """告诉快照模块这一轮的会话目录。不调 = 不存快照（不报错，安静跳过）。"""
    _SESS["dir"] = Path(sess_dir) if sess_dir else None


def _snapdir(sess_dir=None) -> Path | None:
    d = Path(sess_dir) if sess_dir else _SESS["dir"]
    if not d:
        return None
    p = Path(d) / "snap"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return p


def _safe(name: str) -> str:
    """文件名里不能有 / : 这些东西，换成下划线。"""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)[:80]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _read_index(snapdir: Path) -> list[dict]:
    p = snapdir / "index.jsonl"
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except OSError:
        return []
    return out


def index(sess_dir=None) -> list[dict]:
    """这一会话的全部快照记录（按发生顺序）。"""
    sd = _snapdir(sess_dir)
    return _read_index(sd) if sd else []


def take(tool: str, path: str, turn: int = 0, sess_dir=None) -> dict | None:
    """在**改之前**调：把原文件存一份。返回记录（或 None = 没存）。

    ★ 必须在写之前调。写之后再调存到的就是"改完的样子"，
      那种快照撤不回去 —— 而且它看起来是成功的，最难查。
    """
    sd = _snapdir(sess_dir)
    if sd is None:
        return None
    p = Path(path).absolute()
    with _LOCK, FileLock(sd / "index.lock"):
        return _take_locked(sd, tool, p, turn)


def _take_locked(sd: Path, tool: str, p: Path, turn: int) -> dict | None:
    recs = _read_index(sd)
    n = len(recs) + 1
    rec = {"n": n, "t": time.strftime("%H:%M:%S"), "tool": tool,
           "path": str(p), "turn": int(turn or 0), "existed": p.exists()}
    if p.exists():
        try:
            blob = p.read_bytes()
        except OSError as e:
            rec["error"] = "读不出原件：%s" % e
            blob = b""
        if not rec.get("error"):
            rec["sha"] = _sha(blob)
            rec["bytes"] = len(blob)
            if len(blob) > MAX_BYTES:
                # 太大：只记指纹。这种文件改坏了本来也不该靠我们还原。
                rec["too_big"] = True
                rec["error"] = "文件 %d 字节，超过 %d，没存内容（只记了指纹）" % (
                    len(blob), MAX_BYTES)
            else:
                fn = "%04d-%s-%s" % (n, rec["sha"][:8], _safe(p.name))
                try:
                    (sd / fn).write_bytes(blob)
                    rec["file"] = fn
                except OSError as e:
                    rec["error"] = "存不下：%s" % e
    try:
        with (sd / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return rec
    return rec


def _backup_current(sd: Path, targets: list[dict]) -> Path:
    """恢复之前，把**当前**的内容也存一份。

    ★ 为什么：撤回本身也是个会后悔的动作（"我撤错了，想回到撤之前"）。
      只存原件的快照在这一点上是不对称的 —— 撤销之后那次状态就永久没了。
    """
    bdir = Path(tempfile.mkdtemp(prefix="pre-restore-", dir=sd))
    manifest = []
    for r in targets:
        p = Path(r["path"])
        name = hashlib.sha256(str(p.absolute()).encode("utf-8")).hexdigest() + "-" + _safe(p.name)
        entry = {"path": str(p.absolute()), "existed": p.exists(), "file": name}
        if p.is_symlink():
            raise OSError("refusing to back up symlink: %s" % p)
        if p.exists():
            shutil.copy2(p, bdir / name)  # failure aborts restoration
            if p.read_bytes() != (bdir / name).read_bytes():
                raise OSError("backup verification failed: %s" % p)
        manifest.append(entry)
    atomic_json(bdir / "manifest.json", manifest)
    return bdir


def fingerprint(path):
    p = Path(path)
    if p.is_symlink():
        raise OSError("symlink cannot be restored safely: %s" % p)
    return {"exists": p.exists(), "sha": _sha(p.read_bytes()) if p.exists() else None}


def seal(n, sess_dir=None):
    """Persist the observed post-tool version so later external edits conflict."""
    sd = _snapdir(sess_dir)
    if sd is None:
        return
    with FileLock(sd / "index.lock"):
        rec = next((r for r in _read_index(sd) if r.get("n") == n), None)
        if rec:
            atomic_json(sd / ("after-%s.json" % n), fingerprint(rec["path"]))


def restore(n: int | None = None, sess_dir=None, dry: bool = False, force: bool = False) -> list[dict]:
    """撤销第 n 次及之后的修改。`n=None` = 只撤销最后一次。

    返回每一步做了什么（恢复 / 删除 / 跳过 + 原因），**不抛异常** ——
    撤回失败时人需要知道"哪一步没成"，而不是一个 traceback。
    """
    sd = _snapdir(sess_dir)
    if sd is None:
        return [{"ok": False, "why": "没有会话目录，这一轮没存过快照"}]
    recs = _read_index(sd)
    if not recs:
        return [{"ok": False, "why": "这个会话没有改动记录"}]

    last = max(int(r.get("n") or 0) for r in recs)
    start = last if n is None else int(n)
    if start < 1:
        start = 1
    targets = [r for r in recs if int(r.get("n") or 0) >= start]
    if not targets:
        return [{"ok": False, "why": "第 %d 次之后没有改动记录（共 %d 次）" % (start, last)}]

    # 同一个文件被改多次时，用**最早的**那份快照（= 第 start 次之前的样子）
    by_path: dict[str, dict] = {}
    for r in sorted(targets, key=lambda x: int(x.get("n") or 0)):
        by_path.setdefault(str(r["path"]), r)

    # Preflight every target before changing any file. Legacy snapshots have no
    # post-version and require explicit force, with a verified backup regardless.
    observed = {}
    try:
        for path in by_path:
            current = fingerprint(path)
            observed[path] = current
            last_rec = max((r for r in targets if str(r["path"]) == path), key=lambda r: r["n"])
            after = sd / ("after-%s.json" % last_rec["n"])
            expected = json.loads(after.read_text(encoding="utf-8")) if after.exists() else None
            if not force and expected != current:
                return [{"ok": False, "did": "conflict", "path": path,
                         "why": "文件已变化或缺少写后版本；请先检查差异，再显式强制撤回"}]
        bdir = None if dry else _backup_current(sd, list(by_path.values()))
    except (OSError, ValueError) as e:
        return [{"ok": False, "did": "backup-failed", "why": str(e)}]
    out = []
    for path, r in by_path.items():
        try:
            if fingerprint(path) != observed[path]:
                out.append({"ok": False, "did": "conflict", "path": path,
                            "why": "备份期间文件发生变化，未撤回"})
                continue
        except OSError as e:
            out.append({"ok": False, "did": "conflict", "path": path, "why": str(e)})
            continue
        p = Path(path)
        if not r.get("existed"):
            # 这个文件是那之后才新建的 ⇒ 撤回 = 删掉它
            if not p.exists():
                out.append({"ok": True, "path": path, "did": "skip", "why": "已经不在了"})
            elif dry:
                out.append({"ok": True, "path": path, "did": "would-delete"})
            else:
                try:
                    p.unlink()
                    out.append({"ok": True, "path": path, "did": "delete"})
                except OSError as e:
                    out.append({"ok": False, "path": path, "did": "delete",
                                "why": str(e)})
            continue
        fn = r.get("file")
        if not fn or not (sd / fn).exists():
            out.append({"ok": False, "path": path, "did": "skip",
                        "why": r.get("error") or "这份快照没存下内容（超过大小上限或读不出）"})
            continue
        if dry:
            out.append({"ok": True, "path": path, "did": "would-restore",
                        "n": r.get("n")})
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sd / fn, p)
            out.append({"ok": True, "path": path, "did": "restore", "n": r.get("n")})
        except OSError as e:
            out.append({"ok": False, "path": path, "did": "restore", "why": str(e)})
    if bdir is not None:
        out.append({"ok": True, "path": str(bdir), "did": "backup",
                    "why": "撤回前的状态存这里，撤错了还能回来"})
    return out


# ══════════════════════════════════════════════════════════════════
# 基线：开工前给工作区留一份指纹，收尾时回答「这一轮到底改了什么」
#
# 为什么要它（2026-10-27 用户：「自我开工之前保留一份镜像给本我，本我同时读
# 开工前和开工后的代码，直接就能读到意图和它具体改了什么」）
#   本我现在看的是**动作摘要**（「edit_file: 路径」+ 工具返回值），不是代码。
#   所以它能查"说了但没做"，查不了"做了但不是那个做法"。
#   整仓复制不现实（node_modules 几万个文件），而本我要的只是
#   「哪几个文件变了」—— 一份指纹就够，不需要第二座代码山。
#
# ★ 和上面 `take()` 的分工：
#   `take` 存的是**被工具改过的单文件原件**，用来撤回；
#   这里的基线是**全工作区的指纹**，用来对照。两者谁也不替代谁。
BASE_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
               "dist", "build", ".next", ".cache", ".idea", ".vscode",
               "site-packages", ".mypy_cache", ".pytest_cache"}
BASE_HASH_MAX = 1 << 20        # 1MB 以上只记 stat（读全文太贵，收益也小）
BASE_FILE_MAX = 20000          # 文件数上限：再多也会拖慢开跑前的等待
# ★ 原件副本（2026-10-27，为了 L2 的真 diff）：只存**小文本**。
#   为什么必须存：脚本、git、外部工具改的文件**没有写前快照**，
#   没有基线原件就 diff 不出来 —— 而那恰恰是本我最该看见的那半。
BASE_COPY_MAX = 64 * 1024      # 单文件上限 64KB
BASE_COPY_TOTAL = 32 * 1024 * 1024   # 总量上限 32MB，超了就不再存（只留指纹）


def _is_text(b: bytes) -> bool:
    """粗判是不是文本：开头有 NUL 就当二进制（diff 二进制没有意义）。"""
    return b"\x00" not in b[:2048]


def _base_dir(sess_dir=None) -> Path | None:
    d = Path(sess_dir) if sess_dir else _SESS["dir"]
    if not d:
        return None
    p = Path(d) / "base"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return None


def _skip_root(sess_dir) -> "Path | None":
    """会话目录所在的那一片**产物区**要不要整片跳过。

    ★ 为什么不是只跳过会话自己：`runs/` 下面还有报告、看板、探针输出，
      它们每一轮都在变。只跳过会话目录的话，本我每次都会收到一份
      「改了 report.html、board.html…」的清单 —— 那不是它要审的代码。
    ★ 只认「父目录叫 runs」这一种形状（就是我们自己的产物区），
      不按名字瞎跳过别的目录。
    """
    if not sess_dir:
        return None
    try:
        d = Path(sess_dir).resolve()
    except OSError:
        return None
    # 往上找第一个叫 runs 的祖先：会话可能落在 runs/app-<时间>/，也可能落在
    # runs/sessions/<时间>/ —— 只认"父目录"会把后一种漏掉，于是 runs/locks/*.json
    # 这种每次都会变的文件就跑进改动清单里（实测踩到）。
    for p in [d] + list(d.parents):
        if p.name == "runs":
            return p
    return d


def _skip_roots(sess_dir, rootp: Path | None = None) -> list:
    """要整片跳过的目录。

    ★ 除了会话所在的 `runs/`，还要跳**数据根下面那个 runs/** ——
      界面测试时数据根在临时目录、工作区还是仓库，两者不是同一片，
      于是仓库里的 `runs/`（截图、报告、看板）会被算成"改了"
      （实测：`runs/_flow_1_early.png` 出现在改动清单里）。
    """
    out = []
    s = _skip_root(sess_dir)
    if s is not None:
        out.append(s)
    # ★ 两个根都要看：产物可能落在**数据根**下（打包后），也可能落在**程序根**下
    #   （开发时两者是同一个仓库）。测试里数据根在临时目录、工作区还是仓库，
    #   只跳数据根那个就漏了仓库里的 runs/ —— 实测把截图算成了"改了"。
    try:
        from . import paths as P
        for base in (P.data_root(), P.app_root()):
            dr = (Path(base) / "runs").resolve()
            if rootp is not None:
                try:
                    dr.relative_to(rootp)
                except ValueError:
                    continue
            if dr not in out:
                out.append(dr)
    except Exception:
        pass
    return out


def tracks_root(root, sess_dir=None):
    """Internal run directories are not source workspaces for legacy snapshots."""
    root = Path(root).resolve()
    return not any(root.is_relative_to(Path(skip).resolve()) for skip in _skip_roots(sess_dir, root))


def _walk_files(root: Path, skip_dirs: set, skip_under):
    """列出要纳入基线的文件（相对路径 + 绝对路径）。

    `skip_under` 可以是一个路径，也可以是一组（会话所在的 runs/ + 数据根的 runs/）。
    """
    skips = ([skip_under] if isinstance(skip_under, (str, Path)) else list(skip_under or []))
    out = []
    root = root.resolve()
    if any(root.is_relative_to(Path(skip).resolve()) for skip in skips):
        return out
    for p in root.rglob("*"):
        try:
            if p.is_symlink():
                continue
            if p.is_dir():
                continue
            rel = p.relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part in skip_dirs for part in rel.parts[:-1]):
            continue
        _skip = False
        for _s in skips:
            try:
                p.resolve().relative_to(Path(_s).resolve())
                _skip = True      # 会话/产物区自己不纳入（否则会把快照算成"改动"）
                break
            except (OSError, ValueError):
                continue
        if _skip:
            continue
        out.append((rel.as_posix(), p))
        if len(out) >= BASE_FILE_MAX:
            break
    return out


def baseline(root=".", sess_dir=None) -> dict:
    """开工前调：把工作区的指纹存下来。返回 {ok, files, ms, path}。"""
    bd = _base_dir(sess_dir)
    if bd is None:
        return {"ok": False, "why": "没有会话目录，基线无处可放"}
    t0 = time.time()
    rootp = Path(root).resolve()
    files = {}
    _cdir = bd / "files"
    items = _walk_files(rootp, BASE_IGNORE,
                        _skip_roots(sess_dir if sess_dir else _SESS["dir"], rootp))

    # ★ 两遍：先 stat（快）算出"哪些值得存原件"，再并行读+写。
    #   串行读 863 个小文件在 Windows 上要 8 秒（实测），并行后 2~3 秒 ——
    #   这 5 秒是**每次点开始都要等的**，值得优化。
    metas, budget = [], BASE_COPY_TOTAL
    for rel, p in sorted(items, key=lambda x: x[0]):
        try:
            st = p.stat()
        except OSError:
            continue
        copies = st.st_size <= BASE_COPY_MAX and st.st_size <= budget
        if copies:
            budget -= st.st_size
        metas.append((rel, p, st, copies))

    def _one(m):
        rel, p, st, want_copy = m
        h, copied = "", False
        if st.st_size <= BASE_HASH_MAX:
            try:
                b = p.read_bytes()
            except OSError:
                b = b""
            if b:
                h = _sha(b)
                if want_copy and _is_text(b):
                    try:
                        dst = _cdir / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(b)
                        copied = True
                    except OSError:
                        pass
        rec = {"s": st.st_size, "m": st.st_mtime_ns, "h": h}
        if copied:
            rec["c"] = 1
        return rel, rec

    import concurrent.futures as _cf
    if metas:
        with _cf.ThreadPoolExecutor(max_workers=8) as ex:
            for rel, rec in ex.map(_one, metas):
                files[rel] = rec
    obj = {"root": str(rootp), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "files": files}
    try:
        (bd / "index.json").write_text(json.dumps(obj, ensure_ascii=False),
                                       encoding="utf-8")
    except OSError as e:
        return {"ok": False, "why": "写不进去：%s" % e}
    return {"ok": True, "files": len(files), "ms": int((time.time() - t0) * 1000),
            "path": str(bd / "index.json")}


def changes(root=".", sess_dir=None) -> dict:
    """收尾时调：拿现状和基线下比，给出 改/新增/删 三张单子。

    ★ 判"改"用**内容哈希**，不用时间戳：`touch` 一下、编辑器原子保存
      都会动 mtime，但那不算改了内容 —— 只看 mtime 会天天报一堆假改动，
      而假改动一多，这份单子就没人看了。
    """
    bd = _base_dir(sess_dir)
    if bd is None or not (bd / "index.json").exists():
        return {"ok": False, "why": "没有基线（这一轮开工时没留）"}
    try:
        old = json.loads((bd / "index.json").read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "why": "基线读不出来：%s" % e}
    rootp = Path(root).resolve()
    before = old.get("files") or {}
    now = {}
    for rel, p in _walk_files(rootp, BASE_IGNORE, _skip_roots(sess_dir, rootp)):
        try:
            st = p.stat()
        except OSError:
            continue
        h = ""
        if st.st_size <= BASE_HASH_MAX:
            try:
                h = _sha(p.read_bytes())
            except OSError:
                h = ""
        now[rel] = {"s": st.st_size, "m": st.st_mtime_ns, "h": h}

    changed, added, touched = [], [], []
    for rel, cur in now.items():
        prev = before.get(rel)
        if prev is None:
            added.append({"path": rel, "bytes": cur["s"]})
            continue
        if prev.get("h") and cur.get("h"):
            if prev["h"] != cur["h"]:
                changed.append({"path": rel, "bytes": cur["s"],
                                "was": prev.get("s")})
        elif prev.get("s") != cur["s"]:
            # 大文件没有哈希，只能靠大小；变了就记，但标明"没比对内容"
            changed.append({"path": rel, "bytes": cur["s"],
                            "was": prev.get("s"), "stat_only": True})
        elif prev.get("m") != cur.get("m"):
            touched.append(rel)          # 动过但内容一样（比如 touch）
    removed = [{"path": rel, "was": v.get("s")}
               for rel, v in before.items() if rel not in now]

    # ── 真 diff（L2，2026-10-27）：拿基线原件和现在的内容对，给几段真实差异 ──
    #   ★ 只对**有原件的**文件做：脚本/git 改的那些，原件就在基线里，能对；
    #     超过 64KB 或二进制的没有原件，标明"只知变了、看不到差异"——
    #     那种含糊要在单子上显出来，不能让本我以为"没差异=没改"。
    import difflib
    cdir = bd / "files"

    def _base_text(rel):
        try:
            b = (cdir / rel).read_bytes()
        except OSError:
            return None
        return b.decode("utf-8", "replace") if _is_text(b) else None

    diffs = []
    budget = 400                      # 全部 diff 的总行数上限
    _added_set = {x["path"] for x in added}
    _removed_set = {x["path"] for x in removed}
    for item in changed + added + removed:
        rel = item["path"]
        if budget <= 0:
            break
        # 新增 ⇒ 开工前是空；删除 ⇒ 现在是空。两头都不能用 None 表示，
        # None 的意思是"拿不到原件"，那才该标 no_base。
        a = "" if rel in _added_set else _base_text(rel)
        if rel in _removed_set:
            b = ""
        else:
            try:
                cur_b = (rootp / rel).read_bytes()
                b = cur_b.decode("utf-8", "replace") if _is_text(cur_b) else None
            except OSError:
                b = None
        if a is None or b is None:
            item["no_base"] = True
            continue
        al = a.splitlines()
        bl = b.splitlines()
        piece, n_add, n_del = [], 0, 0
        for ln in difflib.unified_diff(al, bl, fromfile="开工前/" + rel,
                                       tofile="现在/" + rel, n=2, lineterm=""):
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            if ln.startswith("+"):
                n_add += 1
            elif ln.startswith("-"):
                n_del += 1
            piece.append(ln)
            if len(piece) >= 60:
                piece.append("…（这一处更长，截断了）")
                break
        item["add"], item["del"] = n_add, n_del
        if piece:
            txt = "\n".join(piece)
            diffs.append({"path": rel, "text": txt, "add": n_add, "del": n_del})
            budget -= len(piece)

    stat_only = [x["path"] for x in changed if x.get("stat_only")]
    no_base = [x["path"] for x in changed + added + removed if x.get("no_base")]
    return {"ok": True, "changed": changed[:200], "added": added[:200],
            "removed": removed[:200], "touched": len(touched),
            "diffs": diffs, "stat_only": stat_only[:50], "no_base": no_base[:50],
            "scanned": len(now), "root": str(rootp),
            "since": old.get("at") or ""}

