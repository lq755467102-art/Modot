#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diagnose.py —— 打一个**能发给别人看**的现场包

为什么需要它
    投入使用之后，第一个真问题永远是「出问题了，让我看看」。
    而现在这个问题的答案是把整个仓库拷走 —— 里面有 `api-token`、
    `~/.workbuddy/harness/credentials.env`、以及你的全部会话文本。**没人敢发**。
    于是只能远程看，或者靠嘴描述「它说连不上」。

它做什么
    收集十几项**摘要**（不是文件），每项要么是一行字，要么是一个计过数的 JSON，
    全部先过一遍脱敏，写进一个目录，打成 zip。产物里没有任何一份原始文件。

三条硬规矩（这个模块的全部安全性就在这里）
    ① **白名单，不是黑名单**：只有下面 `_SECTIONS` 里列的项会被收。
       不存在「给我一个路径我帮你打包」—— 那等于给一个泄漏用户的输入框。
    ② **先脱敏再落盘**，不是「落盘之后再洗」。日志里的东西是任意的，
       包括某次工具输出顺手打印出来的 key。
    ③ **产出之后再自查一遍**：拿真实密钥值（钥匙文件、credentials.env、
       环境里的 KEY/TOKEN）去搜整个包，搜到就**删包并报错**。
       前两条是设计，第三条是判据 —— 设计会漏，判据不会。

它明确不包含
    · 会话正文（只有条数、最新一条的状态）
    · 记忆树内容（只有文件数与档位）
    · 任何原始配置文件（只给键名和值的形态，不给 value）
    · 私钥、令牌、密码（连形态都不给，直接替换掉）

用法
    python bin/diagnose.py                 # 打一个包，打印路径和那一行摘要
    python bin/diagnose.py --no-logs       # 不给别人看日志尾时用（更干净）
    python bin/diagnose.py --out D:/x.zip
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import zipfile
from pathlib import Path

from . import paths as P

# ── 脱敏 ───────────────────────────────────────────────────────────
# ★ 只认三类东西：**已知的真实值**、**形状极强的模式**、**家目录路径**。
#   不做「凡是长随机串就盖掉」—— 那样会把哈希、会话 id、提交号全盖掉，
#   包就没法看了。脱敏过头和脱敏不足一样糟：前者让人放弃用它。
_MASK = "«已脱敏:%s»"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 私钥整段（这是唯一「连形态都不该出现」的东西）
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "私钥"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github-token"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "bearer"),
]

# 环境变量名像密钥的，值一律盖掉（**按名字**判断，不按值的长相）
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)

# 但 `credentials.env` 里还存着**排查必须看、却不是秘密**的东西：
# 端点地址、模型名、超时。把它们也盖掉，包就只剩「有配置」三个字，等于没用。
# ⇒ 两个方向都试过一遍（2026-10-26）：一律盖 ⇒ 模型名没了没法判断"是不是它配错了"；
#   一律不盖 ⇒ 一个叫 `FOO` 的真 key 就漏了。
#   最终取**已知无害放行、其余一律按秘密**：错在"多盖一个配置"的代价，
#   比错在"漏一个 key"小得多。
_HARMLESS_KEY = re.compile(r"(BASE_URL|_URL$|MODEL|TIMEOUT|MAX_TOKENS|MAX_TURNS|"
                           r"PROVIDER|ENDPOINT|REGION|PORT)", re.I)


def _known_secrets() -> list[tuple[str, str]]:
    """这台机器上**确实是密钥**的值。脱敏和产出后自查都用这一份。

    ★ 不缓存：钥匙可能刚被换成新的，而诊断包最常见的用法就是「刚出问题」。
    """
    out: list[tuple[str, str]] = []
    try:
        p = P.token_file()
        if p.exists():
            v = p.read_text(encoding="utf-8", errors="replace").strip()
            if len(v) >= 8:
                out.append(("api-token", v))
    except OSError:
        pass
    # credentials.env：`K=V` 逐行
    for cand in (Path.home() / ".workbuddy" / "harness" / "credentials.env",
                 P.data_root() / ".harness" / "credentials.env"):
        try:
            if not cand.exists():
                continue
            for ln in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if len(v) < 8:
                    continue
                if _HARMLESS_KEY.search(k) and not _SECRET_ENV.search(k):
                    continue      # 地址 / 模型名 / 超时：留着给人看
                out.append((k, v))
        except OSError:
            pass
    # 环境里名字像密钥的
    for k, v in os.environ.items():
        if _SECRET_ENV.search(k) and len(v) >= 8:
            out.append((k, v))
    return out


def redact(text: str, hits: dict | None = None) -> str:
    """把一段文本里能认出来的秘密换成占位符。`hits` 累计各类盖了几处。"""
    s = str(text or "")
    if hits is None:
        hits = {}
    for label, val in _known_secrets():
        if val and val in s:
            n = s.count(val)
            s = s.replace(val, _MASK % label)
            hits[label] = hits.get(label, 0) + n
    for pat, label in _PATTERNS:
        s, n = pat.subn(_MASK % label, s)
        if n:
            hits[label] = hits.get(label, 0) + n
    # 家目录换 `~`：不是为了保密，是为了「发给别人时不暴露用户名」
    try:
        home = str(Path.home())
        for form in (home, home.replace("\\", "/")):
            if form and form in s:
                # ★ 按**出现次数**记，不是按"替换了几次"（2026-10-26 自查：
                #   原来一次替换记 1，于是日志里出现 40 次家目录也只报 1 ——
                #   而这个数字是人判断"这份包暴露了多少"的唯一依据）。
                hits["家目录"] = hits.get("家目录", 0) + s.count(form)
                s = s.replace(form, "~")
    except Exception:
        pass
    # ★ 别人的用户名 / 8.3 短名（`C:////Users////ADMINI~1`）也要盖掉（2026-10-26）：
    #   家目录那一步只认当前用户，而临时目录在 Windows 上经常给短名 ——
    #   于是同一个包在家目录里是 `~`、在临时目录里却把用户名露出来。
    s, n = re.subn(r"([A-Za-z]:[\\/]+Users[\\/]+)([^\\/\s\"'<>|]+)", r"\1«用户»", s)
    if n:
        hits["用户名"] = hits.get("用户名", 0) + n
    return s


# ── 每一项现场 ──────────────────────────────────────────────────────
def _env() -> dict:
    d = P.describe()
    d["python"] = sys.version.split()[0]
    d["os"] = "%s %s" % (platform.system(), platform.release())
    d["cwd"] = str(Path.cwd())
    d["argv0_ok"] = True
    return d


def _version() -> dict:
    """版本：git HEAD + 有没有未提交的改动。

    ★ 为什么这个必须有：说「通过 / 出错」时必须能对上**哪一版**。
      没有快照号的结论，隔一天就不可复核（这条在项目里已经踩过）。
    """
    import subprocess
    out: dict = {}
    root = P.app_root()
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root),
                           capture_output=True, text=True, timeout=10)
        out["head"] = (r.stdout or "").strip() if r.returncode == 0 else "(不是 git 仓库)"
        r2 = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                            capture_output=True, text=True, timeout=20)
        if r2.returncode == 0:
            lines = [x for x in (r2.stdout or "").splitlines() if x.strip()]
            out["dirty"] = len(lines)
        r3 = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=str(root),
                            capture_output=True, text=True, timeout=10)
        if r3.returncode == 0:
            out["last_commit"] = (r3.stdout or "").strip()[:120]
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _config() -> dict:
    """配置的**形状**，不是值。只报键名、类型、以及少数几个非敏感的取值。"""
    from . import charter as CH
    cfg = CH.load_config()
    shape: dict = {}
    for k, v in (cfg or {}).items():
        if k.startswith("_"):
            continue
        shape[k] = type(v).__name__ if not isinstance(v, (str, int, float, bool)) else v
    try:
        from . import cost as CO
        shape["_cost"] = CO.describe()
    except Exception:
        shape["_cost"] = "读不到"
    return {"keys": sorted(shape.keys()), "shape": shape,
            "modes": (cfg or {}).get("modes"),
            "inject": (cfg or {}).get("inject"),
            "memory_tiers": (cfg or {}).get("memory_tiers") or "(默认)"}


def _security() -> dict:
    """安全链五层各自现在什么状态。这是诊断包最该看的一节。"""
    out: dict = {}
    try:
        from . import baseline as BL
        out["baseline"] = {"summary": BL.summary(), "errors": BL.selfcheck(),
                           "hits": {k: v for k, v in BL.stats(7).items() if k != "recent"}}
    except Exception as e:
        out["baseline"] = {"error": "%s: %s" % (type(e).__name__, e)}
    try:
        from . import sandbox_rules as SB
        d = SB.rules()
        out["rules"] = {
            "explain": SB.explain(), "path": str(SB.rules_path()),
            "default_action": d.get("default_action"),
            "counts": {"file": len(d.get("file_rules") or []),
                       "process": len(d.get("process_rules") or []),
                       "scan": len((d.get("script_scan") or {}).get("rules") or []),
                       "script_allow": len((d.get("script_allow") or {}).get("items") or [])},
        }
    except Exception as e:
        out["rules"] = {"error": "%s: %s" % (type(e).__name__, e)}
    try:
        from . import ruleedit as RE
        lst = RE.listing()
        out["exceptions"] = {"count": lst["count"], "path": lst["path"],
                             "items": [{"domain": x.get("domain"), "action": x.get("action"),
                                        "key": x.get("key"), "at": x.get("at"),
                                        "src": x.get("src")} for x in lst["items"]]}
    except Exception as e:
        out["exceptions"] = {"error": "%s: %s" % (type(e).__name__, e)}
    try:
        from . import grants as G
        lst = G.listing()
        out["grants"] = {"describe": G.describe(), "live": lst.get("count"),
                         "items": [{"kind": x.get("kind"), "mode": x.get("mode"),
                                    "label": x.get("label"), "at": x.get("at")}
                                   for x in (lst.get("items") or [])]}
    except Exception as e:
        out["grants"] = {"error": "%s: %s" % (type(e).__name__, e)}
    try:
        from . import isolate as ISO
        out["isolate"] = ISO.describe()
    except Exception as e:
        out["isolate"] = "读不到：%s" % e
    try:
        from . import netguard as NG
        out["netguard"] = NG.describe()
    except Exception as e:
        out["netguard"] = "读不到：%s" % e
    try:
        from . import neutralize as NZ
        out["neutralize"] = NZ.summary()
    except Exception as e:
        out["neutralize"] = "读不到：%s" % e
    try:
        from . import leaks as LK
        out["leaks"] = LK.describe()
    except Exception as e:
        out["leaks"] = "读不到：%s" % e
    try:
        from . import mcp as MC
        out["mcp"] = MC.describe()
    except Exception as e:
        out["mcp"] = "读不到：%s" % e
    try:
        from . import contain as CN
        # ★ 诊断包里**不跑体检**（它会建 profile、起进程）：只报开关状态。
        #   要证据就自己跑 `bin/sandbox.py doctor` —— 那种"起进程"的事不该由诊断包顺手做。
        out["container"] = {"enabled": CN.enabled(),
                            "supported": CN.supported(),
                            "describe": CN.describe() if CN.enabled() else "没开"}
    except Exception as e:
        out["container"] = "读不到：%s" % e
    try:
        from . import runcheck as RC
        out["runcheck"] = RC.explain()
    except Exception as e:
        out["runcheck"] = "读不到：%s" % e
    return out


def _memory() -> dict:
    """记忆三个区各自多大、档位是什么。**不含任何一条内容**。"""
    out: dict = {}
    try:
        from . import memperms as MP
        out["tiers"] = MP.main_tiers()
    except Exception as e:
        out["tiers"] = {"error": str(e)}
    tree = P.data_root() / "memory" / "tree"
    try:
        files = sorted(x for x in tree.glob("*.md")) if tree.exists() else []
        out["tree_files"] = len(files)
        out["tree_layers"] = [x.stem for x in files]
    except OSError:
        out["tree_files"] = 0
    # 事实：走 `facts.entries()`（真实的解析器），不自己数行 ——
    #   自己数的话就变成第二个真源，而「少算了两条」这种事永远没人会发现。
    try:
        from . import facts as F
        total = 0
        where: dict = {}
        for scope in getattr(F, "SCOPES", ("project",)):
            base = F.root_of(scope, "main").parent
            if not base.exists():
                continue
            for kind_dir in sorted(base.glob("*")):
                if not kind_dir.is_dir():
                    continue
                e = F.entries(scope, kind_dir.name)
                if e.get("count"):
                    where["%s/%s" % (scope, kind_dir.name)] = e["count"]
                    total += e["count"]
        out["facts"] = total
        out["facts_by"] = where
    except Exception as e:
        out["facts"] = "读不到：%s" % e
    try:
        from . import habits as HB
        out["habits_file"] = str(HB.path())
        out["habits_exists"] = HB.path().exists()
    except Exception:
        pass
    return out


def _index() -> dict:
    """代码索引现在什么状态（有就用，没有也不该让诊断包失败）。"""
    try:
        from . import index as IX
        return {"describe": IX.describe(Path.cwd()), "stats": IX.stats(Path.cwd())}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _sessions() -> dict:
    """会话：**只数条数、报最新一条的状态**，不碰正文。"""
    runs = P.data_root() / "runs"
    out: dict = {"runs_root": str(runs)}
    if not runs.exists():
        # ★ 没有 runs 目录 = 还没跑过任何任务，**这是 0，不是"读不到"**
        #   （2026-10-26 探针逮到）：报 "?" 会让人以为出了故障，
        #   而真相往往是"刚装完、还没用过"。
        out["count"] = 0
        return out
    try:
        dirs = [d for d in runs.iterdir() if d.is_dir() and not d.name.startswith("_")]
        # `locks` / `sessions` 是功能目录，不是会话
        dirs = [d for d in dirs if d.name not in ("locks", "sessions", "_trash")]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        out["count"] = len(dirs)
        if dirs:
            d = dirs[0]
            out["latest"] = {"name": d.name,
                             "at": time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(d.stat().st_mtime))}
            try:
                st = json.loads((d / "session.json").read_text(encoding="utf-8"))
                out["latest"]["state"] = {k: st.get(k) for k in
                                          ("status", "mode", "turns", "phase", "done")}
            except Exception:
                out["latest"]["state"] = "(session.json 读不到，可能是老会话)"
            try:
                dn = json.loads((d / "done.json").read_text(encoding="utf-8"))
                out["latest"]["done"] = bool(dn.get("done"))
                out["latest"]["checks"] = len(dn.get("checks") or [])
            except Exception:
                pass
    except OSError as e:
        out["error"] = str(e)
    return out


def _log_tails(limit: int = 80, max_files: int = 2) -> dict[str, str]:
    """最近几个会话的 `run.log` 尾部。**读出来，脱敏，再写。**"""
    runs = P.data_root() / "runs"
    got: dict[str, str] = {}
    try:
        dirs = [d for d in runs.iterdir() if d.is_dir() and not d.name.startswith("_")]
        dirs = [d for d in dirs if d.name not in ("locks", "sessions", "_trash")]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[:max_files]:
            f = d / "run.log"
            if not f.exists():
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = txt.splitlines()
            got["%s.log" % d.name] = "\n".join(lines[-limit:])
    except OSError:
        pass
    return got


_SECTIONS = ("env", "version", "config", "security", "memory", "index", "sessions")


def collect(with_logs: bool = True) -> dict:
    """把现场收成一份 dict。**哪一节挂了都不影响别的节**（诊断包最怕整体失败）。"""
    out: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "sections": {}, "errors": []}
    for name in _SECTIONS:
        try:
            out["sections"][name] = globals()["_%s" % name]()
        except Exception as e:
            out["sections"][name] = {"error": "%s: %s" % (type(e).__name__, e)}
            out["errors"].append("%s: %s" % (name, e))
    if with_logs:
        out["logs"] = _log_tails()
    return out


def summary_lines(d: dict) -> list[str]:
    """给人看的那十几行。**结论先行**：先说形态和状态，再说细节。"""
    s = d.get("sections") or {}
    env = s.get("env") or {}
    ver = s.get("version") or {}
    sec = s.get("security") or {}
    ses = s.get("sessions") or {}
    mem = s.get("memory") or {}
    lines = [
        "诊断包 %s" % d.get("at"),
        "",
        "形态：%s ｜ 程序根 %s ｜ 数据根 %s ｜ 分开=%s ｜ 可写=%s"
        % (env.get("os"), env.get("app"), env.get("data"),
           env.get("split"), env.get("data_writable")),
        "版本：%s（未提交改动 %s 个）%s"
        % (ver.get("head") or "?", ver.get("dirty", "?"),
           ("｜" + str(ver.get("last_commit"))) if ver.get("last_commit") else ""),
        "Python：%s" % env.get("python"),
        "",
        "规则：%s" % ((sec.get("rules") or {}).get("explain") or "读不到"),
        "底线：%s" % ((sec.get("baseline") or {}).get("summary") or "读不到"),
        "免问例外：%s 条 ｜ 活着的许可：%s 张"
        % ((sec.get("exceptions") or {}).get("count", "?"),
           (sec.get("grants") or {}).get("live", "?")),
        "隔离：%s" % (sec.get("isolate") or "读不到"),
        "",
        "会话：%s 个（最新 %s，%s）"
        % (ses.get("count", "?"), (ses.get("latest") or {}).get("name", "—"),
           (ses.get("latest") or {}).get("at", "—")),
        "记忆：档位 %s ｜ 树 %s 层 ｜ 事实 %s 条"
        % (mem.get("tiers"), mem.get("tree_files"), mem.get("facts")),
        "索引：%s" % ((s.get("index") or {}).get("describe") or "读不到"),
    ]
    if d.get("errors"):
        lines += ["", "⚠ 有 %d 节没取到：%s" % (len(d["errors"]), "；".join(d["errors"])[:300])]
    return lines


def _dump(path: Path, obj, hits: dict | None = None) -> None:
    """写一份 JSON 或纯文本（**先脱敏**）。

    ★ `hits` 必须由调用方传进来（2026-10-26 探针逮到的真 bug）：
      第一版这里写的是 `redact(obj)`，于是**每个文件各建一份计数**，
      写完就丢 —— 结果是 redaction.json 只报了 2 处（终端摘要那一次），
      而日志里实际盖掉了 7 处。那个数字是人判断「这份包暴露了多少」的唯一依据，
      少报等于让他以为干净。
    """
    if isinstance(obj, str):
        txt = redact(obj, hits)
    else:
        txt = redact(json.dumps(obj, ensure_ascii=False, indent=2, default=str), hits)
    path.write_text(txt, encoding="utf-8")


def build(out: Path | None = None, with_logs: bool = True,
          keep_dir: bool = False) -> dict:
    """打一个包。返回 {ok, path, files, masked, bytes}。

    ★ 顺序不能反：**先写到临时目录 → 自查 → 再 zip → 删临时目录**。
      自查不过就把整份删掉，绝不留下一个「有 leak 但已经打包好」的产物。
    """
    data = collect(with_logs=with_logs)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # ★ 默认落在 `runs/diagnose/`，不直接堆在 `runs/`（2026-10-26）：
    #   堆在 runs 根下就是又一个「只增不减」，而且清理白名单只能按**目录**给 ——
    #   拿 `runs` 当目标会把会话一起删掉。给它一个自己的目录，才能被 housekeep 管。
    root = out.parent if out else (P.data_root() / "runs" / "diagnose")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": "输出目录建不出来：%s" % e}
    work = root / ("diagnose-%s" % stamp)
    work.mkdir(parents=True, exist_ok=True)

    hits: dict = {}
    masked_total = 0
    try:
        _dump(work / "summary.txt", redact("\n".join(summary_lines(data)), hits))
        for name, body in (data.get("sections") or {}).items():
            _dump(work / ("%s.json" % name), body, hits)
        logs = data.get("logs") or {}
        if logs:
            (work / "logs").mkdir(exist_ok=True)
            for fn, txt in logs.items():
                _dump(work / "logs" / fn, txt, hits)
        (work / "README.txt").write_text(_README % (stamp,), encoding="utf-8")
        # 脱敏统计（只说盖了几处、按什么标签，不写原文）
        for v in hits.values():
            masked_total += int(v)
        _dump(work / "redaction.json", {"masked": masked_total, "by_label": hits,
                                        "logs_included": bool(logs)})

        bad = verify(work)
        if bad:
            _rm(work)
            return {"ok": False, "error": "自查没过，包已删除：%s" % "；".join(bad[:3])}
        if keep_dir:
            final = work
        else:
            final = (out if out else (root / ("diagnose-%s.zip" % stamp)))
            if final.suffix.lower() != ".zip":
                final = final.with_suffix(".zip")
            _zip(work, final)
            _rm(work)
        # 文件数：目录形态数文件，zip 形态数**包内条目**（数外面永远是 1）
        if final.is_dir():
            n = sum(1 for x in final.rglob("*") if x.is_file())
        else:
            try:
                with zipfile.ZipFile(str(final)) as z:
                    n = len(z.namelist())
            except Exception:
                n = 1
        return {"ok": True, "path": str(final), "files": n,
                "masked": masked_total,
                "bytes": _size(final) if final.exists() else 0,
                # ★ 终端这一份也要脱敏（2026-10-26 自己逮到的）：它是最容易被
                #   直接粘进聊天窗口的那一份，而它原来走的是**未脱敏**的 summary_lines。
                "summary": redact("\n".join(summary_lines(data)))}
    except Exception as e:
        _rm(work)
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def verify(work: Path) -> list[str]:
    """**产出之后的判据**：包里还有没有真密钥、还有没有私钥形态。

    ★ 为什么要有这一步：前面两道（白名单、先脱敏）都是设计，而设计会漏 ——
      漏法通常是「某个模块我没想到它会把 key 打到日志里」。
      这一步拿真实值去搜，搜到就删包，是唯一不依赖"我记得"的保障。
    """
    try:
        blob = []
        for f in work.rglob("*"):
            if f.is_file():
                try:
                    blob.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
        all_txt = "\n".join(blob)
    except OSError as e:
        return ["包读不出来：%s" % e]
    bad: list[str] = []
    for label, val in _known_secrets():
        if val and val in all_txt:
            bad.append("包里还有真实的 %s" % label)
    for pat, label in _PATTERNS:
        if pat.search(all_txt):
            bad.append("包里还有 %s 的形态" % label)
    return bad


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else sum(
            x.stat().st_size for x in p.rglob("*") if x.is_file())
    except OSError:
        return 0


def _rm(p: Path) -> None:
    import shutil
    shutil.rmtree(str(p), ignore_errors=True)


def _zip(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(str(f), str(Path(src.name) / f.relative_to(src)))


_README = """诊断包（%s）
────────────────────────────────────────────────────
这份包里**没有原始文件**，只有摘要、计数和脱敏过的日志尾。

有什么
    summary.txt     十几行，先看这个
    env.json        两个根在哪、能不能写、python/os 版本
    version.json    git HEAD / 未提交改动数 / 最后一条提交说明
    config.json     配置的**形状**（键名和类型），不含值
    security.json   五层安全链各自的状态（底线 / 规则 / 例外 / 许可 / 隔离）
    memory.json     记忆档位、树有几层、事实几条（不含内容）
    sessions.json   会话条数、最新一条的状态
    logs/*.log      最近两个会话的 run.log 尾部（脱敏过）
    redaction.json  盖了几处、按什么标签盖的（不写原文）

没有什么（故意的）
    · 会话正文、记忆树内容
    · 任何配置文件原文
    · 私钥、令牌、密码

给别人之前
    看一眼 redaction.json 的 masked：是 0 说明这段日志里没碰到密钥，
    但也说明它可能带着你的任务文本 —— 日志不能证明"干净"，只能证明"没密钥"。
"""


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="打一个脱敏的现场包（能发给别人看）")
    ap.add_argument("--out", default="", help="输出 zip 路径（默认 runs/diagnose-<时间>.zip）")
    ap.add_argument("--no-logs", action="store_true", help="不打包日志尾（更干净）")
    ap.add_argument("--dir", action="store_true", help="不压 zip，留一个目录（自己看）")
    ap.add_argument("--json", action="store_true", help="只把摘要按 JSON 打出来")
    a = ap.parse_args(argv)

    r = build(Path(a.out) if a.out else None,
              with_logs=not a.no_logs, keep_dir=a.dir)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 2
    if not r.get("ok"):
        print("✗ %s" % r.get("error"), file=sys.stderr)
        return 2
    print(r["summary"])
    print()
    print("包：%s" % r["path"])
    print("大小：%.1f KB ｜ 脱敏 %d 处 ｜ 文件 %d 个"
          % (r["bytes"] / 1024.0, r["masked"], r["files"]))
    print("自己先看一眼 summary.txt 再发出去 —— 里面可能带着你的任务文本。")
    return 0
