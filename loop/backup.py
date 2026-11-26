#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup.py —— 把「这台机器上丢了就补不回来」的东西打成一个能带走的包。

它解决什么（2026-10-27）
    换机器、重装、硬盘坏之前，手上真正不可再生的只有三样：
    记忆树里的判断、人改过的规则、会话里做过的决定。
    代码能从 git 拿回来，索引能重建，诊断包随手能再打一份 —— 那三样不行。

★ 备份的价值不在「导出了」，在「能说清导出了什么、少了什么」
    所以每次导出都写一份 manifest：每个文件的字节数和 sha256，逐条核对过；
    并且把**没导的东西连原因一起写进去**。
    少一样却不说，这份包就只是一份"看起来像备份"的文件 —— 出事那天才发现少的那一样正好是要的那一样。

★ 三个档，不是两个
    core   —— 默认就导：记忆、会话、规则、习惯、许可底账、提案
    secret —— 默认**不导**：接口钥匙、模型凭据、模型档案、MCP 配置（env 里常放着 key）
    skip   —— 永远不导：索引库（能重建）、回收区（已删的）、上一次的备份自己（否则一层套一层）

★ 往外走的路上打一次码
    会话记录里可能有人贴过 key。导出时逐份过一遍 `leaks`，命中的那一份
    **在包里改成打码版，原文件一个字不动**，并在 manifest 里记一笔。
    这样"备份里带着明文 key"这件事不会因为它藏在第 63 个 json 里就发生。

★ 为什么还原没有接口
    导出是读，还原是**覆盖整份现场**。给它一个 HTTP 入口，等于给 agent
    一条"把自己脚下的地板换掉"的路。所以：导出可以给界面按钮，还原只有命令行。
    这条不是洁癖 —— 它是这个模块唯一一个不可逆动作，只能由人在终端里按下。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

from . import leaks
from . import paths as P

# manifest 的格式版本。以后改结构就 +1，还原时版本不认识就**拒绝**，不猜。
VERSION = 1

T_CORE = "core"
T_SECRET = "secret"

# ── 数据根里要带走的（相对数据根）──────────────────────────────
# ★ 为什么是一张**写死的表**，不是"把数据根整个拷走"：
#   开发时 `data_root() == app_root()`，整个拷就是把源码、node_modules、
#   验收夹具全带上，几十上百 MB，而且和"备份判断"这件事毫无关系。
#   打包后两者分开，这张表反而是唯一说得清"哪几样算用户的东西"的地方。
# ★ 顺序 = 人读的顺序：先判断，再规则，再现场。
DATA_ITEMS = (
    ("memory", "dir", "记忆树、待采纳、事实区（这里是判断本身）"),
    ("harness/charter.json", "file", "档位与开关"),
    ("harness/sandbox_rules.json", "file", "沙箱规则"),
    (".harness/policy.json", "file", "工具策略"),
    (".harness/rules.json", "file", "闸的规则"),
    (".harness/proposed.json", "file", "待采纳提案"),
    (".harness/mem-audit.jsonl", "file", "记忆改动审计"),
    (".harness/mem-backups", "dir", "改记忆前的留底"),
    (".harness/rule-backups", "dir", "改规则前的留底"),
    (".harness/agent-memory", "dir", "项目级事实区"),
    (".harness/agent-memory-local", "dir", "本地级事实区"),
    (".harness/commands", "dir", "斜杠命令"),
    (".harness/agents", "dir", "子 agent 定义"),
    (".harness/prices.json", "file", "价格表（不内置，是用户自己填的）"),
    (".harness/hooks.json", "file", "钩子"),
    ("runs/done.json", "file", "验收快照"),
)

# ── 用户目录里要带走的（相对 ~）────────────────────────────────
# ★ 这一半最容易漏：许可底账、习惯区、技能、用户级事实区都**不在数据根下**，
#   只导数据根的话，新机器上"什么都不记得、什么都要重新批"。
HOME_ITEMS = (
    (".harness/HABITS.md", "file", "习惯区（人写的，不是生成的）"),
    (".harness/habits.json", "file", "习惯索引"),
    (".harness/grants.jsonl", "file", "一次性许可的底账"),
    (".harness/baseline-hits.jsonl", "file", "底线命中计数"),
    (".harness/hooks.json", "file", "钩子"),
    (".harness/agent-memory", "dir", "用户级事实区"),
    (".harness/agents", "dir", "用户级 agent"),
    (".harness/skills", "dir", "技能"),
)

# ── 只在 secret 档里的 ────────────────────────────────────────
SECRET_ITEMS = (
    ("data", "api-token", "file", "接口钥匙"),
    ("home", ".workbuddy/harness/credentials.env", "file", "模型凭据"),
    ("home", ".workbuddy/harness/models.json", "file", "模型档案（含 key）"),
    ("home", ".harness/mcp.json", "file", "MCP 服务清单（env 里常放 key）"),
)

# ── 永远不导，且要说清为什么 ──────────────────────────────────
# 相对数据根。/ 开头的按前缀匹配。
SKIP = (
    ("runs/backups", "上一次的备份自己（套进去就一层套一层）"),
    ("runs/locks", "租约：机器换了就该失效，带过去反而挡住新机器"),
    ("runs/_trash", "回收区：已经删掉的东西"),
    ("runs/diagnose", "诊断包：本机现场的快照，不是判断"),
    (".harness/code-index", "索引库：由源码重建，几十 MB"),
    (".harness/pre-restore", "还原前的现场留底"),
    ("runs/report.html", "生成物：随时能重出"),
    ("runs/health.html", "生成物：随时能重出"),
    ("runs/index.html", "生成物：随时能重出"),
    ("runs/_", "验收/测试夹具（前缀）"),
)

DEFAULT_SUBDIR = "runs/backups"


def _tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _skip_why(rel: str) -> str | None:
    """这个相对路径该不该跳过，以及为什么。返回原因为跳过，None 为要导。"""
    rel = rel.replace("\\", "/")
    for k, why in SKIP:
        if k.endswith("/"):
            # 前缀项（`runs/app-` / `runs/_`）：只按前缀比，不加斜杠
            if rel.startswith(k):
                return why
        elif rel == k or rel.startswith(k + "/"):
            return why
    parts = rel.split("/")
    if "__pycache__" in parts:
        return "字节码缓存"
    if rel.endswith(".pyc"):
        return "字节码缓存"
    return None


def _home() -> Path:
    return Path.home()


def _pairs(with_secrets: bool = False) -> list[dict]:
    """把三张表拼成一份清单：每个条目带根、子路径、类型、档位。

    ★ 真源只有一个：路径都从 `paths` / `Path.home()` 算，不在这里另拼一遍。
      别的模块换了落点，这里跟着变。
    """
    out = []
    for rel, kind, why in DATA_ITEMS:
        out.append({"root": "data", "rel": rel, "kind": kind, "why": why,
                    "tier": T_CORE})
    for rel, kind, why in HOME_ITEMS:
        out.append({"root": "home", "rel": rel, "kind": kind, "why": why,
                    "tier": T_CORE})
    if with_secrets:
        for root, rel, kind, why in SECRET_ITEMS:
            out.append({"root": root, "rel": rel, "kind": kind, "why": why,
                        "tier": T_SECRET})
    return out


def _abs(item: dict) -> Path:
    return (P.data_root() if item["root"] == "data" else _home()) / item["rel"]


def _run_dirs(runs: Path) -> list[Path]:
    """`runs/` 下哪些目录是**真会话** —— 按里面有什么判，不按名字判。

    ★ 这里踩过一次，记下来（2026-10-27）：
      最初照 `session.py` 的 `SESSIONS = runs/sessions` 写成「导 `runs/sessions`、
      排除 `runs/app-*`」。可实际会话就落在 `runs/app-<时间>/`（`app-` 是界面起的），
      而 `runs/sessions` 这个目录**根本不存在**。
      照那份清单导：包打得成功、自检也过（它只核对"包里的东西对不对"），
      唯独**用户的全部会话记录被当成临时目录排除了**。
      ⇒ 判据换成"目录里有 events.jsonl 或 task.md 才算会话"，
        名字只在"下划线开头的是夹具"这一件事上用。
    """
    out = []

    def _is_session(d: Path) -> bool:
        try:
            names = {p.name for p in d.iterdir()}
        except OSError:
            return False
        return "events.jsonl" in names or "task.md" in names

    # ★ 会话有两种落法，**都要扫**（2026-10-27 又漏一次）：
    #   · 界面起的：`runs/app-<时间>/`          —— `runs/` 的直接子目录
    #   · 命令行起的：`runs/sessions/<时间>/`    —— 深一级
    #   只扫第一层的话，`runs/sessions` 自己不是会话（里面没有 events.jsonl），
    #   于是它下面每一个会话都被静静漏掉 —— 和 `runs/sessions` 那次错法是同一个:
    #   以为知道了落点，其实只知道了其中一种。⇒ 按"哪几层会出现会话"扫，不按名字猜。
    for base in (runs, runs / "sessions"):
        try:
            items = sorted(base.iterdir())
        except OSError:
            continue
        for d in items:
            if d.is_dir() and not d.name.startswith("_") and _is_session(d):
                out.append(d)
    return out


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _payload(raw: bytes) -> tuple[bytes, bool]:
    """出去之前过一遍密钥检测。返回（真正写进包里的字节, 有没有打码）。

    ★ 只对**能当文本读**的文件做。二进制原样带走 —— 在二进制里按正则打码
      只会把文件弄坏，那不是"更安全"，那是"备份解开是坏的"。
    """
    try:
        txt = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, False
    if not leaks.has_secret(txt):
        return raw, False
    clean, _hits = leaks.redact(txt)
    return clean.encode("utf-8"), True


def plan(with_secrets: bool = False) -> dict:
    """算一遍要带什么：**不写任何文件**。给导出前看一眼用。"""
    files, absent = [], []
    for it in _pairs(with_secrets):
        p = _abs(it)
        if it["kind"] == "file":
            if p.is_file():
                files.append({**it, "path": p, "size": p.stat().st_size})
            else:
                absent.append({**it, "path": p})
            continue
        if not p.is_dir():
            absent.append({**it, "path": p})
            continue
        n = 0
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(p).as_posix()
            if _skip_why(it["rel"] + "/" + rel):
                continue
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            files.append({**it, "rel": it["rel"] + "/" + rel, "path": f,
                          "size": sz})
            n += 1
        if n == 0:
            # 目录在、但是空的：算"有"还是"没有"？算有，但要标出来。
            absent.append({**it, "path": p, "empty": True})

    # 会话目录单独扫（判据是"里面有什么"，不是"叫什么"，见 `_run_dirs`）
    runs = P.data_root() / "runs"
    rdirs = _run_dirs(runs)
    if not rdirs:
        absent.append({"root": "data", "rel": "runs", "kind": "dir",
                       "why": "会话记录", "path": runs})
    for d in rdirs:
        for f in sorted(d.rglob("*")):
            if not f.is_file():
                continue
            rel = "%s/%s" % (d.relative_to(P.data_root()).as_posix(),
                             f.relative_to(d).as_posix())
            if _skip_why(rel):
                continue
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            files.append({"root": "data", "rel": rel, "path": f, "size": sz,
                          "kind": "file", "why": "会话记录", "tier": T_CORE})
    return {"files": files, "absent": absent,
            "bytes": sum(f["size"] for f in files)}


def export(out=None, with_secrets: bool = False) -> dict:
    """导出。返回一份能直接打印的报告。

    输出默认落在 `<数据根>/runs/backups/` —— 那个目录在 `SKIP` 里，
    所以下一次导出不会把这一份又装进去。
    """
    rep = {"ok": False, "path": "", "files": 0, "bytes": 0, "redacted": 0,
           "absent": [], "skipped": [], "error": "", "sha256": "",
           "with_secrets": with_secrets, "warnings": []}
    pl = plan(with_secrets)
    if not pl["files"]:
        rep["error"] = ("一个文件都没有 —— 数据根是 %s，看着不像这台机器的现场。"
                        "先确认 HARNESS_DATA 有没有指错。" % P.data_root())
        return rep

    if out:
        dest = Path(out)
        if dest.is_dir():
            dest = dest / ("harness-backup-%s.zip" % _tag())
    else:
        dest = P.data_root() / DEFAULT_SUBDIR / ("harness-backup-%s.zip" % _tag())
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        rep["error"] = "建不了输出目录：%s" % e
        return rep

    entries, absent, failed = [], [], []
    total = 0
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for f in pl["files"]:
                try:
                    raw = f["path"].read_bytes()
                except OSError as e:
                    # 单个文件读不到**不能安静跳过** —— 那就是"漏了一样还说自己齐了"。
                    # ★ 只塞进 warnings 还不够（2026-10-27 自查）：warnings 只在这次
                    #   命令行输出里出现，而**跟着包走的是 manifest**。包到了别人手上，
                    #   "少了一个文件"这件事就没人知道了。⇒ 记进 manifest 的 failed。
                    failed.append({"name": "%s/%s" % (f["root"], f["rel"]),
                                   "why": "读不到：%s" % e})
                    rep["warnings"].append("%s 读不到：%s" % (f["rel"], e))
                    continue
                data, cut = _payload(raw)
                if cut:
                    rep["redacted"] += 1
                name = "%s/%s" % (f["root"], f["rel"].replace("\\", "/"))
                z.writestr(name, data)
                entries.append({"name": name, "size": len(data),
                                "sha256": _sha(data), "tier": f["tier"],
                                "redacted": cut})
                total += len(data)
            for a in pl["absent"]:
                absent.append({"name": "%s/%s" % (a["root"], a["rel"]),
                               "why": a["why"],
                               "state": "空目录" if a.get("empty") else "这台机器上没有"})
            man = {
                "kind": "harness-backup",
                "version": VERSION,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tag": _tag(),
                "source": {"data_root": str(P.data_root()), "home": str(_home()),
                           "split": P.is_split()},
                "with_secrets": with_secrets,
                "counts": {"files": len(entries), "bytes": total,
                           "redacted": rep["redacted"], "absent": len(absent),
                           "failed": len(failed)},
                "entries": entries,
                "absent": absent,
                # 想导却读不到的：**清单要自己说**，不能只靠当时那屏输出
                "failed": failed,
                "skipped": [{"path": k, "why": v} for k, v in SKIP],
                "secret_keys": [r for _root, r, _k, _w in SECRET_ITEMS],
            }
            # ★ manifest 最后写：它的内容依赖前面每一个文件的哈希。
            #   放第一个就得先算一遍再重算一遍，多一份"两处不一致"的机会。
            z.writestr("manifest.json",
                       json.dumps(man, ensure_ascii=False, indent=2))
    except (OSError, zipfile.BadZipFile) as e:
        rep["error"] = "写包失败：%s" % e
        return rep

    rep.update({"path": str(dest), "files": len(entries), "bytes": total,
                "absent": absent, "failed": len(failed)})
    # ★ 写完立刻自己验一遍：不验的"导出成功"只是"没报错"。
    v = verify(dest)
    rep["sha256"] = v.get("sha256", "")
    if not v.get("ok"):
        rep["error"] = "包写出来了但自检不过：%s" % (v.get("error") or v.get("bad"))
        return rep
    rep["ok"] = True
    if with_secrets:
        rep["warnings"].append(
            "这份包里有明文钥匙（%d 项）：别发给别人、别放同步盘，"
            "用完就删。" % len(SECRET_ITEMS))
    return rep


def _load_manifest(z: zipfile.ZipFile) -> dict:
    try:
        return json.loads(z.read("manifest.json").decode("utf-8"))
    except KeyError:
        return {"error": "包里没有 manifest.json —— 不是这个工具打的包"}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"error": "manifest 读不出来：%s" % e}


def verify(zip_path) -> dict:
    """不开包就确认它是不是完整的：逐条比 sha256 + 跑一遍 CRC。"""
    out = {"ok": False, "files": 0, "bad": [], "error": "", "sha256": ""}
    p = Path(zip_path)
    try:
        out["sha256"] = _sha(p.read_bytes())
    except OSError as e:
        out["error"] = "读不到这个文件：%s" % e
        return out
    try:
        with zipfile.ZipFile(p) as z:
            man = _load_manifest(z)
            if man.get("error"):
                out["error"] = man["error"]
                return out
            if man.get("version") != VERSION:
                out["error"] = ("这份包的格式版本是 %s，这个版本的工具只认 %s"
                                % (man.get("version"), VERSION))
                return out
            names = set(z.namelist())
            for e in man.get("entries") or []:
                if e["name"] not in names:
                    out["bad"].append("%s 不在包里" % e["name"])
                    continue
                got = _sha(z.read(e["name"]))
                if got != e["sha256"]:
                    out["bad"].append("%s 内容对不上" % e["name"])
            out["files"] = len(man.get("entries") or [])
            # CRC：读一遍所有成员，坏了会抛 BadZipFile
            bad_crc = z.testzip()
            if bad_crc:
                out["bad"].append("%s CRC 坏了" % bad_crc)
    except (zipfile.BadZipFile, OSError) as e:
        out["error"] = "包打不开：%s" % e
        return out
    out["ok"] = not out["bad"] and not out["error"]
    return out


def inspect(zip_path) -> dict:
    """只看不还原：这个包里有什么、少了什么、有没有打码、含不含钥匙。"""
    p = Path(zip_path)
    with zipfile.ZipFile(p) as z:
        man = _load_manifest(z)
        if man.get("error"):
            return man
        man = dict(man)
        man["size"] = p.stat().st_size
        man["verified"] = verify(p)
        return man


# ── 还原 ────────────────────────────────────────────────────

# 用户目录下**只允许**落到这两处。包里写了别的名字一律拒收。
HOME_ROOTS = (".harness", ".workbuddy/harness")


def _member_target(name: str, with_home: bool) -> tuple[Path | None, Path | None, str]:
    """把一个包内路径换成落点。不认识就返回（None, None, 原因）。

    ★ 这里是整个还原唯一的入口校验，也是最该被反例打的地方：
      包是人给的，`../..` 和绝对路径都能写进去，而 `extractall` 会照办。
    ★ 落点顺手把根也带回来：留底时要按"它本来属于哪个根"挪，
      让调用方自己再猜一次根，就等于把这段判断写第二遍。
    """
    if not name or name.endswith("/"):
        return None, None, "空名字"
    if "\\" in name:
        return None, None, "反斜杠（Windows 上会被当分隔符）"
    if name.startswith("/") or ":" in name:
        return None, None, "绝对路径或盘符"
    parts = name.split("/")
    if any(x in ("..", ".", "") for x in parts):
        return None, None, "路径上跳"
    root, rel = parts[0], "/".join(parts[1:])
    if root == "data":
        base = P.data_root()
    elif root == "home":
        if not with_home:
            return None, None, "只还原数据根（要连用户目录一起，加 --home）"
        if not any(rel == h or rel.startswith(h + "/") for h in HOME_ROOTS):
            return None, None, "用户目录里只认 %s" % " 和 ".join(HOME_ROOTS)
        base = _home()
    else:
        return None, None, "不认识的根：%s" % root
    dest = base / rel
    try:
        # 双保险：拼完之后必须仍在 base 里面（挡住符号链接之类的花样）
        dest.resolve().relative_to(base.resolve())
    except (ValueError, OSError, RuntimeError):
        return None, None, "拼出来的落点跑到根外面了"
    return dest, base, ""


def restore(zip_path, force: bool = False, with_home: bool = False,
            no_secrets: bool = True) -> dict:
    """把包解开，**先留一份现场**再覆盖。

    no_secrets 默认 True：迁移时最怕的是顺手把新机器上的钥匙换成了旧机器那把。
    确实要连钥匙一起搬，显式 `--with-secrets`。
    """
    rep = {"ok": False, "restored": 0, "skipped": [], "error": "",
           "backup": "", "bytes": 0}
    p = Path(zip_path)
    v = verify(p)
    if not v.get("ok"):
        rep["error"] = "包不完整，不敢还原：%s" % (v.get("error") or "；".join(v["bad"][:3]))
        return rep

    with zipfile.ZipFile(p) as z:
        man = _load_manifest(z)
        todo, bad = [], []
        for e in man.get("entries") or []:
            dest, base, why = _member_target(e["name"], with_home)
            if dest is None:
                bad.append("%s：%s" % (e["name"], why))
                continue
            if no_secrets and e.get("tier") == T_SECRET:
                rep["skipped"].append("%s（钥匙类，这次不搬）" % e["name"])
                continue
            todo.append((e, dest, base))
        if bad and not todo:
            rep["error"] = "包里每一条都落不了地：%s" % "；".join(bad[:3])
            return rep
        rep["skipped"].extend(bad)

        # 会不会盖掉东西？先说清楚，没 --force 就停。
        clash = [(d, base) for _e, d, base in todo if d.exists()]
        if clash and not force:
            rep["error"] = ("会盖掉 %d 个已有文件（比如 %s）。确认要覆盖就加 --force；"
                            "想先看看有什么就 `list`。"
                            % (len(clash), clash[0][0]))
            return rep

        if clash:
            # ★ 覆盖之前把现场挪走。挪，不是复制 —— 复制在几百 MB 的会话上会卡很久，
            #   而我们要的只是"能退回去"。
            safe = P.data_root() / ".harness" / ("pre-restore-%s" % _tag())
            moved = 0
            for d, base in clash:
                try:
                    rel_d = d.relative_to(base)
                except ValueError:
                    continue
                dst = safe / base.name / rel_d
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(d), str(dst))
                    moved += 1
                except OSError as e:
                    rep["error"] = "留底挪不动 %s：%s（已停，没继续覆盖）" % (d, e)
                    return rep
            rep["backup"] = "%s（%d 个文件）" % (safe, moved)

        for e, dest, _base in todo:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = z.read(e["name"])
                dest.write_bytes(data)
                rep["restored"] += 1
                rep["bytes"] += len(data)
            except (OSError, KeyError) as ex:
                rep["error"] = "写到 %s 时失败：%s" % (dest, ex)
                return rep
    rep["ok"] = True
    return rep


def describe() -> dict:
    """一行状态：上次导的是什么、在哪、多大。给健康页用。"""
    d = P.data_root() / DEFAULT_SUBDIR
    out = {"dir": str(d), "exists": d.is_dir(), "latest": "", "mtime": 0,
           "size": 0, "copies": 0}
    if not d.is_dir():
        return out
    try:
        zs = sorted(d.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    except OSError:
        return out
    out["copies"] = len(zs)
    if zs:
        try:
            st = zs[0].stat()
            out.update({"latest": zs[0].name, "mtime": st.st_mtime,
                        "size": st.st_size})
        except OSError:
            pass
    return out
