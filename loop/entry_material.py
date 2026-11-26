"""Shared task material. Reading saved text does not activate a role."""
# 主要引用方包括 `legacy_entry.py` 和 `native_entry.py`。
import json
from pathlib import Path
from .provider import Msg

def agreement_context(cdir: str, *, planner_notes=False) -> str:
    """把契约里的 `intent` 和规划者试跑时留的话，拼成一段给**执行者**看的说明。

    ★★ 为什么这件事必须做（2026-10-29 用户问「既然已经能读出来了，
      那是否可以把这个任务给执行去做？」）：
      执行者原来只拿到**判据**（要过哪几条），拿不到 `intent`（这件事要改变什么行为），
      也拿不到规划者试跑时发现的风险。于是它只能照着判据的**字面**去凑 ——
      而判据是压缩过的。"字面凑上了、意图没达成"是这类系统最典型的失败，
      而且它会通过全部判据。

    ★ 但**只给提示，不给权力**：这段话说清"这是规划者说的"，并且明确
      契约本身不能改（要改只能找人）。否则执行者会顺着"要堵这个口子需要改配置"
      去动配置 —— 那是人的事，不是它这一层的。
    """
    try:
        d = Path(cdir)
        if not d.is_dir():
            return ""
        obj = json.loads((d / "done.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    _intent = str(obj.get("intent") or "").strip()
    if not planner_notes:
        return '契约意图：' + _intent[:400] if _intent else ''
    _note = str(obj.get("planner_note") or "").strip()
    if not _intent and not _note:
        return ""
    bits = ["---", "## 规划者留下的（不是任务书，是它试跑时看到的）"]
    if _intent:
        bits.append("**这件事要改变什么行为**：%s" % _intent[:400])
        bits.append("★ 判据只判得动一部分。上面这句才是你要达到的状态 —— "
                    "判据全绿但这句话没做到，就是没做到。")
    if _note:
        bits.append("")
        bits.append("**它试跑时发现的问题**：")
        bits.append(_note[:1500])
    bits.append("")
    bits.append("★ 你能据此调整**做法**，但**契约本身不能改** —— "
                "判据是人锁的，改它要找人。它说的\"需要改什么配置\"也归人，不归你。")
    return "\n".join(bits)


def _interrupt_context(intr: dict) -> str:
    """中断重开时给执行者的那条上下文：已做到哪 + 最后怎么整合。

    ★ 用户的要求（2026-10-31）：中断后**不要出一段孤零零的正文**，
      最终答复要把被中断的部分**重新组织语言接起来**，而不是拼接。
      所以这里明确写"整合成一段完整的话"，并给它被中断前说过的原文。
    """
    said = [str(x) for x in (intr.get("said_tail") or []) if str(x).strip()]
    tools = [str(x) for x in (intr.get("tools_tail") or []) if str(x).strip()]
    lines = ["【这是一次「中断后重开」，不是从零开始】",
             "上一轮被中断前，执行者已经说过（最近的几段，按时间先后）："]
    for i, s in enumerate(said, 1):
        lines.append("%d. %s" % (i, s))
    if tools:
        lines.append("它已经做过的动作（成/败）：" + "、".join(tools))
    lines.append("人刚补的要求是：" + str(intr.get("note") or "").strip())
    lines.append("请你：\n"
                 "- 不要重做已经完成的工作；\n"
                 "- 最终答复时，把「被中断前已完成的部分」和「这次续做的部分」"
                 "**整合成一段完整的话** —— 重新组织语言，"
                 "不要分两段叙述，也不要把两段原文简单拼接。")
    return "\n".join(lines)


def _remember_round_plan(sess_dir, text: str, round_no: int = 0) -> None:
    """把**人这一轮追加的要求**记进 memo，供续跑的测试者对照。

    ★ 为什么不能只留首轮 plan（2026-10-30 千行工程实测）：
      首轮规划者说「只做最小竖切」，人续跑要求「把全部接口补齐」；
      测试者仍读首轮 plan，于是把补齐功能判成「做了计划外的活」。
      续跑不重新跑规划者是对的（否则方向反复漂），但计划**必须换靶**。
    """
    t = str(text or "").strip()
    if not t or sess_dir is None:
        return
    try:
        p = Path(sess_dir) / "memo.json"
        m = {}
        try:
            if p.exists():
                m = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            m = {}
        prev = m.get("round_plans")
        if not isinstance(prev, list):
            prev = []
        entry = {"round": int(round_no or 0), "text": t[:4000]}
        # 连点/重发同一句话不重复记；不同要求按顺序保留最近 10 条。
        if not prev or str(prev[-1].get("text") or "") != entry["text"]:
            prev.append(entry)
        m["round_plans"] = prev[-10:]
        m["round_plan"] = entry
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def resume_requirements(session, task_input, system, fragments, messages):
    if task_input["kind"] != "new":
        from loop.delivery_review import executor_gate
        from loop.workbench import Problem as DeliveryProblem

        try:
            gate = executor_gate(session.dir, task_input)
        except (OSError, ValueError, DeliveryProblem):
            system += "\n续接前请用 inspect_delivery 核对当前任务的必验项。"
            if fragments is not None:
                fragments.add('delivery-refresh', '续接前请用 inspect_delivery 核对当前任务的必验项。', 300)
        else:
            pending = [row for row in gate["requirements"]
                       if row["status"] not in {"passed", "confirmed"}]
            if pending:
                summary = [dict(row, text=row["text"][:240]) for row in pending[:8]]
                messages.append(Msg("user",
                    "续接必验项清单（验收记录；共 " + str(len(pending))
                    + " 项）。needsRebind=true 的项目需要用当前版本重绑原 id。"
                    "查看完整清单和最新目标用 inspect_delivery。\n"
                    + json.dumps(summary, ensure_ascii=False)))
    return system
