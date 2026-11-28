#!/usr/bin/env python3
"""server.py —— 把 harness 包成本地 API，任何前端都能接

为什么非要这一层
    「发布出去的应用」不能是「一个 Python 脚本 + 一个窗口」：
    界面和内核焊死在一起，换前端要重写，多窗口不可能，将来要远程也不可能。
    拆成「后端提供 API / 前端只消费」之后：
      · 现在用 Electron 做窗口，将来换网页、换平板都不用动内核；
      · 界面崩了不影响任务（任务在独立进程里跑，界面只是读者）；
      · 同一个人可以开两个窗口看同一个会话。

    ★ 这条边界也回答了另一个问题：**界面永远不持有状态**。
      所有答案都从文件来（events.jsonl / charter.json / done.json），
      接口只做"聚合与转述"，不做缓存。界面显示的和实际发生的不一致，
      是最坏的一类 bug —— 人照着它做决定，它在骗人。

接口（全部 /api 前缀，无状态）
    GET  /api/health                    活着没
    GET  /api/state                     档位、每轮字符、预算、带了哪几层
    GET  /api/sessions?limit=50         会话列表（按修改时间倒序）
    GET  /api/sessions/<id>/events?after=N   增量事件（N 之后的部分）
    POST /api/sessions                  {"task": "..."} → {"session": "app-..."}
    POST /api/sessions/<id>/stop        请求暂停（写 control.json）
    GET  /api/sessions/<id>/summary     这一轮的索引（如果写过）
    POST /api/mode                      {"mode": "lean", "confirm": false}

安全（三层，别混着算）
    ① 只绑 127.0.0.1 —— 局域网里别人碰不到。
    ② **有鉴权**：没有钥匙的请求一律 401（`X-Harness-Token` 或 `Authorization: Bearer`）。
       钥匙在数据目录的 `api-token` 里，外壳开窗口时放进 query 带进来，页面读完就从地址栏抹掉。
       命令行看钥匙：`python bin/server.py --token`。
    ③ CORS 保留 `*` **不是**安全边界（`file://` 的 Origin 是 `null`，按来源白名单等于谁都放），
       真正的边界是 ②。别把这一条当成"只允许本机页面"。
    ★ 这条说明曾经写着"现在没有鉴权、发布前必须补"—— 鉴权 2026-10-26 就做了，
      文档晚了一天才改。以后改安全相关实现，顺手改这一段。

用法
    python bin/server.py --port 8788
"""
# 主要引用或加载入口包括 `owned-process.py` 和 `harness_core.py`。

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ★ 程序根 / 数据根（2026-10-26）：打包后代码在 asar 里（只读，python 还读不到），
#   而 runs/、memory/、.harness/*.json 都要写 ⇒ 必须分成两个。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loop import paths as P                       # noqa: E402
APP = P.app_root()
ROOT = P.data_root()                              # 数据根（开发时 == 程序根）

from loop import charter as CH
from loop import control as CTRL        # noqa: E402
from loop import protocol as PROTOCOL
from loop import event_index as EI
from loop import runtime as RT
from loop import isolate as ISO
from loop import operations as OPS
from loop.core_runtime import command as core_command
from loop.filelock import atomic_json, FileLock
from loop.session import fmt_event      # noqa: E402

INSTANCE_ID = uuid.uuid4().hex
RUNS = ROOT / "runs"
JOBS = RUNS / "jobs"


# ── 任务委托（`job.py` 那一套，2026-10-29 文档《前端接什么》）────────────────
# ★ 原则照文档：**前端只读 JSON，不解析命令行的 stdout**。
#   索引 `jobs-index.jsonl` 是缓存（`job.py index` 能重建），
#   单个 job 的 `verdict.json` 才是裁定的真源 —— 列表里以它覆盖索引。
def _read_json(p, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _managed_job(directory, metadata=None):
    metadata = _read_json(directory / "job.json", {}) if metadata is None else metadata
    return bool((metadata or {}).get("managedConversation") or (directory / "delegation.json").exists())


def _jobs_index():
    # 索引只是缓存；job 目录才是事实。新建但未裁定的 L4 也必须立即可见。
    rows = {}
    idx = RUNS / "jobs-index.jsonl"
    if idx.exists():
        for line in idx.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("id"):
                    rows[r["id"]] = r
            except Exception:
                pass
    try:
        dirs = [d for d in JOBS.iterdir() if d.is_dir()]
    except OSError:
        dirs = []
    for d in dirs:
        meta = _read_json(d / "job.json", {}) or {}
        if _managed_job(d, meta):
            rows.pop(d.name, None)
            continue
        summary = _read_json(d / "summary.json", {}) or {}
        task = ""
        try:
            task = (d / "task.md").read_text(encoding="utf-8").strip().splitlines()[0][:100]
        except OSError:
            pass
        r = dict(rows.get(d.name) or {})
        r.update({"id": d.name, "task": task or r.get("task") or d.name,
                  "crew": meta.get("crew") or summary.get("crew") or r.get("crew") or "",
                  "rounds": summary.get("n_rounds") or len(summary.get("rounds") or []),
                  "max_rounds": meta.get("max_rounds") or summary.get("max_rounds") or 0,
                  "phase": _job_phase(d.name)})
        last = (summary.get("rounds") or [None])[-1]
        if isinstance(last, dict) and isinstance(last.get("contract"), dict):
            c = last["contract"]
            r["passed"] = c.get("passed") if c.get("passed") is not None else c.get("n_passed")
            r["total"] = c.get("total") if c.get("total") is not None else c.get("n_total")
        vd = _read_json(d / "verdict.json")
        if vd:
            r["verdict"] = vd.get("verdict") or ""
            r["reason"] = vd.get("reason") or ""
            r["verdict_at"] = vd.get("at") or ""
            r["stop"] = vd.get("stop") or ""
        else:
            r.pop("verdict", None)
        r["pending"] = not bool(r.get("verdict"))
        rows[d.name] = r
    return {"jobs": sorted(rows.values(),
                           key=lambda r: r.get("verdict_at") or r.get("id") or "", reverse=True)}


def _job_active_round(d: Path) -> int:
    """这个委托现在正在写哪一轮。0 = 没有在执行的轮次。

    ★ 不从 summary.json 猜：它只在**一轮结束后**重写，所以运行中的那一轮
      永远不在里面。这里看每轮自己的 session.json —— 它就是那一轮的实时状态。
    """
    best = 0
    for p in d.glob("r[0-9]*"):
        if not p.is_dir():
            continue
        try:
            n = int(re.sub(r"\D", "", p.name))
        except ValueError:
            continue
        s = _read_json(p / "session.json", {}) or {}
        if str(s.get("status") or "") == "running":
            best = max(best, n)
    return best


def _job_watch(jid: str, limit: int = 5) -> list:
    """这个委托最近被观察哨记到的争用（只读 `runs/watch.jsonl` 的尾巴）。

    ★ 为什么读文件而不问哨兵：哨兵是旁路线程，重启就没了。
      而"谁和谁撞过"是**历史事实**，事实留在盘上比留在内存里可靠。
    """
    p = RUNS / "watch.jsonl"
    out = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except OSError:
        return out
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if jid in (r.get("jobs") or []):
            _jobs = r.get("jobs") or []
            out.append({"at": r.get("at"), "path": r.get("path"),
                        "jobs": _jobs,
                        # ★ 界面上"争用：自己 / 对方"读起来像在跟自己撞 ——
                        #   这里替它分好：`others` 才是人想看的那个名字。
                        "others": [x for x in _jobs if x != jid],
                        "delivered": r.get("delivered") or []})
        if len(out) >= limit:
            break
    return out


# 把任务配置和已记录的轮次状态合为宿主概览。
def _job_overview(jid):
    d = JOBS / jid
    if not d.is_dir():
        return {"error": "没有这个委托"}
    return {"id": jid,
            "job": _read_json(d / "job.json", {}),
            "summary": _read_json(d / "summary.json", {}),
            "phase": _job_phase(jid),
            "active_round": _job_active_round(d),
            "watch": _job_watch(jid),
            "verdict": _read_json(d / "verdict.json")}


def _read_text(p: Path, limit: int = 80000) -> str:
    try:
        s = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 截断必须说明：人看到"正文到一半没了"会以为模型没写完。
    if len(s) > limit:
        return s[:limit] + "\n\n…（正文过长，界面只显示前 %d 字符；全文在 %s）" % (limit, p)
    return s


# 用原任务及轮次号从已保存目录定位轮次。
def _job_round(jid, n):
    d = JOBS / jid / ("r%d" % n)
    return {"memo": _read_json(d / "memo.json"),
            "tester": _read_json(d / "benni-record.json"),
            "changes": _read_json(d / "changes.json"),
            "summary": _read_json(d / "summary.json"),
            "session": _read_json(d / "session.json"),
            # ★ 正文和测试者全文（2026-11-01）：任务页以前只给「成功/失败 + 契约 n/m」，
            #   人看不到这一轮到底写了什么、测了什么。正文必须能从接口读到，
            #   前端才能把它按轮次放进主内容区，而不是让人去 runs/ 里翻文件。
            "out": _read_text(d / "out.md"),
            "tester_md": _read_text(d / "benni-model.md", 40000)}


def _job_round_events(jid, n, after=0, offset=0, limit=0):
    """一轮的实时事件（给「正文里逐轮展开」用，不是命令行的 stdout）。

    ★ 只做转述，不在这里解释：`line` 由 `fmt_event` 统一生成，
      前端用它做工具行；正文/思考仍读 `text` 原文。
    ★ `after` 是增量游标。一轮 events.jsonl 可以上千条，不能每秒整份重传。
    ★ `offset/limit` 是分页（2026-11-01 读协议）：`events` 就是 items，
      另给 `total` 和 `next_offset`（null = 没有了）。limit=0 保持旧行为：全给。
    """
    d = JOBS / jid / ("r%d" % n)
    rows = _events(d, max(0, int(after or 0)))
    # ★ 只给界面需要的字段：工具事件里带着完整 args/out，一轮能有几百 KB。
    #   正文流要的是「做了什么、成功没有」，不是把原始 JSON 再搬一遍。
    keep = ("seq", "t", "act", "kind", "role", "turn", "text", "tool",
            "summary", "ok", "reason", "status", "why", "note", "running",
            "changed", "added", "removed", "scanned", "count", "verdict")
    slim = []
    for r in rows:
        row = {k: r.get(k) for k in keep if k in r}
        if isinstance(row.get("text"), str):
            row["text"] = row["text"][:8000]
        slim.append(row)
    s = _read_json(d / "session.json", {}) or {}
    last = max([int(after or 0)] + [int(r.get("seq") or 0) for r in rows])
    total = len(slim)
    nxt = None
    if limit and int(limit) > 0:
        from loop.paged import page
        pg = page(slim, offset=offset, limit=limit, max_limit=200)
        slim = pg["items"]
        nxt = pg["next_offset"]
    return {"round": n, "events": slim, "after": last,
            "total": total, "offset": int(offset or 0), "next_offset": nxt,
            "status": s.get("status") or "", "turns": s.get("turns") or 0,
            "why": s.get("why") or ""}


# 读回任务验收约定、锁定及判定结果，供旧管理面查看。
def _job_contract(jid):
    d = JOBS / jid / "contract"

    def _mt(p):
        try:
            return p.stat().st_mtime
        except Exception:
            return 0
    # ★ 过期的检查比没有更坏（文档第 6 节）：契约 amend 之后，
    #   probe/crosscheck 说的是**旧题目**，要标"已过期"，别当有效。
    stale = _mt(d / "done.json") > max(_mt(d / "probe.json"),
                                       _mt(d / "crosscheck.json"))
    return {"done": _read_json(d / "done.json"),
            "lock": _read_json(d / "done.lock"),
            "probe": _read_json(d / "probe.json"),
            "crosscheck": _read_json(d / "crosscheck.json"),
            "stale": stale}

_WT_LOCK = RUNS / "watchtower.pid"
_WT_ON = False


def _watchtower_start(every: int = 10) -> bool:
    """起一个常驻线程，盯着"两个委托同时改同一个文件"（实现见 `bin/watchtower.py`）。

    ★ 为什么放在服务里，而不是等人手起一条命令：
      "等人想起来去查一次"正是这件事现在失效的原因 —— 它必须自己在那儿。
    ★ 为什么还要 pid 心跳：同时开两个服务（8799 / 8788）会有两个哨兵，
      同一件事的通知会投两遍。60 秒内有心跳的那个算活的，后来的不起。
      判据和租约同一条：**看心跳，不看文件在不在**。
    ★ 关掉的办法：`HARNESS_WATCHTOWER=0`。它是旁路，不该在排查问题时挡路。
    """
    global _WT_ON
    if str(os.environ.get("HARNESS_WATCHTOWER", "1")) == "0":
        return False
    try:
        if _WT_LOCK.exists() and (time.time() - _WT_LOCK.stat().st_mtime) < 60:
            return False
    except OSError:
        pass
    try:
        from bin import watchtower as _WT
    except Exception:
        return False

    def _loop():
        while True:
            try:
                _WT.scan_once()
            except Exception:
                pass                 # 哨兵自己出错不许带崩服务
            try:
                _WT_LOCK.parent.mkdir(parents=True, exist_ok=True)
                _WT_LOCK.write_text(str(os.getpid()), encoding="utf-8")
            except OSError:
                pass
            time.sleep(max(2, every))

    threading.Thread(target=_loop, daemon=True, name="watchtower").start()
    _WT_ON = True
    return True


GFLOW_CFG = ROOT / "harness" / "gitflow.json"
GFLOW_DEFAULT = {
    "codeowners": ("# 目录级负责人：谁最懂这块，谁就是这块的默认评审人\n"
                   "# 例：app/       @you\n"
                   "#     docs/      @colleague\n"),
    "pr_template": ("## 这次改了什么\n\n"
                    "## 什么时候 rebase 到最新 main 的\n\n"
                    "## 跑了什么测试（结果）\n\n"
                    "## 有没有冲突，怎么取舍的\n"),
}


def _gflow_config() -> dict:
    d = _read_json(GFLOW_CFG, {}) or {}
    return {"codeowners": str(d.get("codeowners") or GFLOW_DEFAULT["codeowners"]),
            "pr_template": str(d.get("pr_template") or GFLOW_DEFAULT["pr_template"])}


def _gflow_config_save(body: dict) -> tuple[int, dict]:
    co = str(body.get("codeowners") or "")
    pr = str(body.get("pr_template") or "")
    if len(co) > 20000 or len(pr) > 20000:
        return 400, {"error": "模板太长了（单份上限 20000 字）"}
    try:
        GFLOW_CFG.parent.mkdir(parents=True, exist_ok=True)
        GFLOW_CFG.write_text(json.dumps({"codeowners": co, "pr_template": pr},
                                        ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
    except OSError as e:
        return 500, {"error": "写不进去：%s" % e}
    return 200, {"ok": True, "note": "模板已保存（harness/gitflow.json）"}


def _job_phase(jid: str) -> str:
    d = JOBS / jid
    recovered = RT.status(d)
    if recovered["active"]:
        return "recovering" if recovered["state"] == "unknown" else recovered.get("phase", "running")
    if recovered["state"] == "unknown":
        return "interrupted"
    if not (d / "contract" / "done.json").exists():
        return "draft_failed" if (d / "plan.log").exists() else "new"
    if not (d / "contract" / "done.lock").exists():
        return "awaiting_lock"
    if (d / "verdict.json").exists():
        return "judged"
    summary = _read_json(d / "summary.json", {}) or {}
    if summary.get("stop"):
        return "stopped"
    return "ready"


# ★ 门禁级别是**进程级配置**，不是每请求参数。改它要重启服务 ——
#   这是刻意的：放宽权限应该是"人做的一个动作"，不是"某个请求带了个字段"。
POLICY = "workspace"
POLICY_CHOICES = ("readonly", "workspace", "trusted")
POLICY_CN = {"readonly": "只读", "workspace": "工作目录内", "trusted": "信任·底线仍在"}

# 每个被这个服务启动的会话 → Popen。**只用来判断"还在不在跑"和"要不要回收"**，
# 不存任务内容、不存进度 —— 那些都在文件里。
PROCS: dict[str, subprocess.Popen] = {}


class WorkbenchHTTPServer(ThreadingHTTPServer):
    # Buffer simultaneous renderer connections while the server accepts them.
    request_queue_size = 64

    # 定期核对本机持有的会话子进程，回收已退出项并处理其待续接状态。
    def service_actions(self):
        if not PROCS:
            return
        now = time.monotonic()
        if now < getattr(self, "_next_process_reap", 0):
            return
        self._next_process_reap = now + 0.5
        from loop import agent_gateway, conversation_record, workbench

        # Share the launch lock so child-task callers can first take a reference
        # to their original process. Existing waiters retain their own handle.
        if not workbench._lock.acquire(blocking=False):
            return
        try:
            for ident, process in list(PROCS.items()):
                if process.poll() is not None:
                    agent_gateway.retire(ident, pid=process.pid)
                    del PROCS[ident]
                    conversation_record.schedule(RUNS / ident)
        finally:
            workbench._lock.release()

# ── 接口的钥匙（2026-10-26）─────────────────────────────────────────────
# ★ 为什么必须有：这个服务能读任意会话、改凭据、起任务、跑命令。
#   它挂在 127.0.0.1 上，而 127.0.0.1 **不等于"只有这个应用能调"** ——
#   本机任何程序、以及浏览器里的一页恶意 JS（同源策略挡不住"发出去"）都能打它。
#   所以"本机 = 安全边界"这个假设是不成立的，钥匙是补这一刀用的。
#
# ★ 钥匙放哪：`data_root()/api-token`（打包后在用户目录，开发时在仓库根，已进 .gitignore）。
#   每台机器一份，不是写死的常量 —— 写死等于没有。
# ★ `HARNESS_TOKEN` 可以覆盖它：验收和一个进程的测试用得上（也让"改钥匙"这件事可测）。
# ★ 唯一的白名单是 `GET /api/health`（只回"我在跑"）：外壳要靠它判断服务起没起，
#   而那时候它还没有钥匙可读。它**不返回任何路径/数据**；要看细节得带钥匙问 `?full=1`。
TOKEN_PATH = P.token_file()
TOKEN = ""            # 进程内一份；main() 里定，别在每次请求时读盘


def load_token(mint: bool = True) -> tuple[str, str]:
    """定这把钥匙：(token, 来源)。来源写进启动日志，别让人猜"到底认哪一把"。"""
    env = (os.environ.get("HARNESS_TOKEN") or "").strip()
    if env:
        return env, "环境变量 HARNESS_TOKEN"
    try:
        if TOKEN_PATH.exists():
            t = TOKEN_PATH.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                return t, str(TOKEN_PATH)
    except OSError:
        pass
    if not mint:
        return "", ""
    import secrets
    t = secrets.token_urlsafe(24)
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(t + "\n", encoding="utf-8")
    except OSError as e:
        # ★ 写不进去要说，而且**不能退回"没钥匙也能用"** —— 那样失败的方式最坏：
        #   看起来一切正常，实际上门是开的。这里直接让调用方把服务起失败。
        print("✗ 钥匙写不进去（%s）：%s" % (TOKEN_PATH, e), file=sys.stderr)
        raise
    return t, str(TOKEN_PATH)


def _is_sess_dir(d: Path) -> bool:
    """这像个会话目录吗。

    ★ 不能只认 events.jsonl：任务在建 Session 之前就失败（比如缺凭据，
      provider 初始化在 run-task 里排在 session 前面）时，目录里只有 task.md 和 run.log。
      只认 events.jsonl 的后果是——**恰恰是失败的那些，你查不到**，
      而它们才是最需要看的。

    ★ 下划线开头的**一律不算**：那是我们自己的临时目录
      （验收的 `_t_*`、回收站 `_trash`、日志 `_acc`）。
      不加这条的后果实测过：跑一次验收，左栏就会冒出一串陌生会话、
      跑完又自己消失 —— 人看到的是"它莫名其妙开了些会话又关了"。
      真实会话 id 是 `app-YYYYMMDD-HHMMSS` 或人起的名字，不会以下划线开头。
    """
    return any((d / f).exists() for f in ("events.jsonl", "run.log", "task.md"))


def _is_visible(d: Path) -> bool:
    """要不要出现在**左栏列表**里。比 `_is_sess_dir` 更严，两个方向必须分清：

    ★ 2026-10-26 修的是「**看得见但打不开**」：原来只看 `d.name`，
      于是 `runs/_trash/api-fake-123` 漏进列表（带下划线的是它父目录），
      点删除报「没有这个会话」—— 因为列表用 rglob 扫两层、删除只认一层。

    现在的分工：
      · **看得见 ⊆ 打得开**：列表只放 `runs/<id>` 这一层、且路径上任何一段都不带下划线；
      · 反过来"打得开但看不见"是允许的 —— 验收/探针的 `_probe_*` 目录要能自己调接口，
        但不该占左栏。这两个方向不要混成一个判定。
    """
    try:
        rel = d.relative_to(RUNS)
    except ValueError:
        return False
    if len(rel.parts) != 1:
        return False
    if any(part.startswith("_") for part in rel.parts):
        return False
    return _is_sess_dir(d)


def _resolve_sid(sid: str) -> Path | None:
    """会话 id → 目录。只允许 runs/ 下面一层，不许 `../` 跳出去。

    ★ 这不是洁癖：接口会把目录里的文件内容读出来给人看，
      路径可控就等于任意文件读取。发布前必须守住这条。
    """
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        return None
    d = RUNS / sid
    return d if d.is_dir() and _is_sess_dir(d) else None


def _legacy_events(d: Path, after: int) -> list[dict]:
    """把**老格式轨迹**（`trace.jsonl`）折成事件列表。

    ★ 为什么需要（2026-10-26，用户问「这些会话是什么情况，有些是一片空白」）：
      10-24 之前的会话，轨迹是一整行 JSON（`{status, final, actions:[...]}`），
      循环结束才落盘；之后的改成了逐行事件 `events.jsonl`（带 `seq`）。
      这里只读 `events.jsonl`，于是 10-24 那批会话**列表里有、点开全白** ——
      文件明明在，人看到的是"这个会话什么都没发生"。
      ★ 这和「文件被删了」长得一样，但处置完全不同：前者要救，后者要重跑。

    折法：动作数组按下标编号当 `seq`；末尾把 `final` 补成一条 say
    （老轨迹的 final 只在对象顶层，动作数组里可能没有对应那条）。
    """
    p = d / "trace.jsonl"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    acts = raw.get("actions") or []
    if not isinstance(acts, list):
        return []
    out: list[dict] = []
    seq = 0
    last_say = ""
    for a in acts:
        if not isinstance(a, dict):
            continue
        seq += 1
        r = dict(a)
        r["seq"] = seq
        r["legacy"] = True
        if r.get("act") == "say":
            last_say = str(r.get("text") or "")
        out.append(r)
    fin = str(raw.get("final") or "")
    if fin and fin.strip() != last_say.strip():
        seq += 1
        out.append({"seq": seq, "t": "", "act": "say", "text": fin, "legacy": True})
    if raw.get("status"):
        seq += 1
        # ★ 结尾补一条 end：列表的状态列读的是它，不补的话老会话永远是"未知"。
        out.append({"seq": seq, "t": "", "act": "end",
                    "status": raw.get("status"), "why": raw.get("why") or "",
                    "turns": raw.get("turns") or 0, "legacy": True})
    return [r for r in out if (r.get("seq") or 0) > after]


# 从持久事件流按原游标返回可重放页，不以现有进程是否启动决定历史可用性。
def _events(d: Path, after: int, limit: int = 0) -> list[dict]:
    out = []
    p = d / "events.jsonl"
    if not p.exists():
        # ★ 老格式也要算 `line`（人话摘要）。只折成事件不给摘要的话，
        #   界面上那些行会是一串没有标题的工具调用 —— "救回来了"和"救了一半"差别很大。
        rows = _legacy_events(d, after)
        for r in rows:
            r["line"] = fmt_event(r)
        return rows
    indexed, _ = EI.rows(p, after=after, limit=limit)
    for r in indexed:
        r["line"] = fmt_event(r)
        out.append(r)

    return out


def _tail_status(ev: Path, size: int = 8192) -> str:
    """从事件文件**尾部**推断这个会话现在是什么状态。

    返回 "needs_human" / "success" / "error" / "paused" / ""（说不清）。

    ★ 为什么只看尾部：状态由**最后一条**有结论意义的事件决定（end / ask_human），
      之前的历史再多也不影响它。全文读一遍纯属浪费 —— 而这是一个每秒都要跑的接口。

    ★ 为什么优先找 end：loop 里 ask_human 之后紧接着就会 _done("needs_human")，
      它会再 emit 一条 end(status=needs_human)。所以正常情况下"最后一条 end"就是答案。
      只有两种情况需要看 ask_human：
        ① 采访那条路（run-task 先 emit ask_human，再 emit status），
        ② 进程在两步之间被杀了。
    """
    try:
        with ev.open("rb") as f:
            f.seek(0, 2)
            total = f.tell()
            f.seek(max(0, total - size))
            raw = f.read()
    except OSError:
        return ""
    lines = raw.decode("utf-8", errors="replace").splitlines()
    # 从中间切进来的第一行可能是半条（字节位置不对齐）—— 丢掉，免得解析出半截 JSON
    if total > size and lines:
        lines = lines[1:]
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        act = e.get("act")
        if act == "end":
            return str(e.get("status") or "")
        if act == "ask_human":
            # 见到它还没见到 end ⇒ 它停在这儿等人（或者进程刚好停在这一步）
            return "needs_human"
    return ""


# 汇总可见的历史会话与当前运行记录，保留分页与归属。
def _sessions(limit: int) -> list[dict]:
    rows = []
    if not RUNS.is_dir():
        return rows
    # ★ 只看直接子目录（2026-10-26）：和 `_resolve_sid` 同一套范围。
    #   原来用 rglob 扫两层，但删除/打开只认一层 ⇒ "看得见、点不动"。
    for d in sorted(RUNS.iterdir() if RUNS.is_dir() else []):
        if not d.is_dir() or not _is_visible(d):
            continue
        stamps = [ (d / f).stat().st_mtime for f in ("events.jsonl", "run.log", "task.md")
                   if (d / f).exists() ]
        n = 0
        count_info = {}
        ev = d / "events.jsonl"
        if ev.exists():
            try:
                count_info = EI.count_snapshot(ev)
                n = count_info["count"]
            except OSError:
                pass
        else:
            # ★ 老格式（trace.jsonl）：整段折成事件再数，不然列表里写着 0 条，
            #   而点开其实有一整轮 —— 数字和事实对不上比没有数字更坏。
            n = len(_legacy_events(d, 0))
        proc = _active_proc(d)
        # ★ 项目 = 会话干活的那个文件夹（session.json 里一直存着 cwd）。
        #   所以"多个会话归一个项目"不需要新字段、不需要新概念 —— 按它就分组。
        #   读不到就按仓库根算，宁可归错组，也不要让会话在界面上消失。
        _cwd = ""
        _sj = d / "session.json"
        if _sj.exists():
            try:
                _cwd = json.loads(_sj.read_text(encoding="utf-8")).get("cwd") or ""
            except Exception:
                pass
        # ★ 界面上不该出现 `app-20261025-001727` 这种东西。
        #   它是**目录名**，是我们的实现细节；人要看的是"这个会话在干什么"。
        #
        # ★★ 标题的优先级（2026-10-26 加了重命名）：
        #   ① `title.txt` —— 人**手动改过**的名字，最高优先
        #   ② `task.md` 第一行 —— 用户自己写的那句话
        #   ③ "新对话"
        #   ★ 为什么单开一个文件、不塞进 session.json：session.json 是**运行状态的存档**
        #     （msgs、cwd、模式…），run-task 每次续跑都会整体重写它。
        #     人在界面上改的名字不该被一次续跑洗掉 —— 一个文件只放一件事，
        #     这样谁写谁清楚，不需要"记得保留别人的字段"。
        _title = ""
        try:
            _nf = d / "title.txt"
            if _nf.exists():
                _t1 = _nf.read_text(encoding="utf-8", errors="replace").strip()
                _title = (_t1.splitlines() or [""])[0].strip()[:40]
        except Exception:
            _title = ""
        if not _title:
            try:
                _tk = d / "task.md"
                if _tk.exists():
                    _t0 = _tk.read_text(encoding="utf-8", errors="replace").strip()
                    _title = (_t0.splitlines() or [""])[0].strip()[:28]
            except Exception:
                _title = ""
        # ★ 读最后一个 end/ask_human 事件,推断会话状态（needs_human / success / error 等）。
        #   客户端需要这个字段来决定是否弹出 ask_human 对话框。
        #
        # ★★ 只读文件**尾部**（2026-10-26）：原来是 readlines() 全文读 ——
        #   而 /api/sessions 界面每秒调一次、一次 60 个会话，
        #   最大的 events.jsonl 有 700KB。这等于每秒把几 MB 从磁盘搬一遍，
        #   只为看最后一条事件。尾部 8KB 足够放最后几十条。
        _status = ""
        if ev.exists():
            _status = _tail_status(ev)
        elif (d / "trace.jsonl").exists():
            # 老格式：状态在 trace 顶层。不读它，老会话在列表里永远是"未知"。
            try:
                _tj = json.loads((d / "trace.jsonl").read_text(
                    encoding="utf-8", errors="replace"))
                _status = str(_tj.get("status") or "") if isinstance(_tj, dict) else ""
            except Exception:
                _status = ""
        if RT.status(d)["state"] == "unknown":
            _status = "unknown"
        rows.append({"id": d.name, "title": _title or "新对话",
                     "dir": str(d), "events": n,
                     "eventsCountIncomplete": bool(count_info.get("catchingUp") or count_info.get("incompleteLine") or count_info.get("skippedLines")),
                     "cwd": _cwd or str(APP),
                     "mtime": int(max(stamps) if stamps else 0),
                     "running": bool(proc and proc.poll() is None),
                      "runtime": RT.status(d),
                      "effective_policy": RT.read(d).get("policy"),
                     "pending_unknown": (d / PENDING_CLAIMED).exists() and not (d / PENDING_RESULT).exists(),
                     "status": _status,
                     # ★ 老格式（只有 trace.jsonl，没有 session.json）**读得出、续不了**。
                     #   界面要能一眼看到，不然人会点「继续」然后收到一条看不懂的报错。
                     "legacy": (not ev.exists()) and (d / "trace.jsonl").exists(),
                     # ★ 置顶：照 WorkBuddy 左栏那个「置顶 (2)」分组（2026-10-26）。
                     #   用**空文件**当标记，不塞 session.json —— 后者每次续跑整体重写。
                     #   一个文件只放一件事：存在=置顶，不存在=没置顶，没有第三种状态。
                     "pinned": (d / "pinned").exists(),
                     # ★ 一条事件都没有 = 多半没跑起来。让界面能一眼分出来，
                     #   而不是让人对着一个空会话猜。
                     "empty": n == 0,
                     # ★ 档位（2026-10-29）：界面上的三个人该显示谁，取决于这一档
                     #   有没有规划者。它原来只在命令行参数里，界面上无从知道。
                     "crew": ((d / "crew.txt").read_text(encoding="utf-8").strip()
                              if (d / "crew.txt").exists() else ""),
                     # ★ 测试者**跑过没有**（2026-10-29 用户：「显示测试者没跑，实际上跑了」）。
                     #   界面原来靠 `_benniSince` / `_lastBenniMs` 判断，而它们是**运行时状态**：
                     #   重开一个已经完成的会话时，那些事件早在 `seen` 之前、不会被重新处理，
                     #   于是两个值都是 0 ⇒ 界面上写「未跑」。
                     #   有没有跑过是**事实**，应该由后端从落盘的记录里给，不靠前端现算。
                     "benni": (d / "benni-record.json").exists()})
    rows.sort(key=lambda x: -x["mtime"])
    return rows[:limit]


def _policy_file():
    return P.data_root() / "harness" / "policy.json"


def _load_policy() -> str:
    """上次选的档位（没有就空串）。★ 只在没给 `--policy` 时才用。"""
    try:
        d = json.loads(_policy_file().read_text(encoding="utf-8"))
        v = str(d.get("policy") or "")
        return v if v in POLICY_CHOICES else ""
    except Exception:
        return ""


def _save_policy(v: str) -> str:
    """记住这一档（下次启动仍然用它）。写不进去就返回原因，不影响本次生效。"""
    try:
        f = _policy_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"policy": v, "at": time.strftime("%Y-%m-%d %H:%M")},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(f))
        return ""
    except OSError as e:
        return "记不住（%s），本次仍然生效，但重启会回到上一档" % e


def _state() -> dict:
    return {"policy": POLICY}


# 启动前经 `workbench.py` 按实际工作区核对是否已有写入者。
def _start(task: str, min_reads=None, min_chars=None, max_turns=None,
           policy: str = "workspace", cwd: str = "", blank: bool = False,
           ctx=None, crew: str = "l0", *, reserved_sid=None) -> tuple[int, dict]:
    from loop import workbench
    with workbench._lock:
        if reserved_sid is not None:
            if not blank or not re.fullmatch(r"app-schedule-[a-f0-9]{32}", reserved_sid):
                return 400, {"error": "定时任务会话身份无效"}
            return _start_unlocked(task, policy=policy, cwd=cwd, blank=True, reserved_sid=reserved_sid)
        if not blank and policy != "readonly" and crew != "l4":
            with FileLock(workbench.writer_lock_path(cwd or APP)):
                try:
                    workbench.guard_workspace_writer(cwd or APP, sys.modules[__name__])
                except workbench.Problem as e:
                    return e.status, {"error":e.message,"code":e.code}
                return _start_unlocked(task,min_reads,min_chars,max_turns,policy,cwd,blank,ctx,crew)
        return _start_unlocked(task,min_reads,min_chars,max_turns,policy,cwd,blank,ctx,crew)


def _start_unlocked(task: str, min_reads=None, min_chars=None, max_turns=None,
           policy: str = "workspace", cwd: str = "", blank: bool = False,
           ctx=None, crew: str = "l0", *, reserved_sid=None) -> tuple[int, dict]:
    """blank=True ⇒ 只建一个空会话，不启动进程。

    ★ 为什么要这个：用户点「新对话」时，界面上必须**立刻**多出一条会话。
      原来的做法是只清空视图、等他说第一句话才真的建目录 ——
      于是他点了没反应，以为按钮坏了（原话：「我点击新对话并不会新产生一个对话窗口」）。
      WorkBuddy 那种做法就是"点了就有一条新的"，这里照它。
    """
    task = (task or "").strip()
    if not task and not blank:
        return 400, {"error": "任务不能是空的"}
    sid = reserved_sid or "app-" + time.strftime("%Y%m%d-%H%M%S")
    d = RUNS / sid
    # 同一秒连点两次也不撞：撞了就等到下一秒
    _n = 2
    while reserved_sid is None and d.exists():
        sid = "app-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(_n)
        d = RUNS / sid
        _n += 1
    if reserved_sid and (d.is_symlink() or not d.resolve().is_relative_to(RUNS.resolve())):
        return 409, {"error": "原定时会话目录已移动"}
    d.mkdir(parents=True, exist_ok=True)
    task_file = d / "task.md"
    if reserved_sid is None or not task_file.exists():
        task_file.write_text(task + "\n", encoding="utf-8", newline="\n")
    # ★ 把档位**落到会话目录**（2026-10-29 联调时撞到）：
    #   它原来只作为命令行参数传给 run-task，于是界面上根本不知道
    #   这个会话是哪一档 —— 用户选了 L2，界面上却写「规划者未接入」。
    #   参数不进数据，就是"界面上显示的东西"和"实际在跑的东西"对不上。
    crew_file = d / "crew.txt"
    if reserved_sid is None or not crew_file.exists():
        try:
            crew_file.write_text(str(crew or "l0"), encoding="utf-8")
        except Exception:
            pass
    if blank:
        cwd_file = d / "cwd.txt"
        if cwd and (reserved_sid is None or not cwd_file.exists()):
            try:
                cwd_file.write_text(str(Path(cwd).resolve()), encoding="utf-8")
            except OSError:
                pass
        return 200, {"session": sid, "dir": str(d), "blank": True}
    args = core_command("run-task.py", "--task", str(d / "task.md"),
            "--out", str(d / "out.md"), "--session", str(d), "--stream",
            # ★ 轨迹必须落到会话目录里。
            #   没有这一行，run-task 的 trace 就是 None ⇒ 它不写轨迹 ⇒
            #   本我（review.py）拿不到 --trace ⇒ **退回它自己的默认值**，
            #   而那个默认值指向仓库里别人的旧文件 —— 于是本我审的是上一件事，
            #   却给出一份看起来完全正常的批注。实测就是审了 10 月 23 日那一轮。
            "--trace", str(d / "trace.jsonl"),
            # ★ 权限级别由**服务端启动参数**决定，不接受请求传参。
            #   让请求能传 trusted，等于任何前端（包括一个被诱导的页面）
            #   都能把门禁关掉 —— 那这道门就没装。
            "--policy", policy)
    # 判据门槛可以让调用方覆盖。★ 界面不该暴露这三个参数 ——
    # 它们默认意味着"真的读过、真的产出"，放松了就等于把验收变成走过场。
    # 这里开放是给自动化和演练用的。
    if min_reads is not None:
        args += ["--min-reads", str(int(min_reads))]
    if min_chars is not None:
        args += ["--min-chars", str(int(min_chars))]
    if max_turns is not None:
        args += ["--max-turns", str(int(max_turns))]
    # ★ 上下文档位由界面拖动条决定（300K / 600K / 1M，2026-10-28）：
    #   不传它，run-task 会去问接口拿真实窗口 —— 那条路会让拖动条变成摆设。
    if ctx:
        args += ["--ctx", str(int(ctx))]
    # ★ 项目 = 一个文件夹。会话在哪个目录里干活，就属于哪个项目。
    #   校验 is_dir 而不是只拼字符串：路径可控就等于能把它指到任何地方，
    #   而 workspace 门禁是按这个目录划界的 —— 界线划错，门禁就形同虚设。
    if cwd:
        _cd = Path(cwd)
        if not _cd.is_dir():
            return 400, {"error": "这个目录不存在：%s" % cwd}
        args += ["--cwd", str(_cd)]
    # ★ 独立进程，不阻塞这个服务。任务是长活，HTTP 请求不能挂在上面等。
    # ★★ 输出必须落到 run.log，不能丢 DEVNULL。
    #   原来丢掉之后的后果：缺凭据时 provider 在 run-task 里抛异常，进程直接退出，
    #   会话目录里**一个事件都没有**。界面上看到的是"多了个空会话，然后什么都没发生"。
    #   点了没反应是最难查的一种失败 —— 用户不知道是没配好、还是卡住了、还是坏了。
    log_path = d / "run.log"
    from loop.process_log import spawn
    _env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = spawn(args, cwd=str(APP), log=log_path, env=_env)
    PROCS[sid] = p
    RT.register(RUNS / sid, p.pid, policy=policy)
    return 200, {"session": sid, "dir": str(d), "pid": p.pid}


def _save_images(d: Path, images) -> "tuple[list, str]":
    """把前端传来的图片落成**会话目录里的文件**，返回（路径列表, 给任务用的附注）。

    ★ 为什么落盘而不是直接塞进任务文本：agent 读图只有一个入口（read_image 工具），
      它只认**路径**。base64 塞进任务文本，模型看不到图，只会看到一坨字符。
    ★ 为什么放会话目录（runs/<id>/）：删会话就一起删了，不会在别处留垃圾；
      而且"这张图属于这一轮"这件事，在文件位置上一眼可见。
    """
    out, notes = [], []
    for i, it in enumerate(images or [], 1):
        try:
            data = str((it or {}).get("data") or "")
            name = str((it or {}).get("name") or ("img-%d" % i))
            m = re.match(r"data:image/([a-z0-9.+-]+);base64,(.*)$", data, re.S)
            if not m:
                continue
            ext = m.group(1).lower()
            ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
            if ext not in ("png", "jpg", "gif", "webp", "bmp", "svg", "avif", "ico"):
                continue
            raw = base64.b64decode(m.group(2))
            if len(raw) > 8 * 1024 * 1024:
                continue
            f = d / ("img-%d.%s" % (i, ext))
            f.write_bytes(raw)
            out.append(f)
            notes.append("（附图 %d：%s —— 这是我给的图，用 read_image 看它）"
                         % (i, f))
        except Exception:
            continue
    return out, ("\n\n" + "\n".join(notes) if notes else "")


_QUEUED: set = set()
_QUEUE_LOCK = threading.Lock()


def _queue_after(d, proc, fn, limit: float | None = 1800):
    """这一轮还在跑时**排队**，而不是拒绝（2026-10-28）。

    ★ 为什么（用户实测）：点完「允许」应该接着跑，结果什么都没发生。
      查下来是这一轮还在**收尾**（本我复核，实测 20 秒到 3 分钟），
      服务端就回了「还在跑，等它停下再点」—— 而那句话没显示给人看，
      人看到的只是「已允许（只这一次）· 重新发一次同样的动作才会用掉它」。
      再点一次还是同样拒绝。于是一路看着像"批了就不动了"。

    ★ 为什么不阻塞等待：HTTP 请求会被前端超时掐掉，而收尾动辄几分钟。
      所以丢到后台线程里等，接口当场返回「已排队」。
    ★ 普通排队上限 30 分钟：真卡死时不要永远挂着这个线程。
      插话传 `limit=None`：工作进度优先，等当前轮自然做完，不设时间上限。
    """
    # 每个会话只需要一个等待线程；已接收的各条输入分别保存在磁盘队列中。
    global _QUEUED
    with _QUEUE_LOCK:
        if d.name in _QUEUED:
            return False
        _QUEUED.add(d.name)

    def _go():
        try:
            # `None` 就是真的不限时（插话专用）；普通排队仍有自己的兜底上限。
            if proc is not None:
                proc.wait() if limit is None else proc.wait(timeout=limit)
        except Exception:
            pass
        # ★ 先放掉"排队中"再跑：跑起来之后 PROCS 里就有新进程了，
        #   这时的并防交给 PROCS 判断，不需要再占着名额。
        with _QUEUE_LOCK:
            _QUEUED.discard(d.name)
        try:
            fn()
        except Exception as e:
            try:
                with (Path(d) / "run.log").open("a", encoding="utf-8") as _f:
                    _f.write("\n[排队续跑失败] %s: %s\n" % (type(e).__name__, e))
            except OSError:
                pass
    threading.Thread(target=_go, daemon=True, name="queued-run").start()
    return True


PENDING_NAME = OPS.PENDING_HEAD
PENDING_CLAIMED = "pending-after.claimed.json"
PENDING_RESULT = "pending-after.result.json"


def _save_pending(d: Path, kind: str, request: dict, *, append=False) -> bool:
    """先把待办原子落盘，再允许停止当前任务。已有待办时不覆盖人的上一句。"""
    p = d / PENDING_NAME
    rec = {"version": 1, "kind": kind, "state": "waiting",
           "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "request": request}
    # HTTP 是多线程的：检查“有没有待办”和落盘必须在同一小段锁里，
    # 否则两句同时到时可能互相覆盖，后一句把前一句悄悄冲掉。
    with _QUEUE_LOCK, FileLock(d / "pending.lock"):
        if (d / PENDING_CLAIMED).exists() and not (d / PENDING_RESULT).exists():
            return False
        waiting = OPS.waiting_inputs(d)
        if waiting and not append:
            return False
        try:
            if waiting:
                tail = d / OPS.PENDING_TAIL
                tail.mkdir(exist_ok=True)
                sequence = int(waiting[-1].stem) + 1 if waiting[-1].parent == tail else 1
                p = tail / f"{sequence:020d}.json"
            atomic_json(p, rec)
            return True
        except OSError:
            return False


def _claim_pending(d: Path) -> dict | None:
    """原子认领：只有改名成功的一方可以执行，重启不会盲目重放 claimed。"""
    with FileLock(d / "pending.lock"):
        waiting = OPS.waiting_inputs(d)
        claimed, result = d / PENDING_CLAIMED, d / PENDING_RESULT
        if not waiting or (claimed.exists() and not result.exists()):
            return None
        src = waiting[0]
        try:
            rec = json.loads(src.read_text(encoding="utf-8"))
            if claimed.exists():
                # The previous receipt must survive slot reuse, including a restart
                # between writing its result file and committing the operation.
                previous, outcome = _read_json(claimed, {}), _read_json(result, {})
                if not previous or not outcome:
                    return None
                _publish_pending(previous, outcome)
                claimed.unlink()
            result.unlink(missing_ok=True)
            rec["claimed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["dispatcher"] = {"pid": os.getpid(), "identity": RT.identity(os.getpid())}
            atomic_json(src, rec)
            os.replace(src, claimed)
            if len(waiting) > 1:
                os.replace(waiting[1], d / PENDING_NAME)
            return rec
        except (OSError, ValueError):
            return None


# 领取一次已保存的续接请求后启动；结果关联原记录，应由回执核对。
def _publish_pending(rec, result):
    request = rec.get("request") or {}
    if result.get("reconciled"):
        state = "unknown"
    elif (result.get("out") or {}).get("code") == "archived_conversation":
        state = "not_dispatched"
    else:
        state = "completed" if result.get("ok") else "failed"
    response = OPS.followup_receipt(request, result)
    OPS.finish(ROOT, request.get("_request_id"), result.get("code", 409 if state == "unknown" else 500), response, state)


def _run_pending(d: Path) -> None:
    from loop import workbench
    if not OPS.waiting_inputs(d):
        return
    with workbench.launch_admission(d, wait_for_launch=True) as admitted:
        if not admitted:
            _recover_one(d)
            return
        if not OPS.waiting_inputs(d):
            return
        proc = _active_proc(d)
        if proc is None:
            rec = _claim_pending(d)
            if rec is None:
                return
            _dispatch_pending(d, rec)
            if not OPS.waiting_inputs(d):
                return
            proc = _active_proc(d)
        _queue_after(d, proc, lambda: _run_pending(d), limit=None)


def _dispatch_pending(d, rec):
    kind = str(rec.get("kind") or "")
    req = rec.get("request") or {}
    try:
        if rec.get("cancelled"):
            code, out = 409, OPS.ARCHIVED_INPUT
        elif kind == "pause":
            _write_interrupt(d, str(req.get("note") or ""))
            code, out = 200, {"ok": True, "paused": True}
        elif kind in ("interrupt", "say"):
            text = str(req.get("text") or "")
            if (req.get("crew", "l0") != "l0"
                    or (req.get("execution_config") or {}).get("crew", "l0") != "l0"):
                code, out = 409, {"error": "旧档位的待办已退役", "code": "execution_retired"}
            else:
                code, out = _continue(
                    d, text, req.get("min_reads"), req.get("min_chars"),
                    req.get("max_turns"), images=req.get("images"), ctx=req.get("ctx"),
                    policy=req.get("policy"), execution_config=req.get("execution_config"),
                    user_input=not req.get("internal_grant_resume"), steering=kind == "interrupt", from_pending=True,
                    attachment_ids=req.get("attachment_ids"), github_context=req.get("github_context"))
        else:
            code, out = 400, {"error": "不认识的待办类型：%s" % kind}
        result = {"ok": code == 200, "code": code, "out": out,
                  "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                  "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    atomic_json(d / PENDING_RESULT, result)
    _publish_pending(rec, result)


def _acknowledge_pending(d: Path, *, wait_for_launch=True) -> tuple[int, dict]:
    """Release an ambiguous queue slot after explicit human reconciliation.

    Preserve the claimed request and mark acknowledgement separately from outcome.
    This never replays a command or asserts that its side effect succeeded.
    """
    from contextlib import ExitStack
    from loop import workbench

    with ExitStack() as locks:
        try:
            locks.enter_context(FileLock(d / "launch.lock", timeout=10 if wait_for_launch else 0))
        except TimeoutError:
            if wait_for_launch:
                raise
            return workbench.launch_busy()
        locks.enter_context(FileLock(d / "pending.lock"))
        if _active_proc(d) is not None:
            return 409, {"error": "运行仍存活或无法确认，不能归档待办"}
        claimed = d / PENDING_CLAIMED
        if not claimed.exists() or (d / PENDING_RESULT).exists():
            return 409, {"error": "没有需要归档的未知待办"}
        rec = _read_json(claimed, {}) or {}
        owner = rec.get("dispatcher") or {}
        if owner.get("pid"):
            actual = RT.identity(owner["pid"])
            if actual == "unknown" or (actual is not None and actual == owner.get("identity")):
                return 409, {"error": "派发进程仍存活或无法确认，不能归档待办"}
        result = {"ok": False, "reconciled": True, "outcome": "unknown",
                  "note": "用户已核对产出并归档待办；没有自动重跑",
                  "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        # Preserve evidence before the next claim reuses the fixed filenames.
        archive = d / ("pending-archive-" + uuid.uuid4().hex + ".json")
        atomic_json(archive, {"claimed": rec, "acknowledgement": result})
        atomic_json(d / PENDING_RESULT, result)
        OPS.finish(ROOT, (rec.get("request") or {}).get("_request_id"), 409, result, "unknown")
        if OPS.waiting_inputs(d):
            _recover_one(d)
        return 200, {"ok": True, "reconciled": True, "outcome": "unknown"}


# 从持久待续接记录安排后续启动，事件流仍可独立读取。
def _queue_pending(d: Path, proc, kind: str, request: dict) -> bool:
    append = kind in ("say", "interrupt") and not request.get("internal_grant_resume")
    if not _save_pending(d, kind, request, append=append):
        return False
    # Another waiter for this session will drain the same durable FIFO.
    _queue_after(d, proc, lambda: _run_pending(d), limit=None)
    return True


def _recover_one(d: Path) -> bool:
    """服务重启后恢复一个 waiting：按租约等旧进程，而不是拿内存 Popen。"""
    global _QUEUED
    with _QUEUE_LOCK:
        if d.name in _QUEUED:
            return False
        _QUEUED.add(d.name)

    def _go():
        from loop.lease import Lease
        try:
            while _active_proc(d) is not None:
                time.sleep(0.5)
        except Exception:
            pass
        with _QUEUE_LOCK:
            _QUEUED.discard(d.name)
        _run_pending(d)

    threading.Thread(target=_go, daemon=True, name="pending-recover").start()
    return True


def _recover_pending() -> None:
    """只恢复 waiting；claimed 是已认领未确认，自动重放可能重复起任务。"""
    try:
        dirs = [d for d in RUNS.iterdir()
                if d.is_dir() and OPS.waiting_inputs(d)]
    except OSError:
        dirs = []
    # Claimed without a result is ambiguous: dispatch may already have happened.
    for d in RUNS.iterdir() if RUNS.is_dir() else []:
        claimed = d / PENDING_CLAIMED
        if d.is_dir() and claimed.exists():
            rec = _read_json(claimed, {}) or {}
            ident = (rec.get("request") or {}).get("_request_id")
            result = _read_json(d / PENDING_RESULT, {}) or {}
            if not result and rec.get("cancelled"):
                # Archive cancelled this input before it was claimed. A restart in
                # the middle of draining it cannot turn that fact into an unknown launch.
                result = {"ok": False, "code": 409, "out": OPS.ARCHIVED_INPUT}
                atomic_json(d / PENDING_RESULT, result)
            if result:
                _publish_pending(rec, result)
            else:
                OPS.finish(ROOT, ident, 202, {"error": "服务中断时请求已认领，结果未确认；请核对后处理"}, "unknown")

    for d in dirs:
        _recover_one(d)


def _write_interrupt(d: Path, note: str) -> None:
    """把"中断时做到哪了"摆成材料，供重开时的规划者重新收揽。

    这一层**不调模型**：整理语言由重开时的规划者做（用户要的就是"从规划者重新收揽"）。
    这里只负责把事实摆齐：它说过什么、做过哪些动作、人新补了什么要求。
    被中断的正文也留在 said_tail 里 —— 重开后的执行者要把它**整合**进最终答复。
    """
    said, tools = [], []
    try:
        ev = d / "events.jsonl"
        if ev.exists():
            for ln in ev.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("act") == "say":
                    t = str(r.get("text") or "").strip()
                    if t:
                        said.append(t[:700])
                elif r.get("act") == "tool":
                    tools.append("%s(%s)" % (r.get("tool") or "?",
                                             "成" if r.get("ok") else "败"))
    except OSError:
        pass
    obj = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
           "said_tail": said[-6:], "tools_tail": tools[-12:]}
    (d / "interrupt.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                                      encoding="utf-8")


# 只认本服务仍持有且未退出的原子进程。
def _active_proc(d):
    p = PROCS.get(d.name)
    if p and p.poll() is None:
        return p
    return RT.RecoveredProcess(d) if RT.status(d)["active"] else None


# 给实际会话子进程准备运行意图和工具桥身份，再由 `process_log.py` 启动。
def _spawn_conversation(d, args, policy, launch_intent):
    from loop import agent_gateway
    from loop.process_log import spawn

    grant = agent_gateway.environment(d.name)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.update(grant)
    env.update(launch_intent or {})
    try:
        process = spawn(args, cwd=str(APP), log=d / "run.log", env=env)
    except BaseException:
        agent_gateway.retire(d.name, token=grant.get("HARNESS_AGENT_TOKEN"))
        raise
    PROCS[d.name] = process
    agent_gateway.bind(d.name, process)
    run_id = RT.register(d, process.pid, policy=policy)["run_id"]
    return process, run_id


# 普通发送、转向和委派续接都核对原会话与工作区占用后，才走对应启动路径。
def _continue(d: Path, text: str, min_reads=None, min_chars=None,
              max_turns=None, images=None, ctx=None, crew: str = "l0", policy=None, execution_config=None, *,
              user_input=True, steering=False, worker_wake=None, scheduled_start=False, wait_for_launch=True, from_pending=False,
              attachment_ids=None, github_context=None):
    from contextlib import nullcontext
    from loop import execution_policy, workbench
    # The FIFO worker holds the same admission gate across claim, spawn and receipt.
    gate = nullcontext(True) if from_pending else workbench.launch_admission(d, wait_for_launch=wait_for_launch)
    with gate as admitted:
        if not admitted:
            return workbench.launch_busy()
        if not from_pending and OPS.waiting_inputs(d):
            return 409, {"error": "会话已有排队消息，请等待按序派发。", "code": "input_queued"}
        if _active_proc(d):
            return 409, {"error": "当前运行尚未结束或状态待核对"}
        if worker_wake is not None:
            from loop.worker_plugins import claim_wake
            try:
                execution_config=claim_wake(d,worker_wake)
            except workbench.Problem as error:
                return error.status, {"error":error.message,"code":error.code}
        checkpoint = workbench.read(d / 'session.json')
        saved = execution_policy.checkpoint_config(d, checkpoint)
        native_message = bool(user_input and text.strip() and crew == 'l0' and execution_config
                              and execution_config.get('purpose') == 'execute')
        config = execution_config or {}
        managed_delegation = crew == 'l4' and config.get('crew') == crew and config.get('delegationId')
        if (crew != 'l0' and not managed_delegation
                or config.get('crew', crew) != crew
                or execution_policy.retired_checkpoint(saved) and not native_message):
            return 409, {"error": "旧运行已退役，请发送新的 L0 任务", "code": "execution_retired"}
        if saved.get('crew') == 'l4' and not (managed_delegation or native_message):
            return 409, {"error": "请在会话的持续委托面板继续", "code": "delegation_required"}
        fresh_native = native_message and (scheduled_start or execution_policy.legacy_checkpoint(saved))
        from contextlib import ExitStack
        from loop.code_service import reconcile_interrupted_writes, selected_write_paths
        targets = [workbench.recorded_directory(d,sys.modules[__name__])] + selected_write_paths(d.name)
        with ExitStack() as ownership:
            # Admission already serializes the guard and spawn under the workbench
            # lock. A busy checkout must not park every other conversation there.
            try:
                for target in sorted({str(workbench.writer_lock_path(path)) for path in targets}):
                    ownership.enter_context(FileLock(target, timeout=0))
            except TimeoutError:
                return 409, {"error": "检出目录正在写入或准备副本，请稍后继续", "code": "writer_busy"}
            try:
                policy = workbench.guard_continuation(d, policy or POLICY, sys.modules[__name__])
                recovered = reconcile_interrupted_writes(d.name, targets)
                from loop import projects, permissions
                original = text if user_input else None
                display = None
                if crew == 'l4':
                    from loop import delegations
                    display = delegations.controller_display()
                    if display is not None:
                        original = delegations.controller_answer()
                launch_intent = permissions.launch_intent(d, original)
                if github_context:
                    launch_intent["HARNESS_GITHUB_WORKROOM_PAGE"] = json.dumps(github_context, ensure_ascii=False)
                if scheduled_start:
                    launch_intent["HARNESS_SCHEDULE_NEW_TASK"] = "1"
                kind = None
                if steering and not fresh_native:
                    _write_interrupt(d, text)
                if display is None and user_input:
                    from loop.task_input import classify, split_answer
                    kind = 'new' if fresh_native else classify(text, checkpoint, steering=steering)
                    launch_intent["HARNESS_TASK_INPUT_KIND"] = kind
                if worker_wake is not None:
                    launch_intent['HARNESS_TASK_INPUT_KIND']='resume'
                answer = split_answer(text)[0] if kind == 'answer_amend' else original
                text = projects.prepare_turn(d, text, user_input=user_input and bool(answer)
                                             and kind in {None, 'answer', 'answer_amend'},
                                             answer_text=answer, display_text=display, recovered_writes=recovered,
                                             attachment_ids=attachment_ids)
            except workbench.Problem as e:
                return e.status, {"error":e.message,"code":e.code}
            return _continue_unlocked(d, text, min_reads, min_chars, max_turns, images, ctx, crew, policy, execution_config,
                                      launch_intent=launch_intent, fresh_native=fresh_native)


def _continue_unlocked(d: Path, text: str, min_reads=None, min_chars=None,
              max_turns=None, images=None, ctx=None, crew: str = "l0", policy=None, execution_config=None, *,
              launch_intent=None, fresh_native=False) -> tuple[int, dict]:
    """在已有会话里接着说一句。历史原样接上，末尾追加这条。

    ★ 为什么不复用 _start：_start 会新建目录、写新的 task.md、重定产物路径 ——
      那是"开一轮新活"。而这里只是"接着聊"，契约、判据、产物都不该变。
      两者混成一个接口，迟早有天要区分，那时候已经分不开了。
    """
    # ★ 空会话（刚点「新对话」建出来的）还没有存档 —— 这时候 --resume 会被拒，
    #   这句话就是它的第一轮任务。判据只有一个：有没有 session.json。
    # ★ 判据是"有没有可续的消息"，不是"有没有 session.json"。
    #   实测踩到：开工前采访（needs_human）退出时 session.json 已经写出来了，
    #   但 msgs 是空的 —— 于是用户回答那句话走了 --resume，被"没有存档的消息"拒掉，
    #   界面表现为"说完没反应"。
    from loop.session import ensure_event_capacity
    try:
        ensure_event_capacity(d)
    except RuntimeError as error:
        return 409, {"error": str(error), "code": "history_capacity"}
    _has_msgs = False
    try:
        if (d / "session.json").exists():
            _st0 = json.loads((d / "session.json").read_text(encoding="utf-8"))
            _has_msgs = bool(_st0.get("msgs"))
    except Exception:
        _has_msgs = False
    if not (_has_msgs or fresh_native):
        # ★★★ 先分清两种"还没有消息"的情况（2026-10-27 用户报「我的输出它直接读不到了，
        #   怎么输入都为空」）：
        #   ① **点「新对话」建出来的空会话**：task.md 里只有一个换行。
        #      用户这句话**就是这一轮的任务书**。
        #   ② **采访之后的回答**：task.md 里已经有原任务书，不能覆盖
        #      （覆盖它等于把原任务冲掉 —— 用户实测过"回答覆盖任务正文"）。
        _t0 = ""
        try:
            _t0 = (d / "task.md").read_text(encoding="utf-8").strip()
        except OSError:
            _t0 = ""
        if not _t0:
            # ① 这句话就是任务书本身。**必须写进 task.md**：
            #   下面启动 run-task 时只传 `--task task.md`，如果这里还把它塞进
            #   interview.json 的 answer，那一轮读到的任务就是空的 ——
            #   契约会回头问「这次要做什么」，而人**刚刚才说过**。
            #   界面上看就是"输入被吞了"，其实是送错了地方。
            try:
                _files, _note = _save_images(d, images)
                (d / "task.md").write_text(text + (_note or "") + "\n", encoding="utf-8", newline="\n")
            except OSError as e:
                return 500, {"error": "任务书写不进去：%s" % e}
        else:
            # ② 采访之后的第一句回答。**不能覆盖 task.md** ——
            #   task.md 是人写的原始任务书，覆盖它等于把原任务冲掉
            #   （用户实测：interview.json 里 questions 还在、answer 是空串，
            #    而 task.md 变成了那三行回答，"回答覆盖任务正文"说的就是这个）。
            #   ⇒ 回答写进 interview.json 的 answer 字段；run-task 开工前会读它
            #     （见 _interview_recall），拼在任务书后面一起给模型。
            try:
                _iw = d / "interview.json"
                _ob = json.loads(_iw.read_text(encoding="utf-8")) if _iw.exists() else {}
                if not isinstance(_ob, dict):
                    _ob = {}
                _ob["answer"] = text
                _files, _note = _save_images(d, images)
                if _note:
                    _ob["answer"] = text + _note
                _iw.write_text(json.dumps(_ob, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            except Exception:
                # 写不进备忘也不能丢这句话：退回到"追加到任务书末尾"，**不覆盖**
                try:
                    with (d / "task.md").open("a", encoding="utf-8") as f:
                        f.write("\n\n补充：%s\n" % text)
                except OSError:
                    pass
        args = core_command("run-task.py", "--task", str(d / "task.md"),
                "--out", str(d / "out.md"), "--session", str(d), "--policy", (policy or POLICY),
                "--trace", str(d / "trace.jsonl"))
        if execution_config is not None:
            args += ['--execution-config', json.dumps(execution_config)]
        # Blank sessions have no session.json yet; honor the registered cwd on
        # their first turn, exactly as on subsequent --resume turns.
        from loop import workbench
        args += ["--cwd", workbench.recorded_directory(d, sys.modules[__name__])]
        if min_reads is not None:
            args += ["--min-reads", str(int(min_reads))]
        if min_chars is not None:
            args += ["--min-chars", str(int(min_chars))]
        if max_turns is not None:
            args += ["--max-turns", str(int(max_turns))]
        # ★ 上下文档位由界面拖动条决定（300K / 600K / 1M，2026-10-28）：
        #   不传它，run-task 会去问接口拿真实窗口 —— 那条路会让拖动条变成摆设。
        if ctx:
            args += ["--ctx", str(int(ctx))]
        process, run_id = _spawn_conversation(d, args, policy or POLICY, launch_intent)
        return 200, {"session": d.name, "pid": process.pid, "fresh": True, "runId": run_id}

    # ★ 图片先落盘，再把路径附进这句话 —— 只在**这一次**的任务里带，不写进 task.md。
    _files, _note = _save_images(d, images)
    if _note:
        text = text + _note

    args = core_command("run-task.py", "--resume", str(d), "--say", text) + [
            "--out", str(d / "out.md"), "--policy", (policy or POLICY),
            # ★ 接着说也要写轨迹：不写的话这一轮的 trace 是空的，
            #   本我又会退回它自己的默认文件（别人的旧轨迹）。
            "--trace", str(d / "trace.jsonl")]
    if execution_config is not None:
        args += ['--execution-config', json.dumps(execution_config)]
    # 轮数上限和判据门槛跟新会话一样可控 —— 否则"接着说一句"又会跑满 20 轮
    if min_reads is not None:
        args += ["--min-reads", str(int(min_reads))]
    if min_chars is not None:
        args += ["--min-chars", str(int(min_chars))]
    if max_turns is not None:
        args += ["--max-turns", str(int(max_turns))]
    # ★ 上下文档位由界面拖动条决定（300K / 600K / 1M，2026-10-28）：
    #   不传它，run-task 会去问接口拿真实窗口 —— 那条路会让拖动条变成摆设。
    if ctx:
        args += ["--ctx", str(int(ctx))]
    process, run_id = _spawn_conversation(d, args, policy or POLICY, launch_intent)
    return 200, {"session": d.name, "pid": process.pid, "continued": True, "runId": run_id}


# ★ 凭据的**写入口只有一个**：`loop/models.py`（写 `models.json`）。
#   原来这里还有一套「界面直接改 credentials.env」的读写实现（`_creds_write` 等），
#   而档案制的优先级高于那个文件 —— 于是"在旧面板里改了一处，跑起来没变化"。
#   那正是这个仓库反复踩的那种坑：两条路都能写，只有一条能生效。
#   ⇒ 2026-10-26 删掉。`credentials.env` 现在**只读不写**（provider 还认它，作最后兜底），
#     要改模型请走界面「档案」或 `python -c` 直接编辑那个文件（然后**不要**再选着档案）。
class H(BaseHTTPRequestHandler):
    server_version = "harness/0.1"

    def _cors(self):
        # ★ CORS 这里**不是**安全边界，别再指望它（2026-10-26 加鉴权时想清楚的）：
        #   界面是 `file://` 打开的，它的 Origin 是 `null`；而任意一个沙箱 iframe 也是 `null` ——
        #   按来源白名单放行等于把 `null` 放行，谁都进得来。
        #   真正的边界是下面那把钥匙：**没有 token 的请求一律 401**，
        #   而 token 在数据目录里，网页读不到。
        #   这里保留 `*` 只是为了让 file:// 的界面读得到响应体。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Harness-Token, Authorization, X-Request-ID, X-Harness-Client-Version")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _key(self) -> str:
        """请求里带的钥匙。支持两种写法（curl 用前者更省事）。"""
        t = (self.headers.get("X-Harness-Token") or "").strip()
        if t:
            return t
        a = (self.headers.get("Authorization") or "").strip()
        if a.lower().startswith("bearer "):
            return a[7:].strip()
        return ""

    # 受控工具桥和桌面用户入口分别验证各自的原身份。
    def _authed(self) -> bool:
        if urllib.parse.urlparse(self.path).path.rstrip("/") == "/api/agent-tools":
            from loop import agent_gateway
            return self.command == "POST" and bool(agent_gateway.authorize(self.headers.get("X-Harness-Agent-Token"), sys.modules[__name__]))
        import hmac
        got = self._key()
        want = TOKEN
        if not want or not got:
            return False
        # ★ 常数时间比较：`==` 会按第一个不同的字节提前返回，
        #   本机场景下这不重要，但这条规矩写下来就不用每次重新想一遍。
        return hmac.compare_digest(got, want)

    def _deny(self):
        """401。故意不回"钥匙错了"还是"没带钥匙"—— 那是给猜的人送信息。"""
        return self._json({
            "error": "这个接口要钥匙。界面从应用里打开就有；"
                     "命令行用 `python bin/server.py --token` 看这把钥匙，"
                     "然后带 header `X-Harness-Token`。",
            "auth": True}, 401)

    # 返回 HTTP 响应前先更新已登记的人工动作和操作回执。
    def _json(self, obj, code: int = 200):
        human_id = getattr(self,"_human_action_id",None)
        if human_id:
            from loop import human_actions
            try:
                human_actions.finish(human_id,code,obj)
                obj=dict(obj,humanActionId=human_id)
            except Exception:
                obj=dict(state="unknown",humanActionId=human_id,error="操作回执留底未确认，请核对实际产出；系统不会自动重放")
                code=202
            self._human_action_id=None
        request_id = getattr(self, "_operation_id", None)
        if request_id:
            obj = dict(obj, request_id=request_id)
            OPS.finish(ROOT, request_id, code, obj, "unknown" if code == 202 and obj.get("state") == "unknown" else None)
            self._operation_id = None
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # ★ 客户端先走了（界面关掉、探针跑完），这不是错误。
            #   不接住的话每关一次窗口就吐一整段 traceback ——
            #   而"日志里天天有红字"会让真正的故障淹在里面（这一条今天就在噪音里泡过）。
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # 读取会话和工作间事实；API 路径确定应交给的状态或历史模块。
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/")

        # ★ 白名单只有**一个**：`GET /api/health`（不带 full）—— 外壳靠它判断"服务起没起"，
        #   而那一刻它还没读到钥匙。其余一律先要钥匙，**默认拒绝**。
        #   写成"先放行唯一一个、其余默认拒"而不是"列一堆要保护的接口"：
        #   后者只要有人新加一个接口忘了登记，它就是敞开的，而且没人会发现。
        if not (path == "/api/health" and "full" not in q):
            if not self._authed():
                return self._deny()

        if path.startswith("/api/v2/"):
            from loop import workbench
            code, result = workbench.handle("GET", path, q, {}, sys.modules[__name__])
            return self._json(result, code)

        if path.startswith("/api/operations/"):
            ident = path.rsplit("/", 1)[-1]
            operation = OPS.get(ROOT, ident)
            return self._json({"operation": operation}, 200 if operation else 404)

        # ── 任务委托（job.py 那一套）──
        if path == "/api/jobs":
            return self._json(_jobs_index())
        if path == "/api/gflow/config":
            return self._json(_gflow_config())
        archive = re.match(r"/api/jobs/([A-Za-z0-9._-]+)(?:/|$)", path)
        if archive and (archive[1] in {".", ".."} or _managed_job(JOBS / archive[1])):
            return self._json({"code": "not_found", "error": "没有这个旧任务"}, 404)
        _mj = re.fullmatch(r"/api/jobs/([A-Za-z0-9._-]+)", path)
        if _mj:
            return self._json(_job_overview(_mj.group(1)))
        _mje = re.fullmatch(r"/api/jobs/([A-Za-z0-9._-]+)/round/(\d+)/events", path)
        if _mje:
            try:
                after = int((q.get("after") or ["0"])[0])
                off = int((q.get("offset") or ["0"])[0])
                lim = int((q.get("limit") or ["0"])[0])
            except ValueError:
                return self._json({"error": "after / offset / limit 必须是整数"}, 400)
            return self._json(_job_round_events(_mje.group(1), int(_mje.group(2)),
                                                after, offset=off, limit=lim))
        _mjr = re.fullmatch(r"/api/jobs/([A-Za-z0-9._-]+)/round/(\d+)", path)
        if _mjr:
            return self._json(_job_round(_mjr.group(1), int(_mjr.group(2))))
        _mjc = re.fullmatch(r"/api/jobs/([A-Za-z0-9._-]+)/contract", path)
        if _mjc:
            return self._json(_job_contract(_mjc.group(1)))

        if path == "/api/models":
            # ★ 模型档案（2026-10-26）：界面上那两个下拉的数据源。
            #   回的是「有哪些档案 + 现在自我/本我各用哪一份 + 是不是被环境变量盖了」。
            #   key 只给掩码 —— 同一个理由：这个响应会被截图。
            from loop import models as _MD
            return self._json(_MD.public())

        if path == "/api/context":
            # ★ 上下文构成（2026-10-27）：**只读会话里那份快照**，不现算。
            #   现算等于第二套口径 —— 注入了没有、工具死了几个、MCP 连上没有，
            #   都要重新判断一遍，而只要有一处不同，面板显示的就不是真正发出去的那份。
            #   快照由 `loop` 在调模型之前写（`runs/<会话>/context.json`）。
            from loop import context as _CTX
            _sid = (q.get("session") or [""])[0]
            _base = ROOT / "runs"
            if _sid:
                # 会话 id 只当目录名用：不许带路径分隔符（否则顺着 `../` 读别处）
                if "/" in _sid or "\\" in _sid or ".." in _sid:
                    return self._json({"ok": False, "why": "会话 id 不合法"}, 400)
                # ★ 两种落法都要试：`runs/<id>` 和 `runs/sessions/<id>`。
                #   只认第一种的话，命令行跑的会话在界面上永远"没有上下文" ——
                #   而它明明是有的，只是我们找错了地方。
                _d = _base / _sid
                if not _d.is_dir():
                    _alt = _base / "sessions" / _sid
                    if _alt.is_dir():
                        _d = _alt
            else:
                # 没给就取最近一次真跑过的（按 context.json 的修改时间）。
                # ★ 会话有两种落法：`runs/<id>`（界面起的）和 `runs/sessions/<id>`
                #   （命令行走的）。只扫一层就永远看不到另一种 —— 这个错今天已经在
                #   备份清单和这里各出现一次，所以按"两层都扫"写，不按名字猜。
                _cands = []
                for _b in (_base, _base / "sessions"):
                    try:
                        _items = list(_b.iterdir())
                    except OSError:
                        continue
                    for _x in _items:
                        if _x.is_dir() and not _x.name.startswith("_") \
                                and (_x / "context.json").exists():
                            _cands.append((_x.stat().st_mtime, _x))
                _d = sorted(_cands)[-1][1] if _cands else None
            if _d is None:
                return self._json({"ok": False, "why": "还没有会话跑过，暂时没有上下文可看"})
            _out = _CTX.read(_d)
            _out["session"] = _d.name
            return self._json(_out)

        if path == "/api/skills":
            # 技能清单（名字 + 一句用途 + 从哪来的）。正文不在这里 ——
            # 列表要给界面用，正文是模型按需 load_skill 取的。
            from loop import skills as _SK
            items = _SK.scan(ROOT)
            return self._json({"ok": True, "skills": items,
                               "roots": [{"level": lv, "dir": str(p)}
                                         for lv, p in _SK.roots(ROOT)]})

        if path.startswith("/api/skills/") and path != "/api/skills":
            # 看某个技能的正文（面板里点「看正文」用）。
            # ★ 只回这一个技能的，不顺手把全部正文都塞回来 —— 列表要轻。
            from loop import skills as _SK
            nm = urllib.parse.unquote(path[len("/api/skills/"):].strip("/"))
            out = _SK.load(nm, ROOT, model=False)
            return self._json(out, 200 if out.get("ok") else 404)

        if path.startswith("/api/sessions/") and "/diff/" in path:
            # 读一次改动的 diff 全文（界面上点开那一行时拉取）。
            # ★ 全文不进事件流：一条 400 行的 diff 足以让 events.jsonl 涨几 MB，
            #   而人只在点开时才需要它。
            sid, _, fn = path[len("/api/sessions/"):].partition("/diff/")
            sid = urllib.parse.unquote(sid.strip("/"))
            fn = urllib.parse.unquote(fn.strip("/"))
            # ★ 只认我们自己写出来的文件名格式 —— 不校验的话
            #   `../../xxx` 就能顺着这个接口读到仓库外的东西。
            if not re.fullmatch(r"\d{4,}\.diff", fn or ""):
                return self._json({"error": "文件名不对：%s" % fn}, 400)
            d = ROOT / "runs" / sid / "diff" / fn
            if not d.is_file():
                return self._json({"error": "没有这份 diff：%s" % fn}, 404)
            return self._json({"ok": True, "text": d.read_text(
                encoding="utf-8", errors="replace")})

        if path == "/api/commands":
            # 斜杠命令清单（名字 + 一句说明 + 参数提示）。
            # ★ 正文不在这里：列表要轻，正文只在真要用时取。
            from loop import commands as _CMD
            return self._json({"ok": True, "commands": _CMD.scan(ROOT),
                               "roots": [{"level": lv, "dir": str(p)}
                                         for lv, p in _CMD.roots(ROOT)]})

        if path.startswith("/api/commands/") and path != "/api/commands":
            from loop import commands as _CMD
            nm = urllib.parse.unquote(path[len("/api/commands/"):].strip("/"))
            out = _CMD.load(nm, ROOT)
            return self._json(out, 200 if out.get("ok") else 404)

        if path == "/api/mcp":
            # 外部工具（MCP）的配置概览。★ 这里**只读配置，不连接** ——
            #   面板一打开就去连所有 server 的话，一个卡住的 server 会让整页转圈；
            #   而"我配了哪些"这件事读文件就够。要真连，走下面的 /probe。
            from loop import mcp as _MCP
            servers = _MCP.load_config()
            rows = []
            for nm, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                rows.append({
                    "name": nm,
                    "transport": "http" if cfg.get("url") else "stdio",
                    "target": str(cfg.get("url") or cfg.get("command") or ""),
                    "disabled": bool(cfg.get("disabled")),
                    # ★ 把"要不要人点头"的**结论**直接给界面算出来，
                    #   而不是让前端再实现一遍那套三层优先 ——
                    #   两处各算一次，迟早一处漏掉（比如只认 allow 不认 approval_mode）。
                    "deny": list(cfg.get("deny") or []),
                    "allow": list(cfg.get("allow") or []),
                    "approval": str(cfg.get("default_tools_approval_mode") or "prompt"),
                    "budget": int(cfg.get("output_token_limit") or 8000),
                })
            return self._json({"ok": True, "servers": rows,
                               "config": str(_MCP._cfg_path())})

        if path.startswith("/api/mcp/") and path.endswith("/probe"):
            # 真连一次，看它到底给了哪些工具、哪些会被 deny 掉。
            # ★ 这一步慢（要起进程或发请求），所以按需触发，不跟着列表一起做。
            from loop import mcp as _MCP
            nm = urllib.parse.unquote(
                path[len("/api/mcp/"):-len("/probe")].strip("/"))
            cfg = _MCP.load_config().get(nm)
            if not isinstance(cfg, dict):
                return self._json({"error": "没有这个服务器：%s" % nm}, 404)
            client = (_MCP.HttpClient(nm, cfg, ROOT) if cfg.get("url")
                      else _MCP.StdioClient(nm, cfg, ROOT))
            if not client.start():
                return self._json({"ok": False, "error": client.err or "连不上"}, 200)
            try:
                tools = client.list_tools()
            finally:
                client.close()
            out = []
            for t in tools:
                tn = str(t.get("name") or "").strip()
                if not tn:
                    continue
                denied = _MCP._is_denied(tn, cfg)
                out.append({"name": tn,
                            "desc": str(t.get("description") or "")[:160],
                            "denied": denied,
                            "need_approval": False if denied else _MCP._need_approval(tn, cfg)})
            return self._json({"ok": True, "tools": out})

        if path == "/api/health":
            # ★ 不带 full 的 health **只回"我在跑"**（2026-10-26 加鉴权时收窄的）：
            #   它是唯一免钥匙的接口，所以不能顺带把路径、数据根、目录结构一起吐出来 ——
            #   那等于用"要不要鉴权"换来一份侦察情报。
            if "full" not in q:
                return self._json({"ok": True, "time": int(time.time()), "auth": True})
            # ★ 带 full 才给两个根：打包后"写不进数据目录"是第一种故障，
            #   而它表现成"保存失败""会话列表空"，从界面完全看不出是路径问题。
            return self._json({"ok": True, "time": int(time.time()),
                               "paths": P.describe(), "instance": INSTANCE_ID,
                               "protocol": PROTOCOL.PROTOCOL_VERSION,
                               "agentFirstClient":2,
                               "dataFormat": __import__("loop.legacy_migration",fromlist=["format_state"]).format_state(),
                               "features": PROTOCOL.FEATURES,
                               "isolation": {"required": bool(ISO._cfg().get("require_container")),
                                             "scope": "per-command; Python/MCP may be ineligible"}})
        if path == "/api/state":
            return self._json(_state())
        if path == "/api/probe":
            # 判据体检：造一个"什么都没做"的世界，看判据还绿不绿。
            # 同步跑（每条判据 4 次、单次上限 45 秒），整体封顶 240 秒 ——
            # 超过就报"太慢"，不能把界面卡死。
            _sid = (q.get("id") or [""])[0]
            _args = core_command("probe_judge.py", "--json")
            if _sid:
                _args += ["--dir", str(RUNS / _sid)]
            try:
                _pr = subprocess.run(_args, cwd=str(APP), capture_output=True,
                                     timeout=240,
                                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                _txt = (_pr.stdout or b"").decode("utf-8", "replace").strip()
                _obj = json.loads(_txt) if _txt.startswith("{") else {
                    "error": "体检没输出", "raw": _txt[:300],
                    "err": (_pr.stderr or b"").decode("utf-8", "replace")[:300]}
            except subprocess.TimeoutExpired:
                _obj = {"error": "体检超过 240 秒，这条判据太慢了"}
            except Exception as e:
                _obj = {"error": "%s: %s" % (type(e).__name__, e)}
            return self._json(_obj)

        if path == "/api/memory":
            # ★ 三层记忆的状态（2026-10-26）。界面要能一眼看出：
            #   哪层有东西、哪层是空的、哪层该整理了 —— 三层混在一起看，
            #   人分不清"没写"和"写了但没生效"。
            from loop import agents as _AGm
            from loop import facts as _Fm
            from loop import habits as _HBm
            from loop import memperms as _MPm
            rows = []
            try:
                # ★ 主这一半也在列表里（2026-10-26）：它有自己的事实目录
                #   （`agent-memory/main/`），不列出来的话"agent 记下的东西
                #   在哪"这个问题在界面上没有答案。
                _r0 = _Fm.read("project", "main", ROOT)
                rows.append({"kind": "main", "scope": "project",
                             "who": "主 agent（人这一半）",
                             "path": _r0["path"], "exists": _r0["exists"],
                             "chars": len(_r0["text"]), "files": _r0["listed"],
                             "stale_days": _r0["stale_days"], "note": _r0["note"]})
                for a in _AGm.scan(ROOT):
                    if not a.get("memory"):
                        continue
                    r = _Fm.read(a["memory"], a["name"], ROOT)
                    rows.append({"kind": a["name"], "scope": a["memory"],
                                 "who": a.get("desc") or "",
                                 "path": r["path"], "exists": r["exists"],
                                 "chars": len(r["text"]), "files": r["listed"],
                                 "stale_days": r["stale_days"], "note": r["note"]})
            except Exception as e:
                rows = [{"error": "%s: %s" % (type(e).__name__, e)}]
            # ★ 权限表 + 留痕（2026-10-26）：
            #   ① 每个区现在是哪一档（以及主 agent 的档位）；
            #   ② `edit` 档改过什么 —— 没有这张表，那一档就是**看不见的权力**。
            _tiers = {"main": _MPm.main_tiers(), "types": []}
            try:
                for a in _AGm.scan(ROOT):
                    _tiers["types"].append({"name": a["name"],
                                            "mem": a.get("mem") or {},
                                            "text": _MPm.describe(a.get("mem") or {})})
            except Exception:
                pass
            _aud = []
            try:
                _aud = _MPm.all_audit(ROOT, 30)
            except Exception:
                pass
            return self._json({"habits": _HBm.outline(), "facts": rows,
                               "tiers": _tiers, "audit": _aud,
                               "regions": {k: list(v) for k, v in _MPm.GRANTS.items()}})
        if path == "/api/grants":
            # ★ 一次性许可（2026-10-26）：界面上那个「允许这一次」按钮的数据源。
            #   没有它，人只能去改规则文件 —— 而"改规则文件"是永久许可，
            #   代价比"就这一次"大得多，于是人会干脆把闸关掉。
            from loop import grants as _Gg
            from loop import sandbox_rules as _Sg
            from loop import ruleedit as _REg
            d = _Gg.listing()
            d["rules"] = _Sg.explain()
            # ★「以后这类都别问」的例外清单（2026-10-26）：和一次性许可放在一起，
            #   因为它们回答的是同一个问题 ——「现在有哪些不问的地方」。
            #   分开两页的话，人只记得看其中一页，而"看不见的权力"就等于没有。
            try:
                d["exceptions"] = _REg.listing()
            except Exception as _eE:
                d["exceptions"] = {"count": 0, "items": [], "error": str(_eE)}
            # ★ 底线命中次数（2026-10-26）：它和"现在有哪些不问的地方"是同一类信息 ——
            #   一个说"哪里放开了"，一个说"哪里一直在被挡"。两者都要能看见，
            #   否则人会只知道闸存在，不知道它到底在工作还是在误伤。
            try:
                from loop import baseline as _BLg
                d["baseline_hits"] = _BLg.stats(7)
            except Exception as _eB:
                d["baseline_hits"] = {"error": str(_eB)}
            return self._json(d)

        if path == "/api/facts":
            # ★ 事实区的**每一条**（2026-10-26 收口）。之前只给"这个类型有多少字"，
            #   于是"删掉一条过期的"只能去翻文件 —— 而人不会翻，于是没人清。
            from loop import facts as _Ff
            q = urllib.parse.parse_qs(u.query)
            sc = (q.get("scope") or ["project"])[0]
            kd = (q.get("kind") or ["main"])[0]
            return self._json(_Ff.entries(sc, kd, ROOT))

        if path == "/api/habits":
            # ★ 习惯区（跨项目）—— 和记忆树分开展示：它们的作用域不同，
            #   混成一个列表会让人以为"都是本项目的"。
            from loop import habits as _HAB
            return self._json(_HAB.outline())
        if path == "/api/config":
            # ★ 配置的实际生效值 + **写错的地方**（2026-10-26）。
            #   把这些只留在日志里，等于"改错了也算改过了" ——
            #   那和"配置读了没人用"是同一种毛病：看起来配了，其实没生效。
            from loop import settings as _SET
            c = _SET.load()
            return self._json({"agents": c["agents"], "sandbox": c["sandbox"],
                               "warns": c["warns"], "path": str(_SET.CONFIG)})
        if path == "/api/sessions":
            try:
                lim = max(1, min(int((q.get("limit") or ["50"])[0]), 200))
            except ValueError:
                lim = 50
            return self._json({"sessions": _sessions(lim)})

        if path.startswith("/api/sessions/"):
            rest = path[len("/api/sessions/"):].split("/")
            d = _resolve_sid(rest[0])
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            if len(rest) == 2 and rest[1] == "runtime":
                runtime = RT.status(d)
                return self._json({"id": d.name, "running": _active_proc(d) is not None,
                    "runtime": runtime, "status": "unknown" if runtime['state']=='unknown' else _tail_status(d/'events.jsonl'),
                    "pending_unknown": (d/PENDING_CLAIMED).exists() and not (d/PENDING_RESULT).exists()})
            from loop import workbench
            if workbench.registry()["threads"].get(d.resolve().name, {}).get("projectless"):
                return self._json({"error": "此会话请从工作台读取记录", "code": "use_workbench"}, 404)
            if len(rest) == 2 and rest[1] == "events":
                try:
                    after = int((q.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                try:
                    limit=max(0,min(1000,int((q.get("limit") or ["0"])[0])))
                except ValueError:
                    return self._json({"error":"Invalid event limit"},400)
                rows=_events(d,after,limit+1 if limit else 0)
                more=bool(limit and len(rows)>limit)
                if more: rows=rows[:limit]
                return self._json({"events":rows,"has_more":more,
                                   "next_after":max((r.get("seq",0) for r in rows),default=after)})
            if len(rest) == 2 and rest[1] == "summary":
                p = d / "summary.json"
                if not p.exists():
                    return self._json({"error": "这一轮还没写索引"}, 404)
                return self._json(json.loads(p.read_text(encoding="utf-8")))
            if len(rest) == 2 and rest[1] == "live":
                # 思考草稿 + 正文流**一次给**（2026-10-27 前端优化）：
                #   原来前端每 120ms 发两个请求，跑起来就是 16 次/秒；
                #   合一个接口，一次拿两样。
                # ★ 而且**不截尾**（用户：不要在读取和输出上做限制）——
                #   原来 stream 只给末尾 4000 字，长回复的开头会在界面上直接消失。
                def _rd(name):
                    p = d / name
                    return (p.read_text(encoding="utf-8", errors="replace")
                            if p.exists() else "")
                return self._json({"think": _rd("think.txt"),
                                   "stream": _rd("stream.txt")})
            if len(rest) == 2 and rest[1] == "stream":
                # 正在生成的正文（流式打字用）。和 think 同理：
                # 事件要整轮结束才写，靠这个文件才能边生成边出字。
                p = d / "stream.txt"
                if not p.exists():
                    return self._json({"text": ""})
                return self._json(
                    {"text": p.read_text(encoding="utf-8", errors="replace")})
            if len(rest) == 2 and rest[1] == "think":
                # ★ 正在进行的思考（实时草稿）。llm 事件要整轮结束才写，
                #   这个文件是"边想边落盘"，界面靠它让人看见过程。
                p = d / "think.txt"
                if not p.exists():
                    return self._json({"text": ""})
                txt = p.read_text(encoding="utf-8", errors="replace")
                return self._json({"text": txt})
            # ── 子 agent 的过程（2026-10-26）──
            #    /api/sessions/<id>/sub/<n>          元信息 + 结论全文
            #    /api/sessions/<id>/sub/<n>/events   它自己的过程流
            #   ★ 为什么单独开一个接口、而不是把子事件塞进父的 events.jsonl：
            #     父流里放的是"结论"和"派了谁"，子流里是"它翻了哪些文件"。
            #     混在一起，父流会被三倍的过程噪音撑爆，而人真正要看的父流只有几行。
            if len(rest) >= 3 and rest[1] == "sub":
                no = rest[2]
                if not re.fullmatch(r"\d{1,4}", no):
                    return self._json({"error": "子任务号不对"}, 400)
                sd = d / "sub" / no
                if not sd.is_dir():
                    return self._json({"error": "没有这个子任务"}, 404)
                if len(rest) == 3:
                    meta = {}
                    mp = sd / "meta.json"
                    if mp.exists():
                        try:
                            meta = json.loads(mp.read_text(encoding="utf-8"))
                        except Exception:
                            meta = {}
                    res = ""
                    rp = sd / "result.md"
                    if rp.exists():
                        res = rp.read_text(encoding="utf-8", errors="replace")
                    return self._json({"sub": int(no), "meta": meta, "result": res})
                if len(rest) == 4 and rest[3] == "events":
                    try:
                        after = int((q.get("after") or ["0"])[0])
                    except ValueError:
                        after = 0
                    return self._json({"events": _events(sd, after)})
            if len(rest) == 2 and rest[1] == "log":
                # 子进程的原样输出。★ 界面在"一条事件都没有"的时候必须看它 ——
                # 那多半意味着 python 那边一启动就失败了，原因只在这里。
                p = d / "run.log"
                if not p.exists():
                    return self._json({"log": ""})
                from loop.process_log import status
                with p.open("rb") as stream:
                    stream.seek(max(0, p.stat().st_size - 16000))
                    txt = stream.read(16000).decode("utf-8", "replace")
                capacity = status(p)
                notice = capacity.get("notice", "")
                return self._json({"log": (notice + "\n" if notice else "") + txt[-4000:], "size": p.stat().st_size, **capacity})

        return self._json({"error": "没有这个接口"}, 404)

    # 先识别请求身份与受控入口，再分派工作间、会话和遗留操作；可追踪请求保留最终回执。
    def do_POST(self):
        global POLICY
        self._operation_id = None
        self._human_action_id = None
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/")
        # Authenticate before reading any body; bound both size and read time.
        if not self._authed():
            self.close_connection = True
            return self._deny()
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._json({"error": "Transfer-Encoding is not supported"}, 400)
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) > 1:
                raise ValueError("duplicate Content-Length")
            n = int(lengths[0]) if lengths else 0
            if n < 0:
                raise ValueError("negative Content-Length")
        except ValueError:
            self.close_connection = True
            return self._json({"error": "Invalid Content-Length"}, 400)
        if n > 32 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "Request body exceeds 32 MiB"}, 413)
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(15)
            raw = self.rfile.read(n) if n else b""
            if len(raw) != n:
                raise ValueError("incomplete request body")
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
        except (TimeoutError, OSError):
            self.close_connection = True
            return self._json({"error": "Request body read timed out"}, 408)
        except (ValueError, UnicodeError):
            self.close_connection = True
            return self._json({"error": "Invalid JSON request body"}, 400)
        finally:
            self.connection.settimeout(previous_timeout)

        # ★ POST 全要钥匙，一个例外都不留（2026-10-26）：这里每一个入口都能改东西 ——
        #   起任务、写凭据、删会话、回退记忆、应用子任务的 diff。
        #   GET 那边放行 `/api/health` 是为了"探活"，POST 没有这种需求。
        if not self._authed():
            return self._deny()

        if path == "/api/sessions" or (path.startswith("/api/sessions/") and path.endswith(("/say", "/interrupt", "/stop"))):
            try:
                PROTOCOL.validate_command(body)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            if path.endswith(("/say", "/interrupt")):
                directory = _resolve_sid(path.split("/")[-2])
                if directory is not None:
                    from loop import execution_policy, workbench
                    saved = execution_policy.checkpoint_config(directory, workbench.read(directory / "session.json"))
                    if execution_policy.retired_checkpoint(saved):
                        return self._json({"error": "旧运行已退役，请发送新的 L0 任务",
                                           "code": "execution_retired"}, 409)
                    if saved.get("crew") == "l4":
                        return self._json({"error": "请在会话的持续委托面板继续",
                                           "code": "delegation_required"}, 409)
        from loop import legacy_migration as _MIG, workbench as _WB
        from loop.human_actions import safe_stop
        if not safe_stop(path,body):
            try:
                _MIG.check_client(path,self.headers.get("X-Harness-Client-Version"))
            except _WB.Problem as error:
                return self._json({"ok":False,"code":error.code,"error":error.message},error.status)
        if path == "/api/jobs" or path.startswith("/api/jobs/"):
            return self._json({"code": "execution_retired", "error": "旧任务仅供查看，请在会话中建立持续委托"}, 410)
        if path == "/api/operations/reconcile":
            ids = body.get("ids")
            seal_missing = body.get("sealMissing", False)
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 256
                    or any(not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", ident)
                           for ident in ids) or type(seal_missing) is not bool):
                return self._json({"error": "Invalid operation reconciliation request"}, 400)
            return self._json({"states": OPS.reconcile(ROOT, ids, seal_missing)})
        request_id = self.headers.get("X-Request-ID")
        if path == "/api/agent-tools":
            from loop import agent_gateway, workbench
            authorized_ident = agent_gateway.authorize(self.headers.get("X-Harness-Agent-Token"), sys.modules[__name__])
            try:
                agent_gateway.validate_call(authorized_ident, body, request_id or "")
            except workbench.Problem as error:
                return self._json({"ok":False,"code":error.code,"error":error.message}, error.status)
        if request_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
                return self._json({"error": "Invalid X-Request-ID"}, 400)
            try:
                fresh, old = OPS.claim(ROOT, request_id, path, body)
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            if not fresh:
                if old["response"] is not None:
                    return self._json(old["response"], old["code"])
                return self._json({"request_id": request_id, "state": old["state"],
                                   "error": "原请求结果待确认，请查询操作状态，不要重复执行"}, 202)
            self._operation_id = request_id
            body["_request_id"] = request_id

        if path == "/api/agent-tools":
            from loop import agent_gateway, workbench
            ident = agent_gateway.authorize(self.headers.get("X-Harness-Agent-Token"), sys.modules[__name__])
            if not ident:
                return self._deny()
            try:
                directory = _resolve_sid(ident)
                watch = CTRL.StopWatch(CTRL.path(directory)) if directory else None
                def validate_live():
                    if (agent_gateway.authorize(self.headers.get("X-Harness-Agent-Token"), sys.modules[__name__]) != ident
                            or not watch or watch.immediate()):
                        raise workbench.Problem("expired_run", "所属运行已结束或能力已轮换，未启动新操作", 403)
                return self._json(agent_gateway.dispatch(ident, body, validate_live, sys.modules[__name__]))
            except workbench.Problem as error:
                return self._json({"ok":False,"code":error.code,"error":error.message}, error.status)

        if not path.startswith("/api/v2/"):
            from loop import human_actions
            try:
                self._human_action_id=human_actions.begin(path,body)
            except Exception:
                if not safe_stop(path,body):
                    return self._json({"error":"人工操作留底不可用，尚未开始新操作"},503)
        if path.startswith("/api/v2/"):
            from loop import workbench
            code, result = workbench.handle("POST", path, {}, body, sys.modules[__name__])
            return self._json(result, code)

        pending_match = re.fullmatch(r"/api/sessions/([A-Za-z0-9._-]+)/pending/resolve", path)
        if pending_match:
            d = _resolve_sid(pending_match.group(1))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            if body.get("confirm") is not True:
                return self._json({"error": "核对日志和产出后才能确认归档"}, 400)
            code, out = _acknowledge_pending(d)
            return self._json(out, code)

        if path == "/api/gflow/config":
            code, out = _gflow_config_save(body)
            return self._json(out, code)

        # ★ 把子任务的改动**应用**到工作区（2026-10-26）。
        #   为什么先 `--check`：diff 是它当时看到的那份内容算出来的，工作区可能已经变了。
        #   check 不过就把 git 的原话交回去 —— 不猜、不强行、不做"部分应用"。
        #   ★ 没有"自动合并"按钮是刻意的：合并冲突让人决定，机器猜错的代价比多一次点击大得多。
        _ma = re.fullmatch(r"/api/sessions/([^/]+)/sub/(\d{1,4})/apply", path)
        if _ma:
            d = _resolve_sid(_ma.group(1))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            sd = d / "sub" / _ma.group(2)
            if not sd.is_dir():
                return self._json({"error": "没有这个子任务"}, 404)
            dp = sd / "change.diff"
            if not dp.exists():
                return self._json({"error": "这趟没有改动可应用"}, 400)
            diff = dp.read_text(encoding="utf-8")
            if not diff.strip() or "没有改任何文件" in diff:
                return self._json({"error": "这趟没有改动可应用"}, 400)
            mp = sd / "meta.json"
            meta = {}
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            if meta.get("applied"):
                return self._json({"ok": True, "already": True,
                                   "note": "这份改动已经应用过了"})
            cwd = ""
            sj = d / "session.json"
            if sj.exists():
                try:
                    cwd = json.loads(sj.read_text(encoding="utf-8")).get("cwd") or ""
                except Exception:
                    cwd = ""
            cwd = cwd or str(ROOT)

            def _git(args, data=None):
                return subprocess.run(["git", "-C", cwd] + args, input=data,
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace")

            if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
                return self._json({"error": "这个工作区不是 git 仓库，用不了 git apply。",
                                   "hint": "diff 在 %s，可以手动对比。" % dp}, 400)
            chk = _git(["apply", "--check", "--whitespace=nowarn", "-"], diff)
            if chk.returncode != 0:
                return self._json({"ok": False,
                                   "error": "这段 diff 现在应用不上（工作区可能已经变了）",
                                   "detail": (chk.stderr or "")[:1200],
                                   "hint": "让子任务重跑一遍，或者手动合并。"}, 409)
            got = _git(["apply", "--whitespace=nowarn", "-"], diff)
            if got.returncode != 0:
                return self._json({"ok": False, "error": "应用时失败",
                                   "detail": (got.stderr or "")[:1200]}, 500)
            meta["applied"] = True
            meta["applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                              encoding="utf-8")
            except Exception:
                pass
            return self._json({"ok": True,
                               "note": "已应用到工作区（再 git diff 看一眼更安心）"})

        # ★ 建一份习惯区模板（2026-10-26）。**只在人点了「建一个」时**才写文件 ——
        #   读的时候偷偷创建，等于替人做了决定（那会变成一份没人认领的配置）。
        if path == "/api/rules/exception/revoke":
            # ★ 撤一条「以后这类都别问」（2026-10-26）。**必须能撤**：
            #   例外是"以后不问"，它唯一的安全阀就是"随时能收回"。
            #   不能撤的例外，用一周就变成没人敢动的既成事实。
            from loop import ruleedit as _REr
            return self._json(_REr.revoke(rid=str(body.get("id") or ""),
                                          domain=str(body.get("domain") or ""),
                                          key=str(body.get("key") or "")))

        if path == "/api/habits/add":
            # ★ 人力手写的这一条，意思就是要它生效 —— 所以同名死记录会被摘掉、
            #   白名单模式下会被显式勾上（否则"写了没反应"，而屏幕上毫无提示）。
            from loop import habits as _Ha
            return self._json(_Ha.add(str(body.get("title") or ""),
                                      str(body.get("body") or ""),
                                      str(body.get("group") or "")))

        if path == "/api/habits/body":
            from loop import habits as _Hb
            return self._json(_Hb.edit_body(str(body.get("title") or ""),
                                            str(body.get("body") or "")))

        if path == "/api/habits/remove":
            # ★ 删一条要**连开关表那条一起删**，否则表里多一个死名字，
            #   下次写同名习惯会被上一条的开关状态接管。
            from loop import habits as _Hd
            return self._json(_Hd.remove(str(body.get("title") or "")))

        if path == "/api/habits/prune":
            # 只清开关表里的死名字，不动文件。
            from loop import habits as _Hp
            return self._json(_Hp.prune())

        if path == "/api/habits/rename":
            # ★ 改名必须**和开关表一起搬**（2026-10-26）。开关是按标题映射的，
            #   只改文件不改表 = 那条习惯悄悄变回"默认开"，而人以为自己只是改了个名字。
            from loop import habits as _Hr
            return self._json(_Hr.rename(str(body.get("old") or ""),
                                         str(body.get("new") or "")))

        if path == "/api/habits/toggle":
            # ★ 开关是**用户级、跨项目一份**（2026-10-26 用户拍板）：
            #   一份表意味着只有一份真源，改一条所有会话立刻生效。
            from loop import habits as _Ht
            return self._json(_Ht.toggle(str(body.get("title") or "")))

        if path == "/api/habits" and body.get("create"):
            from loop import habits as _HAB
            return self._json(_HAB.ensure())

        # ★ 对**运行中**的子任务补一句（2026-10-26，steer）。
        #   和"叫停再重派"的区别不是省事，是省 token：方向上偏了一点，
        #   重来要把它已经翻过的文件、跑过的命令全部再花一遍。
        _mt = re.fullmatch(r"/api/sessions/([^/]+)/sub/(\d{1,4})/steer", path)
        if _mt:
            d = _resolve_sid(_mt.group(1))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            sd = d / "sub" / _mt.group(2)
            if not sd.is_dir():
                return self._json({"error": "没有这个子任务"}, 404)
            txt = str(body.get("text") or "").strip()
            if not txt:
                return self._json({"error": "没写要说什么"}, 400)
            if len(txt) > 2000:
                return self._json({"error": "插话上限 2000 字 —— 长的请叫停重派，"
                                            "插话只该改方向，不该重写任务"}, 413)
            meta = {}
            mp = sd / "meta.json"
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            st = str(meta.get("status") or "")
            if st and st != "running":
                # ★ 已经跑完就明说"这句没用上"，不要假装送达 ——
                #   人以为它看到了，结果复盘时完全对不上。
                return self._json({"ok": False, "late": True,
                                   "error": "它已经跑完了（%s），这句话没用上" % st}, 409)
            try:
                with (sd / "steer.txt").open("a", encoding="utf-8") as f:
                    f.write(txt + "\n")
            except Exception as e:
                return self._json({"error": "没写进去：%s" % e}, 500)
            return self._json({"ok": True, "note": "下一次模型调用前它会看到"})

        # ★ 叫停一个子任务（2026-10-26）：只写一个 `stop` 文件。
        #   子 agent 在父的进程里同步跑，人在界面上点按钮走的是**另一个进程**的 HTTP，
        #   两边唯一共用的东西只有磁盘 —— 进程内的旗子在这儿传不过去。
        _ms = re.fullmatch(r"/api/sessions/([^/]+)/sub/(\d{1,4})/stop", path)
        if _ms:
            d = _resolve_sid(_ms.group(1))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            sd = d / "sub" / _ms.group(2)
            if not sd.is_dir():
                return self._json({"error": "没有这个子任务"}, 404)
            try:
                (sd / "stop").write_text("1", encoding="utf-8")
            except Exception as e:
                return self._json({"error": "叫停没落下去：%s" % e}, 500)
            return self._json({"ok": True,
                               "note": "下一轮开头就停；正在跑的那条命令要等它自己结束"})

        if path == "/api/skills/install":
            # 装一个技能包（前端把 .zip 读成 base64 传过来）。
            # ★ 为什么走接口而不是让界面直接解压写盘：解压逻辑只该有一份
            #   （安全边界在 skills.install_zip 里），而界面没有 Node 权限。
            import base64 as _b64
            from loop import skills as _SK
            raw64 = str(body.get("data") or "")
            if raw64.startswith("data:"):                 # 容忍 dataURL 前缀
                raw64 = raw64.split(",", 1)[-1]
            try:
                blob = _b64.b64decode(raw64, validate=False)
            except Exception as e:
                return self._json({"error": "包体解不开：%s" % e}, 400)
            if not blob:
                return self._json({"error": "没收到包体"}, 400)
            if len(blob) > 30 * 1024 * 1024:
                return self._json({"error": "压缩包超过 30 MB，没装"}, 413)
            level = "project" if body.get("level") == "project" else "user"
            out = _SK.install_zip(blob, level=level, repo_root=ROOT,
                                  overwrite=bool(body.get("overwrite")))
            return self._json(out, 200 if out.get("ok") else
                              (409 if out.get("already") else 400))

        if path == "/api/commands/expand":
            # 把一个 `/name 参数` 展开成最终要发出去的文本。
            # ★ 展开规则**只在 Python 那一份**（loop/commands.render）。
            #   前端再实现一遍就是第二套规则：两处一处改了忘另一处，
            #   不报错，只是同一个命令发出去的内容不一样。
            from loop import commands as _CMD
            nm = str(body.get("name") or "").strip()
            out = _CMD.render(nm, str(body.get("args") or ""), ROOT)
            return self._json(out, 200 if out.get("ok") else 404)

        if path == "/api/skills/remove":
            # 卸一个技能。★ 这是**不可逆**动作（把目录删了），所以两道关：
            #   显式 confirm + 只许删技能根目录下的子目录（不许顺着 name 跑到别处去）。
            from loop import skills as _SK
            nm = str(body.get("name") or "").strip()
            if not body.get("confirm"):
                return self._json({"error": "卸载要带 confirm: true"}, 400)
            if not nm:
                return self._json({"error": "没说要卸哪个"}, 400)
            hit = [x for x in _SK.scan(ROOT) if x["name"] == nm]
            if not hit:
                return self._json({"error": "没有这个技能：%s" % nm}, 404)
            target = Path(hit[0]["dir"]).resolve()
            # 只允许删"某个技能根目录的直接子目录"
            ok_root = False
            for _lv, _r in _SK.roots(ROOT):
                try:
                    _rr = Path(_r).resolve()
                except OSError:
                    continue
                if target.parent == _rr:
                    ok_root = True
                    break
            if not ok_root:
                return self._json({"error": "这个路径不在技能目录下，不删：%s" % target}, 400)
            try:
                shutil.rmtree(target)
            except OSError as e:
                return self._json({"error": "删不掉：%s" % e}, 500)
            return self._json({"ok": True, "name": nm})

        if path == "/api/sessions":
            _crew = body.get("crew") or "l0"
            code, out = _start(body.get("task") or "", body.get("min_reads"),
                               body.get("min_chars"), body.get("max_turns"),
                               POLICY, body.get("cwd") or "",
                               blank=bool(body.get("blank")),
                               ctx=body.get("ctx"), crew=_crew)
            return self._json(out, code)

        if path.startswith("/api/sessions/") and path.endswith("/stop"):
            d = _resolve_sid(path[len("/api/sessions/"):-len("/stop")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            proc = _active_proc(d)
            if proc and proc.poll() is None:
                # 先落盘，再发停止：服务在等待期间重启也能恢复这份待办。
                _req = {"note": "暂停前的任务继续；没有新增需求，先收揽已完成工作再接着做。",
                        "_request_id": body.get("_request_id")}
                if not _queue_pending(d, proc, "pause", _req):
                    return self._json({"error": "已经有一份暂停/插话在等待"}, 409)
                CTRL.request_stop(d, why=body.get("why") or "界面请求步边界暂停",
                                  after_turn=True)
                return self._json({"ok": True, "queued": True,
                                   "note": "将在当前执行步完成后暂停；继续时会先收揽断点前的进度"})
            # 没有活动进程时也留一份步边界控制记录（命令行/探针同一条路径），
            # 但不生成待办，避免下次普通发言被误当成暂停恢复。
            CTRL.request_stop(d, why=body.get("why") or "界面请求步边界暂停",
                              after_turn=True)
            return self._json({"ok": True, "note": "当前没有在跑的任务"})

        # ★★ 步边界插话（2026-10-31 用户明确的最小粒度）：
        #   当前执行步（模型调用 + 它要的工具）完整做完，在下一执行步开始前停。
        #   不等整个任务循环自然结束，也不截断当前执行步；后台等待不限时。
        #   停下后整理进展 → 从规划者重新收揽 → 最终答复整合插话前后内容。
        if path.startswith("/api/sessions/") and path.endswith("/interrupt"):
            d = _resolve_sid(path[len("/api/sessions/"):-len("/interrupt")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "插话不能是空的"}, 400)
            proc = _active_proc(d)
            if proc and proc.poll() is None:
                # 先把完整请求落盘，再发步边界停止。等待不限时，重启可恢复。
                _req = {"text": text, "min_reads": body.get("min_reads"),
                        "min_chars": body.get("min_chars"), "max_turns": body.get("max_turns"),
                        "images": body.get("images"), "ctx": body.get("ctx"),
                        "crew": body.get("crew") or "l0", "_request_id": body.get("_request_id")}
                if not _queue_pending(d, proc, "interrupt", _req):
                    return self._json({"error": "已经有一条插话在等当前执行步完成，"
                                                "等它重开后再补下一句"}, 409)
                CTRL.request_stop(d, why="人插话：当前执行步完成后重新收揽",
                                  after_turn=True)
                return self._json({"queued": True, "session": d.name,
                                   "note": "已收到插话：当前执行步完成后重新收揽，不限时"}, 200)
            code, out = _continue(d, text, body.get("min_reads"), body.get("min_chars"),
                                  body.get("max_turns"), images=body.get("images"),
                                  ctx=body.get("ctx"), steering=True)
            return self._json(out, code)

        if path == "/api/diagnose":
            # ★ 打一个诊断包（2026-10-26）。为什么要有这个接口，而不只是命令行：
            #   出问题时人正在看这一页，让他去开终端、找路径、记得加 --no-logs，
            #   等于把「自救」变成一道考试。这里只做一件事：**按默认参数打一个包，返回路径**。
            #   ★ 它不收任何路径参数 —— 收路径就等于开一个"把任意文件打进包里"的输入框。
            from loop import diagnose as _Dg
            _r = _Dg.build(with_logs=bool(body.get("logs", True)),
                           keep_dir=bool(body.get("dir", False)))
            return self._json(_r)

        if path == "/api/grants":
            # ★ 三个动作：allow / deny / revoke（2026-10-26）。
            #   `allow` 之后如果带 `session`，就**顺手把那句话续上去** ——
            #   不续的话，人点完按钮还得自己再打一句"继续"，而"点完没反应"
            #   会让人以为按钮坏了（这一条在别的功能上已经踩过一次）。
            from loop import grants as _Gg
            act = str(body.get("act") or "allow").strip().lower()
            sig = str(body.get("sig") or "").strip()
            why = str(body.get("why") or "").strip() or "界面里点的"
            label = str(body.get("label") or "").strip()
            if act == "revoke":
                r = _Gg.revoke(sig, why=why, src="ui")
            elif act == "deny":
                r = _Gg.deny(sig, why=why, src="ui", label=label)
            elif act == "never":
                # ★★「以后这类都别问」（2026-10-26）：一次做两件事。
                #   ① 这一步**现在就放行**（人已经看过它了，明确表示同意）。
                #      不这么做的话人要点两次 —— 先"允许这一次"、再"以后别问"，
                #      而第二次点的东西**还没生效**，下一次照样问他。那种摩擦
                #      会把人推回"把闸整个关掉"，而那是不可逆的。
                #   ② 记一条**待采纳**的免问提案（不直接改规则文件）——
                #      扩大权力那一步必须留一次人的复核，界面上看得见、能撤。
                r = _Gg.grant(sig, kind=str(body.get("kind") or ""), mode=_Gg.ONCE,
                              ttl=int(body.get("ttl") or _Gg.DEFAULT_TTL),
                              why=why + "（并且要求：以后这类都别问）", src="ui",
                              label=label)
                if r.get("ok"):
                    _sc = body.get("scope")
                    if isinstance(_sc, dict) and _sc.get("domain"):
                        from loop import proposals as _PRv
                        from loop import ruleedit as _REv
                        # ★ 提案阶段就验一遍（2026-10-26 端到端走查），
                        #   否则待采纳区会先冒出一条"以后读 ~/.ssh 不问"，
                        #   而人只会把它读成"这系统居然能提这种提案"。
                        _v = _REv.validate_scope(_sc)
                        if not _v.get("ok"):
                            r["proposal"] = {"ok": False, "error": _v.get("error")}
                        else:
                            _txt = _REv.describe_scope(_sc)
                            _pr = _PRv.add(CH.rules_store(), None, "", _txt["title"],
                                           _txt["body"], why=why, src="ui", allow=_sc)
                            r["proposal"] = _pr
                            r["scope"] = _sc
                    else:
                        r["proposal"] = {"ok": False,
                                         "error": "这一步没能算出「这类」的范围，"
                                                  "只给了这一次许可"}
            else:
                mode = _Gg.TTL if str(body.get("mode") or "") == _Gg.TTL else _Gg.ONCE
                r = _Gg.grant(sig, kind=str(body.get("kind") or ""), mode=mode,
                              ttl=int(body.get("ttl") or _Gg.DEFAULT_TTL),
                              why=why, src="ui", label=label)
            sid = str(body.get("session") or "").strip()
            if r.get("ok") and sid and act in ("allow", "deny", "never"):
                d = _resolve_sid(sid)
                if d is None:
                    r["resume"] = {"error": "没有这个会话（许可已经记下了）"}
                else:
                    # ★★ 先把"该跟它说什么"拼好，再决定立刻发还是排队（2026-10-28）。
                    #   上一版把排队那段放在拼话之前，`text` 那时还不存在 ——
                    #   lambda 会在**后台线程里**炸 NameError，而那个异常没人看得见，
                    #   表现就是"批了以后什么都不发生"。
                    if act in ("allow", "never"):
                        # ★ 秒 → 分钟（`ttl` 存秒；说给人听用分钟）
                        word = ("只放行这一次" if r.get("mode") == _Gg.ONCE
                                else "接下来 %d 分钟内不再问"
                                     % max(1, round(int(r.get("ttl") or 0) / 60)))
                        text = ("人已经允许了这一步（%s）：%s\n"
                                "现在**重新执行刚才被拦下的那一步**，"
                                "内容一个字都别改 —— 许可是按内容签名的，"
                                "改了就变成另一步，又要重新问一遍。做完继续原来的活。"
                                % (word, label or sig))
                        if act == "never":
                            # ★ 这句必须说：人点的是「以后别问」，但那条**还没生效**。
                            #   不说的话，模型会以为"以后这类都免了"，然后开始
                            #   连做十次 —— 而第九次会被重新拦，人会觉得"它疯了"。
                            _pf = r.get("proposal") or {}
                            text += ("\n另外：人把它记成了一条**待采纳**的"
                                     "「以后这类都别问」。**它现在还没生效**"
                                     "（要人采纳才不问），所以后面同样的动作照旧可能被拦，"
                                     "被拦了就照旧等 —— 别当成已经放开了。")
                    else:
                        text = ("人**不同意**这一步：%s\n"
                                "不要换个写法再试同一件事（那等于绕过人的决定），"
                                "也不要换别的工具达到同样效果。"
                                "直接给结论：这件事为什么做不到、需要人做什么。"
                                % (label or sig))
                    proc = _active_proc(d)
                    if proc and proc.poll() is None:
                        # ★ 这一轮还在**收尾**（本我复核，常见 20 秒~3 分钟）。
                        #   原来这里直接拒绝，而拒绝理由界面不显示 —— 人看到的只有
                        #   「已允许 · 重新发一次同样的动作才会用掉它」，再点也一样拒绝。
                        #   ⇒ 改成排队：收完自动接着跑。
                        _ctxq = body.get("ctx")
                        _q2 = _queue_pending(d, proc, "say",
                                             {"text": text, "ctx": _ctxq, "internal_grant_resume": True,
                                              "_request_id": body.get("_request_id")})
                        r["resume"] = ({"queued": True,
                                        "note": "这一轮还在收尾，收完自动接着跑"} if _q2 else
                                       {"error": "已有其他请求在排队；许可已保存，但本次续跑没有排入"})
                    else:
                        code, out = _continue(d, text, ctx=body.get("ctx"), user_input=False)
                        r["resume"] = out if code == 200 else {"error": out}
            # ★ 这一行被我在 2026-10-28 重排那段时误删过一次，后果是
            #   `/api/grants` 的请求一路落到文件末尾的「没有这个接口」（404）——
            #   而界面把它读成"没批成"，端到端探针也当场红了。
            #   凡是"分支末尾的 return"，重排时最容易一起被带走。
            return self._json(r, 200 if r.get("ok") else 400)

        if path in ("/api/facts/drop", "/api/facts/edit", "/api/facts/prune"):
            # ★ 三个改事实的动作。**删/改都先备份**，且都是"人按的"——
            #   所以这里不做"要不要人确认"的判断，界面那一侧用两次点击上膛。
            from loop import facts as _Ff
            sc = str(body.get("scope") or "project")
            kd = str(body.get("kind") or "")
            if not kd:
                return self._json({"ok": False, "error": "要指明是哪个类型的事实"}, 400)
            if path.endswith("/drop"):
                r = _Ff.drop(sc, kd, int(body.get("n") or 0), ROOT)
            elif path.endswith("/edit"):
                r = _Ff.edit_entry(sc, kd, int(body.get("n") or 0),
                                   str(body.get("text") or ""), ROOT)
            else:
                _d = body.get("days")
                r = _Ff.prune(sc, kd, int(_d) if _d not in (None, "") else None, ROOT)
            return self._json(r, 200 if r.get("ok") else 400)

        if path == "/api/memory/revert":
            # ★ 回退：拿一份备份盖回记忆文件（2026-10-26）。
            #   这是**唯一**能覆盖记忆文件的接口，所以它的边界校验收在最里面
            #   （`memperms.target_ok`）：只认 HABITS.md / tree/*.md / agent-memory*/MEMORY.md。
            from loop import memperms as _MPr
            r = _MPr.revert(str(body.get("region") or ""), str(body.get("file") or ""),
                            str(body.get("backup") or ""),
                            str(body.get("why") or ""), ROOT)
            return self._json(r, 200 if r.get("ok") else 400)

        if path.startswith("/api/sessions/") and path.endswith("/say"):
            d = _resolve_sid(path[len("/api/sessions/"):-len("/say")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "说点什么呢"}, 400)
            # ★ 旧格式会话（只有 trace.jsonl、没有 session.json）续不了。
            #   拦在这里说人话：不拦的话，报错来自深层的 `--resume`，
            #   人会以为是"服务坏了" —— 而真实原因只是"这一轮没留下可续的存档"。
            if (d / "events.jsonl").exists() is False and (d / "trace.jsonl").exists() \
                    and not (d / "session.json").exists():
                return self._json({"error": "这是旧格式的会话（轨迹只留了一份、没留可续的存档），"
                                            "能翻着看，但不能接着跑。想继续就在新会话里重说一遍。"}, 409)
            _imgs = body.get("images")
            _ctx = body.get("ctx")
            proc = _active_proc(d)
            if proc and proc.poll() is None:
                # ★ 排队，不拒绝（2026-10-28）：见 _queue_after 的说明。
                _q = _queue_pending(d, proc, "say", dict(body, text=text))
                if not _q:
                    return self._json({"error": "已有请求在排队，这条消息尚未接收，请保留后重试",
                                       "accepted": False, "queued": False}, 409)
                return self._json({"queued": True, "accepted": True, "session": d.name,
                                   "note": "这一轮还在收尾，收完自动接着跑"}, 200)
            code, out = _continue(d, text, body.get("min_reads"),
                                  body.get("min_chars"), body.get("max_turns"),
                                  images=_imgs, ctx=_ctx, crew=(body.get("crew") or "l0"))
            return self._json(out, code)

        if path.startswith("/api/sessions/") and path.endswith("/pin"):
            # 置顶/取消置顶。可逆、无害，所以只有一道关：会话得存在。
            d = _resolve_sid(path[len("/api/sessions/"):-len("/pin")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            want = bool(body.get("pinned"))
            mark = d / "pinned"
            try:
                if want:
                    mark.write_text("", encoding="utf-8")
                elif mark.exists():
                    mark.unlink()
            except OSError as e:
                return self._json({"error": "写不进去：%s" % e}, 500)
            return self._json({"ok": True, "pinned": want})

        if path.startswith("/api/sessions/") and path.endswith("/rename"):
            # 改名是可逆动作（再改回来就行），所以只要一道关：标题别为空、别太长。
            d = _resolve_sid(path[len("/api/sessions/"):-len("/rename")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            # 压掉换行和多余空格：标题是一行，带换行会把左栏撑开
            title = " ".join(str(body.get("title") or "").split())[:40]
            nf = d / "title.txt"
            try:
                if title:
                    nf.write_text(title, encoding="utf-8")
                elif nf.exists():
                    nf.unlink()          # 空标题 = 恢复成 task.md 第一行
            except OSError as e:
                return self._json({"error": "写不进去：%s" % e}, 500)
            return self._json({"ok": True, "title": title})

        if path.startswith("/api/sessions/") and path.endswith("/delete"):
            # ★ 删目录是**不可逆**的，所以两道关：显式 confirm，且不许删正在跑的。
            #   和"不可逆动作先停"是同一条道理 —— 只是这里人已经在界面上点了确认。
            d = _resolve_sid(path[len("/api/sessions/"):-len("/delete")].strip("/"))
            if d is None:
                return self._json({"error": "没有这个会话"}, 404)
            if not body.get("confirm"):
                return self._json({"error": "删除要带 confirm: true"}, 400)
            proc = _active_proc(d)
            if proc and proc.poll() is None:
                return self._json({"error": "这个会话还在跑，别删"}, 409)
            from loop import workbench as _WB, debugging as _DBG
            if _WB.registry()["threads"].get(d.name, {}).get("projectless"):
                _DBG.stop_conversation(d.name)
            try:
                shutil.rmtree(d)
            except OSError as e:
                return self._json({"error": "删除未完成：%s" % e}, 500)
            return self._json({"ok": True, "deleted": d.name})

        if path == "/api/policy":
            # ★ 改门禁档位（2026-10-27 用户：「权限这个按钮能不能直接出下拉选」）。
            #   原来是启动参数，进程起来就定了，界面上只能看不能改。
            #   ★ 这条是**人的入口**：要带钥匙（默认拒绝那一层已经挡住 agent），
            #     而且改到 trusted 必须显式带 confirm —— 它意味着「不再逐条问你」。
            want = str(body.get("policy") or "").strip()
            if want not in POLICY_CHOICES:
                return self._json({"ok": False,
                                   "error": "不认识的档位：%s（只认 %s）"
                                            % (want or "空", " / ".join(POLICY_CHOICES))}, 400)
            if want == "trusted" and not body.get("confirm"):
                return self._json({"ok": False, "need_confirm": True,
                                   "why": "trusted = 不再逐条问你。底线清单仍然拦凭据读写、"
                                          "写系统目录、改放权配置这三类 —— 但其余都不问了。"})
            if want == POLICY:
                return self._json({"ok": True, "policy": POLICY,
                                   "note": "本来就是「%s」" % POLICY_CN[want]})
            _bad = _save_policy(want)
            if _bad:
                return self._json({"ok":False,"error":_bad,"policy":POLICY,"note":"保存失败，旧有效权限保留"},503)
            POLICY = want
            return self._json({"ok": True, "policy": POLICY,
                               "note": "已改成「%s」，只影响**后面开始**的轮次" % POLICY_CN[want]
                                       + ("；" + _bad if _bad else "")})

        if path == "/api/models":
            # ★ 五个动作（2026-10-26）：save / del / select / swap / import_env。
            #   ★ 这里**没有**「把 key 读出来给我」这一类动作 —— 只有写和掩码。
            from loop import models as _MD
            r = _MD.user_action(body)
            if r.get("ok"):
                r["state"] = _MD.public()
            return self._json(r)

        return self._json({"error": "没有这个接口"}, 404)

    def log_message(self, *a):
        return


def _port_busy(port: int) -> bool:
    """这个端口上已经有人在听吗。

    ★ 为什么非得查（2026-10-26 实测）：`ThreadingHTTPServer` 默认
      `allow_reuse_address=1`，而 **Windows 上它允许两个进程绑同一个端口** ——
      于是新旧两个服务同时活着，请求落到先来的那个上。
      表现是"代码改了、功能没出现"，而且两边日志都是正常的。
      这个坑我今天就踩了一次（`/api/memory` 少了新字段），查了十分钟。
    """
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


# 启动本机 HTTP 服务并恢复未结束的持久操作，程序退出时回收实际拥有的进程。
def main(argv=None) -> int:
    global POLICY, SCHEDULED_TASKS
    ap = argparse.ArgumentParser(description="harness 本地 API")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--policy", default=None,
                    choices=POLICY_CHOICES,
                    help="门禁级别（进程级默认）。不给就用上次在界面上选的那一档，"
                         "再没有就是 workspace。trusted 只给调试用 —— "
                         "它意味着模型能读写工作目录外的任何东西")
    ap.add_argument("--token", action="store_true",
                    help="只打印这把钥匙然后退出（命令行调试用；不启动服务）")
    a = ap.parse_args(argv)

    # ★ 钥匙要在起服务**之前**定下来：起完之后再定，中间那几毫秒是敞开的。
    global TOKEN
    try:
        TOKEN, _src = load_token(mint=True)
    except OSError:
        print("✗ 没有可用的钥匙，服务不起（不能退回「裸奔」）", file=sys.stderr)
        return 2
    if a.token:
        # 只打印钥匙本身，方便 `TOKEN=$(python bin/server.py --token)`。
        print(TOKEN)
        return 0
    # 顺手把端口占用也查了（今天实测：两个进程蹲同一个端口，请求落到旧的那个上，
        # 表现是"更新了但功能没出现"）。
    if _port_busy(a.port):
        print("✗ 端口 %d 已经被别的进程占着 —— 先关掉它，或换个 --port。"
              % a.port, file=sys.stderr)
        return 2

    POLICY = a.policy or _load_policy() or "workspace"
    # ★ 打包形态第一次启动：把模板/记忆树/配置从程序目录拷到数据目录（只拷缺的）。
    #   开发形态下这是空操作（两个根是同一个）。
    _sd = P.seed()
    if _sd.get("copied"):
        print("首次启动：把模板拷到数据目录 —— %s" % "；".join(_sd["copied"]))
    if _sd.get("error"):
        print("✗ 数据目录有问题：%s" % _sd["error"])
    # 恢复上次服务退出时仍在等待的暂停/插话；claimed 不自动重放，防止重复起任务。
    OPS.recover(ROOT)
    _recover_pending()
    from loop import check_sessions
    check_sessions.recover()
    # 旁路观察：跨委托的文件争用（只记、只投通知，不拦任何写）。
    _watchtower_start()
    srv = WorkbenchHTTPServer(("127.0.0.1", a.port), H)
    from loop import agent_gateway
    agent_gateway.origin = "http://127.0.0.1:%d" % srv.server_address[1]
    from loop import conversation_record, repository_staging, scheduled_tasks, subtasks
    SCHEDULED_TASKS = scheduled_tasks.Scheduler(sys.modules[__name__])
    SCHEDULED_TASKS.start()
    threading.Thread(target=subtasks.recover,args=(sys.modules[__name__],),daemon=True).start()
    threading.Thread(target=conversation_record.recover, args=(_resolve_sid,), daemon=True).start()
    threading.Thread(target=repository_staging.recover, name="repository-staging-recovery", daemon=True).start()
    from loop import sandbox_lifecycle
    sandbox_lifecycle.start_recovery(P.data_root())
    print("harness 接口：http://127.0.0.1:%d/api/health" % a.port)
    # ★ 说清钥匙从哪来，但**不打印钥匙本身**：控制台会被截图、会进日志。
    print("接口钥匙：%s（`python bin/server.py --token` 查看）" % _src)
    print("门禁：%s（%s）" % (POLICY, "工作目录内自由，出界拒绝"
                              if POLICY == "workspace" else POLICY))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")
    finally:
        SCHEDULED_TASKS.close()
        srv.server_close()
        from loop import verification_workspace

        verification_workspace.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
