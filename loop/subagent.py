"""subagent.py —— 派一个子 agent 去干一件事，只把结论带回来。

★ 它解决什么
    长任务里有一类活是"查清楚再告诉我"：翻五个文件确认某个函数有没有被调用、
    跑一遍验收看哪几组红了、把某个目录的结构摸清楚。
    这些活会让主线上下文里塞满**只有这一次有用**的细节，
    而主线真正需要的只是最后那两行结论。
    子 agent = 一个独立的上下文跑完这件事，只把结论交回来。

★ 三种类型（2026-10-26 加，定义在 `loop/agents.py`）
    general 通用 · explorer 只读探索 · plan 只出方案 · verifier 跑验证判红绿。
    类型**真的**决定能力：plan 连 `run_check` 都没有 ——
    "只准想不准跑"这件事用提示词写是没用的，得从工具表里拿掉。
    用户还能自己写类型：`<仓库>/.harness/agents/*.md`。

★ 三条硬边界（都是**执行边界**，不是提示词建议）

    ① 工具白名单：具体给哪几样由类型定义决定，全集在 `agents.ALLOWED`。
       不在这个表里的工具，模型**连 schema 都看不到** ——
       这和 Claude Code 的 `tools:` 白名单同一个意思。

    ② 权限只降不升：子 agent 的 policy 一律是 `verify`（能核实、不能改），
       **即使父这一轮是 trusted**。理由：父拿到的能力是"可以派活"，
       如果子能在父的限制之外写文件，那"派活"就成了绕过限制的通道。
       ★ 2026-10-26 用户拍板保留这条（Codex 那边是"继承父 sandbox"，
         我们更保守，理由如上）。

    ③ 不能再派子 agent：白名单里没有 task 工具。
       递归没有边界，而"派活"的成本是**乘法**不是加法 ——
       一次 task 里面套三次，是四倍的模型调用。
       Codex 用 `max_depth=1` 允许套一层；我们直接不给，
       等并行（第 3 步）跑稳了再谈"要不要放开一层"。

★ 为什么不复用父的 Registry
    工具对象绑着它自己那本「读过什么」的账（ledger）。
    复用的话，子 agent 读过的文件会记进父的账 ——
    于是父去改一个它**其实没读过**的文件时，`edit_file` 的"必须先读"检查会放行。
    那种错误不报错，只是改错地方。所以宁可多花一次构造的开销。
"""
# 主要引用方包括 `tools.py` 和 `bin/accept.py`。
from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from . import agents as AG
from . import settings as SET

# ★ 全集就是 `agents.ALLOWED`，这里只是个别名。
#   留一个名字是为了让人 grep 得到"子 agent 能用哪些工具"这个答案，
#   而不是去翻两个文件。**具体给哪几样按类型来**（`spec["tools"]`）。
SUBSET = AG.ALLOWED

# ★ 下面这三个是**默认值**。实际生效值从 `harness/charter.json` 的 `agents` 段读
#   （见 `settings.py` 开头那段：一个数字不可能同时适合所有人和所有机器）。
# 同时最多跑几个子 agent。
# ★ 为什么要有这个闸：`task` 标成可并行之后，模型一次回复里可以发 5 个。
#   5 份沙箱副本 + 5 个子循环同时跑，机器会先垮，而不是模型。
MAX_PARALLEL = SET.DEFAULT_AGENTS["max_parallel"]

# 子任务的轮数上限。★ 这是**成本闸**，不是"活干完没有"的判据 ——
#   它防的是"对着一个查不清的点反复跑"，不是说 8 轮就该干完。
MAX_TURNS = SET.DEFAULT_AGENTS["max_turns"]

# 结论长度上限：父 agent 的上下文要装得下，而且结论本来就该短
MAX_CHARS = SET.DEFAULT_AGENTS["max_chars"]

# 超出的不是拒绝，是**排队**：派活这件事，晚几秒开始没关系，拒绝了要模型重发。
_PAR = None
_PAR_N = 0


def _gate():
    """并发闸。第一次用时按配置建，之后一直复用。

    ★ 惰性建的理由：配置是运行期读的，模块导入时读会把第一次的值锁死，
      而这个进程里一般只读一次配置 —— "第一次用时建"既对又便宜。
    """
    global _PAR, _PAR_N
    n = max(1, int(SET.agents().get("max_parallel") or MAX_PARALLEL))
    if _PAR is None or n != _PAR_N:
        _PAR_N = n
        _PAR = threading.Semaphore(n)
    return _PAR


def _build_registry(repo_root: Path, names, python: str = "",
                    no_stage: bool = False, sub_provider=None, sub_dir=None,
                    sub_emit=None, depth: int = 0, spawn_kinds=None,
                    mem: dict | None = None, facts_scope: str = "",
                    facts_kind: str = ""):
    """按类型给的名单造一个独立账本的工具集。

    ★ no_stage：副本里不需要 `.stage` 镜像（主工作区那份就是原件），
      否则能写的子 agent 连第一步都迈不出去。
    ★ sub_provider：**传了才注册 `task`**。子 agent 能不能再派活，就由这一个参数决定 ——
      默认不传（不能），配 `max_depth >= 1` 才传（能派一层）。
    """
    from . import memperms as MP
    from .tools import Registry, default_registry
    full = default_registry(Path(repo_root), python=python, ledger={},
                            no_stage=no_stage, sub_provider=sub_provider,
                            sub_dir=sub_dir, sub_emit=sub_emit, sub_depth=depth,
                            sub_kinds=spawn_kinds,
                            # ★ 记忆权限表传进去，用它决定**造不造**那几个记忆工具。
                            #   不传的话会按主 agent 的默认值造（能提案、能写事实），
                            #   而筛工具的那一步只按 `names` 过滤 —— 于是子 agent
                            #   会拿到它定义里根本没申请的权限，且不报错。
                            mem=dict(mem or MP.DEFAULT_SUB),
                            facts_scope=facts_scope, facts_kind=facts_kind)
    reg = Registry()
    # ★ 档位授的工具**自动给**（不需要在 `tools:` 里点名）——
    #   `tools:` 管的是"文件/命令能力"，档位管的是"记忆能碰几档"，两件事不混。
    for n in list(names) + MP.tools_for(mem or MP.DEFAULT_SUB):
        t = full.get(n)
        if t is not None:
            reg.add(t)
    # ★ 递归能力由 `max_depth` 决定，**不由类型定义决定**（2026-10-26）。
    #   `task` 从来不在 `ALLOWED` 里（它是那条"唯一例外"），所以上面那个循环
    #   永远拿不到它 —— 第一版就是这样，配了 max_depth=1 照样派不出去，
    #   而现象是"配置读了但没生效"，最难查。
    if sub_provider is not None:
        t = full.get("task")
        if t is not None:
            reg.add(t)
    # ★ 账本独立（见模块注释最后一段）。空 dict = 子 agent 的读，父那边看不到。
    reg.ledger = {}
    return reg


def _wjson(p: Path, obj) -> None:
    """写一个 json，失败就算了 —— 记录不能反过来把活搞崩。"""
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _sum_tokens(sd) -> dict:
    """从它自己的事件流里把 token 加回来。

    ★ 为什么要单独算：`loop` 只在每条 `llm` 事件上记**当次**用量，没有累计；
      而且父的账里不该混进子 agent 的消耗 —— 混进去之后，
      「这一轮花了多少」就永远说不清哪些是主体花的、哪些是派活花的。
    """
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
           "calls": 0}
    try:
        raw = (Path(sd) / "events.jsonl").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("act") != "llm":
            continue
        out["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                out[k] += int(r.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return out


def _drain_steer(sd) -> list:
    """把人在中途塞进来的话取走（取走即删，一句话只生效一次）。

    ★ 用文件不用队列：子 agent 在父的进程里跑，而点击来自另一个进程的 HTTP。
      两边唯一共用的东西只有磁盘 —— 和叫停（stop 文件）同一个理由。
    """
    if sd is None:
        return []
    p = Path(sd) / "steer.txt"
    try:
        if not p.exists():
            return []
        txt = p.read_text(encoding="utf-8")
        p.unlink()
    except OSError:
        return []
    return [ln.strip() for ln in txt.splitlines() if ln.strip()]


def run(prompt: str, provider, repo_root, *, kind: str = "general",
        max_turns: int = 0, policy=None, on_event=None, label: str = "",
        sub_dir=None, depth: int = 0) -> dict:
    """跑一个子任务。返回 {ok, text, turns, why, kind, model}，**不抛异常**。

    ★ 不抛异常的理由：子任务是"帮手"，帮手出错不该把主线带崩。
      出错就把错误文本当结论交回去 —— 父 agent 会看到
      "这个子任务没跑成，原因是 X"，然后自己决定要不要换个办法。

    ★ `sub_dir`（2026-10-26）：给了就把这一趟落成一份**自己的事件流**
      （`events.jsonl` + `meta.json` + `result.md`）。
      没有它，父那边只剩一行摘要 —— 而"派了谁、去干了什么"必须能追到底，
      否则并行（第 3 步）一开，人分不清谁在干什么。
    """
    from . import loop as LOOP
    from .policy import Policy

    q = str(prompt or "").strip()
    if not q:
        return {"ok": False, "text": "", "turns": 0, "kind": kind,
                "model": "", "why": "子任务没说要干什么"}
    if provider is None:
        return {"ok": False, "text": "", "turns": 0, "kind": kind,
                "model": "", "why": "没有可用的模型，跑不了子任务"}

    # ★ 类型取不到 ⇒ 把"有的是哪些"一起告诉父（只说不存在，它没法选）。
    try:
        spec = AG.load(kind, Path(repo_root))
    except AG.AgentError as e:
        return {"ok": False, "text": "", "turns": 0, "kind": kind,
                "model": "", "why": str(e)}

    # ★ 会变的数字从配置读（`harness/charter.json` 的 agents 段），不写死在这里。
    _A = SET.agents()
    # ★ 轮数默认**不设上限**（2026-10-28，用户：「时间上和预算上全都先不设限制」）。
    #   0 = 不限（LOOP.run 里 0 就是"跑到它自己说完了为止"）。
    #   要卡就显式配 `agents.max_turns`，别再默认给一个数 —— 默认值会被当成"干完的判据"，
    #   而它其实只是成本闸：子任务被 8 轮砍断，父 agent 只能自己再猜一遍。
    _cfg_turns = _A.get("max_turns")
    turns_cap = int(max_turns if max_turns else (_cfg_turns if _cfg_turns is not None else 0))
    chars_cap = int(_A.get("max_chars") or MAX_CHARS)
    # 递归：depth 是"我现在是第几层子任务"，max_depth 是"允许几层"。
    #   max_depth=0 ⇒ 子 agent 拿不到 task 工具（默认，和以前一样）。
    max_depth = int(_A.get("max_depth") or 0)
    # ★ 派活要**两个条件同时成立**（2026-10-26，对齐 Claude Code 的类型化授权）：
    #   ① 深度没到上限（`max_depth`，全局安全网）
    #   ② 这个类型自己声明了能派谁（`spawn:`，白名单）
    #   只满足①不满足② ⇒ 不派。因为"能派几层"和"能派谁"是两件事：
    #   一个只会查代码的类型，不该有权限去派一个能改文件的类型。
    spawn_kinds = [k for k in (spec.get("spawn") or []) if k]
    can_spawn = depth < max_depth and bool(spawn_kinds)

    # ── 它自己的事件流（可选）──
    sd = Path(sub_dir) if sub_dir is not None else None
    sub_sess = None
    if sd is not None:
        try:
            sd.mkdir(parents=True, exist_ok=True)
            # ★ 刻意**不清理**目录里可能已有的 `stop`：
            #   一个目录就是这一趟（`_run_sub` 每次新建一个号），所以旧标记不会串过来；
            #   而"人在这趟开跑前半秒点了叫停"是可能的，那时清掉就等于无视他的点击。
            _wjson(sd / "meta.json", {"kind": spec["name"], "label": label,
                                      "prompt": q[:2000], "status": "running",
                                      "model": str(getattr(provider, "label", "") or "")})
            from .session import Session
            sub_sess = Session(sd)
        except Exception:
            sub_sess = None                            # 落盘失败不影响跑

    # ── 能动手的类型：先开一间隔离房间（2026-10-26 第 4 步）──
    # ★ root 换成副本 ⇒ 它写的是副本，不是人的工作区。
    #   "权限只降不升"仍然成立：父能写主工作区，子只能写那间房。
    # ★ 顺序要点：这一段必须在 `sd` 之后（副本放在这一趟的目录里），
    #   也必须建 registry 之前（工具要绑副本的 root）。
    base_root = Path(repo_root)
    run_root = base_root
    sb = None
    if spec.get("write"):
        try:
            from . import sandbox as SB
            where = (sd / "ws") if sd is not None else Path(
                tempfile.mkdtemp(prefix="hws-"))
            sb = SB.create(base_root, where)
            run_root = Path(sb["root"])
        except Exception as e:
            return {"ok": False, "text": "", "turns": 0, "kind": spec["name"],
                    "model": "", "why": "开不了隔离副本：%s: %s"
                                        % (type(e).__name__, e)}

    # ★ 只读类型：verify（能核实、不能改）。
    #   能写类型：workspace，但 root 已经是副本 —— 它能写的地方只有那间房。
    sub_pol = Policy("workspace" if spec.get("write") else "verify",
                     root=run_root,
                     rules=getattr(policy, "rules", None)
                     if policy is not None else None)

    def _stop() -> bool:
        """★ 叫停靠一个**文件**，不靠进程内变量：
        子 agent 在父的进程里同步跑，人在界面上点"叫停"走的是另一个进程的 HTTP，
        两边唯一共用的东西只有磁盘。"""
        try:
            return bool(sd is not None and (sd / "stop").exists())
        except Exception:
            return False

    # ★ 子任务用的 provider 要在建 registry **之前**定下来：
    #   `can_spawn` 时要把同一个 provider 递给它，让它也能派活。
    sub_prov = AG.provider_for(spec, provider)

    try:
        reg = _build_registry(
            run_root, spec["tools"],
            no_stage=bool(spec.get("write")),
            # ★ 只有 max_depth 允许时才把 task 递给它。子任务的事件流挂在它自己的
            #   `sub/<n>/sub/` 下，这样嵌套派活的记录也是一棵树，不是散在父的流里。
            sub_provider=(sub_prov if can_spawn else None),
            sub_dir=((sd / "sub") if (can_spawn and sd is not None) else None),
            sub_emit=(sub_sess.emit if (can_spawn and sub_sess is not None) else None),
            spawn_kinds=(spawn_kinds if can_spawn else None),
            depth=depth + 1,
            mem=spec.get("mem") or None,
            facts_scope=spec.get("memory") or "",
            facts_kind=spec.get("name") or "")
    except Exception as e:
        return {"ok": False, "text": "", "turns": 0, "kind": kind,
                "model": "", "why": "子任务的工具集没建起来：%s: %s"
                                    % (type(e).__name__, e)}

    try:
        with _gate():                 # 排队而不是拒绝（见 _gate 上面那段）
            r = LOOP.run(q, sub_prov, reg,
                     # 子任务没有契约判据 —— 判据是"父这一轮要交付什么"，
                     # 不适用于"去查一件事"。恒过。
                         lambda: {"ok": True, "reason": ""},
                         max_turns=turns_cap,
                         policy=sub_pol,
                         system=AG.system_prompt(spec, base_root),
                         session=sub_sess, should_stop=_stop,
                         # ★ 人可以在它跑到一半时补一句（换方向），
                         #   而不是只能叫停重来 —— 重来要重新花一遍 token。
                         inbox=lambda: _drain_steer(sd))
    except Exception as e:
        _wjson(sd / "meta.json", {"kind": spec["name"], "label": label,
                                  "prompt": q[:2000], "status": "crash",
                                  "why": "%s: %s" % (type(e).__name__, e)}) \
            if sd is not None else None
        return {"ok": False, "text": "", "turns": 0, "kind": spec["name"],
                "model": "", "why": "子任务崩了：%s: %s" % (type(e).__name__, e)}

    text = str(r.get("final") or "").strip()
    # ★ 注入中和（2026-10-26）：子任务的结论会被原样交回父 agent，而那段文本里
    #   可能混着"像系统消息"或"像指令"的东西（`<system-reminder>`、bypassPermissions）。
    #   父 agent 分不清"要我做的"和"它转述的" ⇒ 别人的文本就成了我的命令。
    #   ⇒ 原文照旧存 `result.md`（给人看），回灌给父的是 `safe` 版 + 一句发现项。
    #   ★ 必须**紧跟 text 定稿之后**算：下面 meta.json 就要用 `_nz["n"]`。
    #     第一版放到最后去了，结果 meta.json 那行 `UnboundLocalError` —— 验收逮到的。
    from . import neutralize as NZ
    _nz = NZ.neutralize(text)
    if not text:
        # 没给出最终答复（轮数用尽 / stuck）⇒ 把它说过的话尾巴带上，
        # 而不是回一个空字符串。空字符串会让父 agent 以为"查了，没问题"。
        for a in reversed(r.get("actions") or []):
            if a.get("act") == "say" and str(a.get("text") or "").strip():
                text = str(a["text"]).strip()
                break
    cut = False
    if len(text) > chars_cap:
        text = text[:chars_cap]
        cut = True
    ok = r.get("status") == "success"
    why = ""
    if not ok:
        why = "子任务没跑完（%s）%s" % (r.get("status") or "?",
                                       ("：" + str(r.get("why"))) if r.get("why") else "")
    if cut:
        text += "\n…（结论超过 %d 字，后面截掉了）" % chars_cap
    who = str(getattr(sub_prov, "label", "") or "")
    usage = _sum_tokens(sd) if sd is not None else {}

    # ── 改了什么：算 diff、落盘、能删的副本就删掉 ──
    # ★ 副本只在 diff 被截断时保留：完整的 diff 已经够人审，
    #   留着一个几 MB 的副本，几十趟就是几百 MB 的垃圾。
    change = {}
    ws_keep = ""
    if sb is not None:
        try:
            from . import sandbox as SB
            d = SB.diff(base_root, run_root)
            change = d["stat"]
            cut = "截掉了" in (d["text"] or "")
            if sd is not None:
                (sd / "change.diff").write_text(
                    d["text"] or "（这趟没有改任何文件）", encoding="utf-8")
            if cut:
                ws_keep = str(run_root)      # 看不全 ⇒ 副本留着，人能进去看
            else:
                SB.remove(run_root)
        except Exception as e:
            change = {"error": "%s: %s" % (type(e).__name__, e)}

    if sd is not None:
        try:
            (sd / "result.md").write_text(text or "（没有给出结论）", encoding="utf-8")
        except Exception:
            pass
        _wjson(sd / "meta.json", {
            "kind": spec["name"], "label": label,
            "prompt": q[:2000], "status": str(r.get("status") or ""),
            "turns": int(r.get("turns") or 0), "chars": len(text),
            "why": why, "model": who, "flagged": _nz["n"],
            "stopped": _stop(),          # 是"人叫停的"还是"它自己跑完的"，界面要分开说
            "write": bool(spec.get("write")),
            "usage": usage,              # 这趟花了多少（父的账里不混这个）
            "change": change,            # {added, modified, removed, plus, minus}
            "ws": ws_keep,               # 只有 diff 被截断才有值
        })
    if on_event is not None:
        try:
            on_event({"label": label, "kind": spec["name"], "model": who,
                      "status": r.get("status"), "turns": r.get("turns"),
                      "chars": len(text)})
        except Exception:
            pass
    # ★ 子任务被闸拦下 ⇒ 把那条 `needs_human` 一起带上去（2026-10-26）。
    #   不带的话，父 agent 只会看到"子任务没跑成"，然后**重派一次** ——
    #   而重派会撞同一道闸，看起来像"它一直在试同一件被禁止的事"。
    #   带上去之后父这一轮也会停（主循环只看结果里的 needs_human 字段），
    #   人点头、许可落盘，重派才会真的过去。
    blocked = {}
    for _a in reversed(r.get("actions") or []):
        if _a.get("act") == "needs_human":
            blocked = {k: _a.get(k) for k in ("sig", "label", "blocked_by",
                                              "gkind", "tool", "gscope")}
            break
    return {"ok": ok, "text": text, "text_safe": _nz["safe"],
            "flagged": _nz["hits"], "turns": int(r.get("turns") or 0),
            "kind": spec["name"], "model": who, "why": why,
            "change": change, "usage": usage, "blocked": blocked}
