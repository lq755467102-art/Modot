#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""循环 —— 想 → 做 → 看，直到**验证器**说完成

核心规则只有一条，其余都是它的附加条款：

    ★ "我做完了"不是终止条件，验证器绿了才是。

四条出口（规格 §6）
    success      验证器绿了
    budget       轮数用完（防死循环）
    stuck        同工具同参数连续太多次（防原地打转）
    needs_human  碰到不可逆工具 / 判据缺失

为什么要自己写而不是套框架
    循环本身几百行；难点在"在哪停"和"谁有权判完成"。
    这两件事我们已经有判据了（`bin/accept.py` 就是现成的验证器）。
"""
from __future__ import annotations

import concurrent.futures as cf
from contextlib import nullcontext
import datetime as dt
import hashlib
import json
import time
import uuid
from pathlib import Path

from .provider import Msg, Interrupted, assistant_tool_calls
from . import compact as CP
from . import context as CTX
from . import hooks as _HOOKS
from . import snapshot as _SNAP
from .policy import Policy
from .workbench import Problem

APPROVAL_STOPS = {"approval_denied", "approval_expired", "approval_revoked", "security_stopped"}
TOOL_PRELUDE_INSTRUCTION = (
    "\n每批工具调用前，在同一条回复的公开正文用一句简短的话说明要核对的对象和目的。"
    "例如「核对构建配置缺少模型名时会怎样处理」，随后发送工具调用。"
    "依据当前已知信息，不提前宣称工具结果，不输出内部推理。"
)


def tool_prelude(text, calls):
    if text and text.strip():
        return text.strip()
    names = {call["name"] for call in calls if isinstance(call.get("name"), str)}
    operations = []
    if names & {"write_project_file", "edit_project_file", "restore_project_file", "undo_own_project_write", "write_file", "apply_patch", "edit_file"}:
        operations.append("处理文件改动")
    if names & {"run_project_check", "run_shell", "shell", "bash"}:
        operations.append("运行命令并核对输出")
    if names & {"inspect_code", "search_project_code", "read_project_resource", "read_file", "search_files"}:
        operations.append("读取相关资料")
    if "ask_human" in names:
        operations.append("请你确认下一步")
    return "接下来会" + ("、".join(operations) or f"执行这批工具操作（{len(calls)} 项）") + "。"


def verification_boundary(calls):
    """Return the version of a check-only batch that cannot advance verification."""
    if not calls or any(name not in {"run_project_check", "read_check_output", "inspect_code", "read_action_evidence"}
                        for name, _ in calls):
        return None
    checks = [result for name, result in calls if name == "run_project_check"]
    if not checks or not all(isinstance(result, dict) and result.get("code") == "verification_closed" for result in checks):
        return None
    return tuple(sorted({r["verification"]["revision"] for r in checks}))


def tool_waits(result):
    return isinstance(result, dict) and (
        result.get("needs_human") or (isinstance(result.get("code"), str)
                                      and result["code"] in APPROVAL_STOPS)
    )


def emit_tool_wait(emit, result, *, role, action_id, tool, **fields):
    why = str(result.get("error") or result.get("question") or "需要用户决定")[:400]
    if result.get("question"):
        emit("ask_human", role=role, action_id=action_id, tool=tool,
             question=result["question"], options=result.get("options"),
             memoryConflictId=result.get("memoryConflictId"))
    emit("needs_human", role=role, action_id=action_id, tool=tool,
         why=why, code=result.get("code"), blocked_by=result.get("blocked_by") or "",
         sig=result.get("grant_sig") or "", gkind=result.get("grant_kind") or "",
         gscope=result.get("grant_scope"), label=result.get("label") or "",
         hint=str(result.get("hint") or "")[:300],
         approvalRefs=[result["grant_sig"]] if result.get("grant_sig") else [],
         **fields)
    return why


def pairing_errors(msgs: list) -> list:
    """检查 tool_calls ↔ tool 的配对是否**连续且完整**。返回问题列表，空 = 没问题。

    为什么要有这个函数
        这条规则靠人眼看不出来，而违反了就是 400，且报错说的是"工具消息不够"，
        听起来像"少了一条结果"，实际往往是"中间被插了一条 user"。
        会插消息的地方有两个：**总纲注入** 和（将来的）压缩/摘要。
        每个动 msgs 的地方，都该在动手之后跑一次这个。

    规则（接口就是这么要求的）
        一条 assistant 带 N 个 tool_calls ⇒ 它**紧接着**必须有 N 条 tool 消息，
        id 一一对应、顺序一致。中间夹任何别的角色都算断。
    """
    errs = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if getattr(m, "role", "") == "assistant" and getattr(m, "tool_calls", None):
            want = [tc.get("id") for tc in m.tool_calls]
            j, got = i + 1, []
            while j < len(msgs) and getattr(msgs[j], "role", "") == "tool":
                got.append(msgs[j].tool_call_id)
                j += 1
            if got != want:
                errs.append("第 %d 条 assistant 要了 %s，紧接着的 tool 是 %s"
                            % (i, want, got))
        i += 1
    return errs


def repair_msgs(msgs: list) -> list:
    """Repair legacy/incomplete tool groups without guessing whether a tool ran."""
    repaired = []
    i = 0
    while i < len(msgs):
        message = msgs[i]
        repaired.append(message)
        i += 1
        if message.role != "assistant" or not message.tool_calls:
            continue
        results = {}
        images = []
        while i < len(msgs):
            item = msgs[i]
            if item.role == "tool":
                results[item.tool_call_id] = item
            elif item.role == "user" and isinstance(item.content, list) and any(
                    isinstance(part, dict) and part.get("type") == "image_url" for part in item.content):
                images.append(item)
            else:
                break
            i += 1
        for call in message.tool_calls:
            cid = call["id"]
            repaired.append(results.get(cid) or Msg("tool", json.dumps({
                "ok": False, "execution_state": "unknown",
                "error": "旧存档缺少结果，执行状态未知。先核对动作证据，禁止直接重做。",
            }, ensure_ascii=False), tool_call_id=cid))
        repaired.extend(images)
    return repaired


def _take_notices(session, limit: int = 4) -> list[str]:
    """取走会话目录里待投的旁路通知。取走即移入 `notices/read/`，同一句不会重复投。

    ★ 为什么用"移动文件"而不是"记一个已读计数"：
      计数是第二份状态。一旦和目录不同步（崩在中间、人手工删了一个），
      要么重复投、要么永远不投 —— 两种都不会报错。移动文件没有这个问题：
      移动是原子的，移动失败就是没投，下次还会投。

    ★ 为什么限 4 条：通知是**打断注意力**的东西，攒一堆等于逼它读第二份任务书。
      多的留在目录里，下一个执行步再投 —— 一条一条来。
    """
    d = Path(getattr(session, "dir", "") or "")
    box = d / "notices"
    if not box.is_dir():
        return []
    out = []
    try:
        done = box / "read"
        done.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    for f in sorted(box.glob("*.json")):
        t = ""
        try:
            t = str(json.loads(f.read_text(encoding="utf-8")).get("text") or "").strip()
        except Exception:
            t = ""
        try:
            f.replace(done / f.name)
        except OSError:
            continue
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def run(task: str, provider, registry, verify, *,
        system: str = "", max_turns: int = 0, max_same: int = 2,
        allow_danger: bool = False, trace_path: Path | None = None,
        session=None, should_stop=None, resume_msgs=None,
        on_turn=None, parallel_tools: bool = True, should_stop_step=None,
        tool_timeout: float | None = None, on_delta=None, on_flush=None, policy=None,
        watch=None, peer=None, inbox=None,
        compact_at: int = 0, compact_cfg: dict | None = None,
        ctx_limit: int = 0, task_message=None, handoff_notice_at=None, role_coordinator=None,
        defer_outcome=False, executor_only=False, execution_core=None, system_fragments=None,
        input_images=()) -> dict:
    """跑一轮。返回结果 dict，不抛异常（除非 provider 自己炸）。

    session（可选）
        落盘载体。给了它，**每条事件立刻写一行**，**每一轮存一次 msgs**。
        不给就退化成"攒到最后写 trace"，和以前一样（366 条验收靠这条保持不动）。

    should_stop（可选）
        每轮开头问一次。返回 True 就进 **paused** 出口 —— 这是第五个出口。
        为什么用回调而不是在这里装信号处理：装信号是平台的事，
        库装了就会干扰调用方（比如 UI 自己也要处理 Ctrl+C）。**库不碰信号。**

    should_stop_step（可选）
        模型调用和工具执行期间问的停止回调。普通调用不传时等于 should_stop；
        「步边界插话」会单独传一个只响应立即暂停的回调，使当前执行步完整结束。

    resume_msgs（可选）
        从存档恢复的消息列表。给了就接着跑，不再塞 task。

    on_turn（可选）
        每轮开头调一次，参数是轮次。用来续租约 / 刷心跳 ——
        不刷的话，一次长时间的模型调用会让别人以为这个会话已经死了。

    on_flush（可选）
        每次模型调用返回后、写 llm/say 定稿前调用。流式调用方用它清空本次增量缓冲，
        保证顺序恒为「增量 → 定稿」，不把上一轮的残片带进下一轮。

    parallel_tools / tool_timeout（可选）
        同一条回复里**标了 readonly 的工具**并发执行，其余串行。
        默认开。关掉（False）只用于两种情况：调试"到底哪一步慢"、
        或者某个调用方自己就是单线程环境不想引入线程。
        ★ 并发的是**只读**工具，写类永远串行 —— 两个写操作同时动一个文件，
          谁都不会报错，而顺序不可复现。这一点不靠自觉，靠 `Tool.readonly` 默认 False。

    compact_at 保留旧调用兼容，不再按估算值触发压缩。
        所有实际压缩统一在 provider 请求边界核对用户/API实际限额。
    """
    from . import review_notes

    audit_enabled = bool(getattr(session, "dir", None))
    from .prompt_fragments import PromptFragments
    fragments = system_fragments.copy() if system_fragments is not None else PromptFragments().add('base', system)
    if audit_enabled and system:
        fragments.add('review-notes', review_notes.INSTRUCTION, 800)
        fragments.add('tool-prelude', TOOL_PRELUDE_INSTRUCTION, 900)
        if registry.get("edit_project_file"):
            fragments.add('editing-and-checks', (
                "\n项目代码修改优先用 edit_project_file，只替换读到的唯一原文片段。"
                "整文件写入不得使用节选或占位符。若误写，先用原 operationId 调用 undo_own_project_write；"
                "系统拒绝自行撤销时保留现场并说明原因。"
                "验证先跑能复现问题的最小检查，再跑直接相关的回归。根据当前版本的 verification 回执收尾，"
                "说明已验证、失败或未覆盖的内容。验收失败或受阻不能声明成果通过；无法解决时可以说明限制并结束本轮运行。"
                "版本查询、源码打印和测试收集只是诊断。定点验证已有结果时，"
                "额外的诊断、测试收集或非必验宽测应在调用前标 purpose=exploration；真实验收使用 acceptance。"
                "已失败的验收不能事后降级成探索。宽测卡在环境或外部依赖时保留阻塞证据，勿循环重复；"
                "实际产品回归仍失败时，集中修复该失败。对照基线请保留当前改动，不在检查命令里 stash。"
            ), 1000)
    system = fragments.render()
    msgs = []
    if resume_msgs:
        msgs = list(resume_msgs)          # 对话历史保留，系统注入采用本次运行快照
        if system:
            msgs = [Msg("system",system,context_parts=fragments.parts())] + [m for m in msgs if m.role != "system"]
    else:
        if system:
            msgs.append(Msg("system", system, context_parts=fragments.parts()))
        msgs.append(task_message if task_message is not None else Msg("user", task))
    # Message images live only in this run's transport, outside the durable checkpoints.
    attached_images = list(input_images)
    image_anchor = next((msg for msg in reversed(msgs) if msg.role == "user"), None) if attached_images else None

    actions: list[dict] = []
    trace = []
    last_sig = None
    same = 0
    status = "budget"
    final = ""
    last_verify = None
    runtime_completion = bool(getattr(role_coordinator, 'runtime_completion', False)
                              or executor_only and role_coordinator is None)

    # ★ 门禁。没传 policy 时**按默认档围住**，不再退回 trusted。
    #   原来这里是 `Policy("trusted")`，理由是"兼容现有用例"——
    #   但那意味着：谁忘了传 policy，谁就拿到了全开权限，而且看不出来。
    #   一个默认值只有两种：要么拦住，要么等于没装门禁。
    #   调用方要全开，请显式写 Policy("trusted") 并说明理由。
    _pol = policy if policy is not None else Policy("workspace")

    # ★ 快照模块要会话目录（存档就放在那儿）。不设 = 不存快照，安静跳过。
    try:
        _SNAP.init(getattr(session, "dir", None) if session is not None else None)
    except Exception:
        pass
    from . import tools as _TOOLS
    _run_id = (session.owned_run_id() if callable(getattr(session,'owned_run_id',None)) else None) or uuid.uuid4().hex
    if getattr(session, "dir", None):
        from . import permissions as _PERMISSIONS
        _PERMISSIONS.freeze(_run_id, Path(session.dir).name)
        # Legacy trusted configuration must not silently bypass the new mode.
        _permission_owner = _TOOLS.set_call(dict(runId=_run_id,sessionId=Path(session.dir).name))
        try:
            if _pol.mode == "trusted" and _PERMISSIONS.effective_sandbox() != "danger-full-access":
                _pol = Policy("workspace",root=_pol.root,rules=_pol.rules,scope=_pol.scope)
        finally:
            _TOOLS.reset_call(_permission_owner)
    _execution_attempt_id=uuid.uuid4().hex

    def rec(kind, **kw):
        kw.setdefault("run_id", _run_id)
        # ★ 有 session 就立刻落盘。没有就只攒内存 —— 保持老行为，验收不受影响。
        if session is not None:
            row = session.emit(kind, **kw)
        else:
            row = {"t": dt.datetime.now().strftime("%H:%M:%S"), "act": kind}
            row.update(kw)
        actions.append(row)
        trace.append(row)
        return row

    from .completion_review import CompletionReview, ReviewLimit, actionable, closing_text
    review = None
    review_reason = None
    if executor_only and execution_core and not role_coordinator:
        try:
            review = CompletionReview.enabled(session, _run_id)
        except (OSError, ValueError, KeyError, TypeError):
            rec("completion_review", status="skipped", reason="ledger_unavailable")
    if review and review.state is not None and not review.active:
        saved = session.load()
        if saved.get('run_id') == _run_id and saved.get('status') in {'success', 'paused', 'budget', 'needs_human', 'error'}:
            return _finish(saved['status'], saved.get('turns', 0), actions, saved.get('final', ''),
                           saved.get('verify'), trace_path, trace, why=saved.get('why') or '')

    def _done(st, turns, why=""):
        """统一出口：先把状态存档，再走原来的 _finish。

        存档放在这里而不是散在各处调用的地方 —— 五个出口漏一个，
        那次中断就会「看起来跑完了，其实没存」，那是最难查的一类。
        """
        nonlocal last_verify, final
        _joined_at_exit = _join_children(cancel_pending=st in {'paused','failed','error','needs_human','stuck'}
                                        or bool(execution_core and st == 'budget'), closing=True)
        if _joined_at_exit and (_joined_at_exit.get('unknown') or st == 'success'):
            st, why = 'needs_human', '子任务刚刚汇总或状态未知；没有继续模型判断或假定完成'
        if st == "success" and ((_stop_step and _stop_step()) or (should_stop and should_stop())):
            st, why = "paused", "收到停止请求，保留已确认的进度和回执"
        settlement = None
        if st == 'success' and review and review.active and not review.state.get('publication'):
            settlement = review.wait_for_checks(getattr(session, 'task_input', None))
            if settlement:
                rec('completion_review_wait', **settlement)
                if (settlement['reason'] == 'stopped' or (_stop_step and _stop_step())
                        or (should_stop and should_stop())):
                    st, why = 'paused', '收到停止请求，保留复核回执'
            if st == 'success':
                from . import corrections

                if corrections.deliver(session, msgs, _run_id, turns):
                    st, why = 'paused', '收到用户纠正，已写入会话，待继续处理'
                    final = ''
        if executor_only and session is not None and st in {"success", "paused"}:
            from .code_service import executor_verification

            try:
                prior_runs = review.prior_run_ids() if review and review.state else ()
                summary = executor_verification(Path(session.dir).name, _run_id,
                                                task=getattr(session, 'task_input', None),
                                                prior_run_ids=prior_runs)
            except Exception as error:
                summary = dict(status="unknown", resources=[])
                rec("verification_unavailable", reason=str(error)[:240])
            if summary:
                if execution_core and last_verify is not None:
                    # Keep the original criterion distinct from observational
                    # check coverage; unverified probes are not failed criteria.
                    last_verify = dict(last_verify, verifierOk=bool(last_verify.get('ok')))
                last_verify = dict(last_verify or {}, codeChecks=summary)
                if summary["status"] != "checked":
                    last_verify["ok"] = False
                    if st == "success" and not (runtime_completion or getattr(role_coordinator, "serial_review", False)):
                        st, why = "paused", "当前交付版本检查尚未通过或完成"
                    if not execution_core:
                        note = "当前交付版本仍有未通过、未运行或未确认的检查。请以对应版本的回执为准。"
                        gaps=[gap.get('text','') for row in summary.get('resources',[]) for gap in row.get('pending',[])]
                        if gaps:
                            note=(note+'\n当前版本尚未验收：'+'；'.join(dict.fromkeys(gaps))[:500]).strip()
                        rec("say", role="executor", text=note, attempt_id=_execution_attempt_id)
                        final = (final + "\n\n" + note).strip()
                        msgs.append(Msg("assistant", content=note))
                if summary["status"] == "unknown" or any(row["status"] == "unknown" for row in summary["resources"]):
                    st, why = "paused", "检查或工作区版本尚未核实，保留进度"
                rec("executor_verification", **summary)
                if role_coordinator:
                    role_coordinator.state["executorChecks"] = summary
            if st == "success" and (task_input := getattr(session, "task_input", None)):
                from .delivery_review import executor_gate

                try:
                    gate = executor_gate(session.dir, task_input)
                except Exception as error:
                    gate = dict(ok=False, requirements=[])
                    rec("verification_unavailable", reason=str(error)[:240])
                if gate["requirements"] or not gate["ok"]:
                    last_verify = dict(last_verify or {}, acceptance=gate)
                    if gate["ok"] and last_verify.get("ok"):
                        last_verify["reason"] = "当前交付版本的已登记必验项均有证据"
                    rec("executor_acceptance", **gate)
                if not gate["ok"]:
                    last_verify["ok"] = False
                    gaps = [row["text"][:120] for row in gate["requirements"]
                            if row["status"] not in {"passed", "confirmed"}]
                    why = "必验项尚无当前版本证据" + ("：" + "、".join(gaps[:5]) if gaps else "")
                    last_verify["reason"] = why
                    if not (runtime_completion or getattr(role_coordinator, "serial_review", False)):
                        st = "paused"
                    if not review_reason:
                        rec("say", role="executor", text=why, attempt_id=_execution_attempt_id)
                        final = (final + "\n\n" + why).strip()
                        msgs.append(Msg("assistant", content=why))
                if role_coordinator:
                    role_coordinator.state["executorAcceptance"] = gate
        if st == 'success' and review and review_reason:
            reviewed = review.state is not None
            task_input = getattr(session, 'task_input', None)
            unmet = ((last_verify or {}).get('acceptance') or {}).get('ok') is False
            changed = reviewed and review.candidate_unverified(last_verify, task_input)
            unconfirmed = settlement and settlement['reason'] in {'deadline', 'unconfirmed'}
            if unmet or changed or unconfirmed:
                final = closing_text(last_verify, settlement, reviewed=reviewed)
            elif reviewed and (review_reason != 'final_reply' or not final):
                draft = review.draft_text(last_verify, task_input)
                final = '\n\n'.join(part for part in (closing_text(last_verify, settlement), draft) if part)
            elif settlement:
                if review.contradicted(settlement, msgs):
                    final = review.draft_text(last_verify, task_input)
                final = closing_text(last_verify, settlement) + '\n\n' + final

            def say(text, **fields):
                return rec('say', role='executor', turn=turns, text=text[:20000], roleSummary=True,
                           truncated=len(text) > 20000, full_len=len(text), attempt_id=_execution_attempt_id, **fields)

            if reviewed:
                final = review.publish(final, review_reason, lambda text: say(text, completionReview=review.review_id),
                                       refresh_unsent=unmet or changed)
            elif final.strip():
                say(final.strip(), reviewNotes=audit_notes)
            if final:
                msgs.append(Msg('assistant', content=final))
        if review and review.active:
            try:
                reason = 'paused' if st == 'paused' else review_reason or st
                rec("completion_review", **review.finish(reason, time.time()),
                    verification=last_verify)
            except (OSError, ValueError, KeyError):
                rec("completion_review", status="unconfirmed", reason="ledger_unavailable")
        rec('role_phase',attempt_id=_execution_attempt_id,role='executor',state={'success':'completed','paused':'paused','needs_human':'waiting_human'}.get(st,'failed'),reason=why or st)
        if session is not None:
            question = next((a for a in reversed(actions) if a.get("act") == "ask_human"), {})
            try:
                # ★ 判据校验的结果也要落盘（2026-10-28）：只留在返回值里，
                #   人翻 session.json 时看到的是 `verify: null` —— 那会被读成
                #   "这一轮没验"，而它其实验了、只是没说。
                session.save(run_id=_run_id,status="running" if role_coordinator or defer_outcome else st, turns=turns, why=why, final=final,
                             public_reply_persisted=True,
                             waitQuestion=st == "needs_human" and bool(question),
                             waitConflictId=question.get("memoryConflictId") if st == "needs_human" else None,
                             verify=last_verify,
                             task=task, msgs=[m.dump() for m in msgs])
                if role_coordinator:
                    role_coordinator.executor_done(st, turns, why, final)
                if execution_core:
                    execution_core.finish(st, last_verify, why)
                if getattr(session,'worker_contract',False) and not (role_coordinator or defer_outcome):
                    from .worker_plugins import parent_finished
                    parent_finished(session,st)
                # ★ 结束这件事本身也要是一条事件。不记的话，"事件流里看不到结束"
                #   ⇒ 界面只能靠"再没有新行了"去猜它停了，而忙和停看起来一样。
                session.emit("role_attempt_end" if role_coordinator or defer_outcome else "end", run_id=_run_id,
                             status=st, turns=turns, why=why or "",
                             **({"role": "executor"} if role_coordinator or defer_outcome else {}))
            except Exception as e:                      # 存档失败不要带崩这一轮
                rec("session_save_failed", error="%s: %s" % (type(e).__name__, e))

        # ── Stop 钩子（2026-10-26）──
        # ★ 挂在**这里**，和存档同一个理由：五个出口都走 _done，散在各出口写就会漏。
        #   漏掉的后果不是"钩子没跑"，而是"钩子有时候跑有时候不跑" ——
        #   那种不一致比完全没有更难查（上次 review.py 从没被调用就是这样）。
        # ★ 放在 session 判断**外面**：没有会话也该收尾（--dry-run 之类）。
        #   HARNESS_ROOT 环境变量给项目根，所以不依赖 session。
        # ★ needs_human 也算一次 Stop：进程真的退出了，外部该知道。
        #   hook 自己可以看 status 决定要不要做事。
        try:
            for _h in ([] if role_coordinator or defer_outcome else _HOOKS.run("Stop", {
                    "event": "Stop", "status": st, "turns": turns,
                    "why": (why or "")[:400],
                    "session": str(getattr(session, "dir", "") or ""),
                }, Path.cwd())):
                rec("hook", **_h, note=_HOOKS.brief(_h))
        except Exception:
            pass        # 钩子出问题不能把已经跑完的这一轮带崩

        return _finish(st, turns, actions, final, last_verify, trace_path, trace, why=why)

    def _join_children(cancel_pending=False, *, wait=True, closing=False, native_only=False, before_delivery=None):
        from . import subtasks, evidence
        if not native_only and getattr(session,'worker_contract',False):
            from .worker_plugins import updates
            updates(session,msgs,closing=closing or cancel_pending,stopping=cancel_pending)
        def accept(result):
            items = [item for item in result["tasks"] if item["state"] != "unknown"]
            if not items:
                return True
            new_messages = [subtasks.result_message(result, item) for item in items]
            if before_delivery is not None:
                new_messages.insert(0, before_delivery)
            try:
                saved = session.load()
                accepted = set(saved.get("nativeSubtaskDeliveries", ()))
                accepted.update(item["deliveryId"] for item in items)
                session.save(run_id=_run_id, nativeSubtaskDeliveries=sorted(accepted),
                             msgs=[m.dump() for m in [*msgs, *new_messages]])
            except (OSError, ValueError, RuntimeError) as error:
                rec("session_save_failed", error=str(error)[:240])
                return False
            msgs.extend(new_messages)
            return True

        timeout = max(0, review.time_left()) if review and review.active else None
        result = subtasks.join(session, should_stop, cancel_pending, timeout=timeout, wait=wait,
                               wait_unscoped=bool(role_coordinator), accept=accept)
        if result:
            action = uuid.uuid4().hex
            rec('action', action_id=action, tool='harness_join_subtasks', actor='harness', role='executor', state='running', args={}, startedAt=time.time())
            observed = evidence.capture(session, action, result)
            rec('action', action_id=action, tool='harness_join_subtasks', actor='harness', role='executor', state='unknown' if result['unknown'] else 'completed', finishedAt=time.time(), outputEvidence=observed)
        return result

    def _checkpoint(turn: int) -> bool:
        """每轮开头存一次 msgs。崩在任意一步，最多丢一轮。"""
        if session is None:
            return False
        try:
            session.save(run_id=_run_id,status="running", turns=turn, task=task,
                         msgs=[m.dump() for m in msgs])
            return True
        except Exception as e:
            rec("session_save_failed", error="%s: %s" % (type(e).__name__, e))
            return False

    # 判据喂回去的次数。到 1 就不再拦 —— 判据是提醒，不是关卡（见下面那个出口）。

    # ★★ 轮数上限：`<= 0` 表示**不限**（2026-10-24 用户拍板）
    #   原话：「轮数上限这种限制是不应该加的……一个是不卡时限，尽量提供资源，
    #         一个是让 ai 自己确认自己是否都做到了。」
    #   轮数上限本来就是"用数字代替判断"，它不知道活干完没有，只知道跑了多少次。
    #   现在停下来的理由变成三个，都是行为判据，不是数字：
    #     ① 模型给出最终答复（且二次询问后仍这么说）
    #     ② stuck：同工具同参数连续重复（真正卡住的唯一可靠信号）
    #     ③ 人叫停（control.json）
    #   ★ 代价要说清：nag 只拦"原地打转"，拦不住"每次换个花样试"。
    #     那种情况只能靠人看事件流 —— 所以事件流必须好用（这也是为什么在改渲染）。
    _limit = max_turns if max_turns > 0 else 10 ** 9

    # ★★ 把 should_stop 挂到工具层 —— 这是「暂停要等很久」的根因修复点。
    #   原来停止只在**模型调用**期间被检查（provider 每 0.1 秒看一次），
    #   而工具执行是 `subprocess.run(timeout=120)` 整条阻塞：
    #   一条 `python bin/accept.py`（实测 130 秒）跑起来之后，control.json 没人看，
    #   人按了暂停要等它跑完 —— 观感就是"我点了暂停，很久之后才停"。
    #   现在 run_check 内部每 0.1 秒看一次这个钩子，收到就杀进程树。
    #   摘钩子放在 finally：进程里可能跑第二个 agent，摘晚了会把别人的停掉。
    # ★★ 停止钩子不再挂到模块级全局（2026-10-27 改）。
    #   原来的写法是"进来时给 `_TOOLS.STOP` 赋值、finally 再还原"，单任务时看不出问题；
    #   两个任务同时跑就会：A 读到 B 的钩子、A 先结束把全局还原成 None
    #   ⇒ **B 从此停不下来**。谁先结束决定结果，不是谁该停。
    #   现在改成"执行工具的那个线程自己挂"（见下面 _exec 的 set_stop），
    #   并发天然隔离 —— 线程池里的每个 worker 只认自己这一次调用。
    from . import tools as _TOOLS

    # 轮次边界与执行步内部是两种停止语义。默认仍完全相同；只有步边界插话
    # 会分别传值，避免插话把当前模型调用或工具切断。
    _stop_step = should_stop_step if should_stop_step is not None else should_stop
    import threading as _threading
    _tool_cancel = _threading.Event()
    closed_verification = None
    completion_reminders = 0
    repairing = False
    if role_coordinator and role_coordinator.handoff_deadline is not None:
        reserved_notice = role_coordinator.handoff_deadline - role_coordinator.handoff_check_lead
        handoff_notice_at = min(reserved_notice, handoff_notice_at) if handoff_notice_at is not None else reserved_notice

    def handoff_due(lead=0):
        return bool(role_coordinator and role_coordinator.handoff_due(repairing=repairing, lead=lead))

    def stop_model():
        return bool((_stop_step and _stop_step()) or handoff_due() or review and review.timed_out())

    def finish_review(turn, reason):
        nonlocal final, review_reason
        if (_stop_step and _stop_step()) or (should_stop and should_stop()):
            return _done("paused", turn, why="收到停止请求，保留复核回执")
        review_reason = reason
        final = ""
        return _done("success", turn)

    def prepare_final_review(turn, draft):
        if review is None or review.state is not None:
            return False
        from .code_service import executor_verification
        from .delivery_review import executor_gate

        try:
            task_input = getattr(session, 'task_input', None)
            checks = executor_verification(Path(session.dir).name, _run_id, task=task_input,
                                           include_review_receipts=True)
            acceptance = executor_gate(session.dir, task_input) if task_input else {}
            reason = review.start(actionable(checks, acceptance), _limit - turn,
                                  draft=draft, checks=checks, acceptance=acceptance, task=task_input)
        except (OSError, ValueError, Problem):
            reason = 'evidence_unavailable'
        if reason == 'started':
            session.save(completionReviewId=review.review_id)
            rec("completion_review", **review.event_fields())
            review.feedback(msgs)
            return True
        rec("completion_review", status="skipped", reason=reason)
        return False

    def forward_delta(kind, value):
        if review is None or kind != 'text':
            on_delta(kind, value)

    def pause_or_handoff(turn, why):
        closing = bool(role_coordinator and role_coordinator.work_expired())
        if closing or handoff_due():
            intent = next((m.content for m in reversed(msgs)
                           if m.role == "assistant" and isinstance(m.content, str) and m.content.strip()), "")
            _checkpoint(turn)
            rec("review_budget_handoff", role="executor", turn=turn, closing=closing)
            role_coordinator.publish_checkpoint(intent or "复核最后确认的改动。", closing=closing)
            if not closing and not (should_stop and should_stop()):
                _tool_cancel.clear()
                msgs.append(Msg("user", "已到保底交接点。测试者可复核已封存的候选，你仍可继续实现。"
                                         "收到已复现的发现时核对并修正，交付时由测试者验收当前版本。"))
                return None
        return _done("paused", turn, why=why)

    rec('role_phase',attempt_id=_execution_attempt_id,role='executor',state='running',reason='执行循环已开始')
    review_boundary = False
    _run_permission_token = _TOOLS.set_call(dict(runId=_run_id,sessionId=Path(session.dir).name,
        tool="__run__",cwd=str(_pol.root))) if getattr(session,"dir",None) else None
    try:
        if review and review.state and (publication := review.state.get('publication')):
            final, review_reason = publication['text'], publication['reason']
            return _done('success', session.load().get('turns', 0))
        if review and review.state is not None and not review.active:
            return finish_review(0, 'already_used')
        if review and review.active:
            session.save(completionReviewId=review.review_id)
            review.feedback(msgs)
            rec("completion_review", status="resumed", reviewId=review.review_id, modelRequests=review.state["modelRequests"],
                checkRequests=review.state["checkRequests"], expiresAt=review.state["expiresAt"])
        if role_coordinator and (ready := role_coordinator.state.get("deliveryReady")):
            final = ready["final"]
            decision, reason = role_coordinator.delivery_gate(final, ready["turns"], partial=ready.get("partial", False))
            if decision == "pass":
                return _done("success", ready["turns"], why=reason or None)
            if decision != "repair":
                return _done("paused", ready["turns"], why=reason)
            repairing = True
            review_boundary = True
        for turn in range(1, _limit + 1):
            _children = _join_children(wait=not (execution_core or role_coordinator))
            if _children and _children.get('unknown'):
                return _done('needs_human', turn-1, why='子任务状态无法确认，不继续或重放')
            # ★ 第五个出口：paused。放在**每轮开头**问，因为它最便宜 ——
            #   已经发出去的模型调用收不回来，但还没发的那次可以省掉。
            if handoff_due() or should_stop is not None and should_stop():
                paused = pause_or_handoff(turn - 1, "收到停止请求；状态已存档，可以 --resume 接着跑")
                if paused is not None:
                    return paused
            if review and review.active:
                if review.timed_out():
                    return finish_review(turn - 1, 'deadline')
                if review.state['modelRequests'] >= review.state['requestLimit']:
                    return finish_review(turn - 1, 'model_budget')

            # ★★ 旁路观察（2026-11-01）：不是人说话，也不是复核，是"有人看着你"。
            #   和 peer 的区别：peer 是旁边那个模型在复核**这句话**；notice 是**程序**
            #   记下来的跨委托事实（典型：同一个文件另有委托在改）。
            #   为什么措辞要这么绕：它既不是指令也不是建议 —— 给成指令会被当命令执行、
            #   跑偏；给成建议会被当参考、忽略。所以写成"事实 + 你自己判断"。
            if session is not None:
                try:
                    _nts = _take_notices(session)
                except Exception as _e:
                    _nts = []
                    rec("notice_failed", error="%s: %s" % (type(_e).__name__, _e))
                if _nts:
                    _ntext = "\n".join("- " + str(x) for x in _nts)[:800]
                    msgs.append(Msg("user",
                        "（旁路观察 —— 不是人说的话，是程序记下来的事实：\n%s\n"
                        "它不改变你的任务和授权。如果它涉及你正要改的东西，先读一遍"
                        "磁盘上现在是什么再动手；如果和你无关，说一句无关就继续。）" % _ntext))
                    rec("notice", count=len(_nts), text=_ntext[:400],
                        note="旁路观察已进上下文（这个执行步就会带上）")
            # ★ 上下文压缩：放在**每轮开头**，因为这是唯一还没花钱的时刻。
            from . import corrections as _CORRECTIONS
            _CORRECTIONS.deliver(session, msgs, _run_id, turn)
            if handoff_notice_at is not None and time.monotonic() >= handoff_notice_at:
                handoff_notice_at = None
                rec("handoff_notice", role="executor", turn=turn)
                msgs.append(Msg("user",
                    "Harness 运行提醒：规划与执行窗口即将结束。根据当前版本的回执尽快交接。"
                    "核心改动形成可验收候选后，请直接提交结果和未验证项，由测试者独立复核。"
                    "若仍有具体产品失败，优先修复它。不要再跑宽测、版本查询或收集测试来延长诊断。"
                    "未完成就明确写出当前改动、已知失败和剩余工作，不要宣称通过。"))
            # Legacy byte-estimate compaction is intentionally retired. The
            # provider boundary is the only owner of actual-limit compaction.
            if execution_core:
                execution_core.feedback(msgs)
                from .runtime_facts import refresh
                refresh(execution_core,msgs)
                from .workspace_instructions import refresh as refresh_instructions
                refresh_instructions(session,msgs)
            review_ids = role_coordinator.feedback(msgs, validation=review_boundary) if role_coordinator else []
            if _checkpoint(turn) and review_ids:
                role_coordinator.acknowledge(review_ids)
            review_boundary = False
            if on_turn is not None:
                try:
                    on_turn(turn)
                except Exception:
                    pass          # 心跳失败不该带崩这一轮

            # ★★ 测试者（并行的那个模型）攒下的批注，投给**执行者**。
            #   为什么必须投进去：测试者只看不说的话，它就只是个旁白 ——
            #   执行者下一轮开口时并不知道"刚才有人在旁边指出你这句对不上"。
            #   反驳要构成反驳，就得回到它的上下文里，让它解释或修正。
            #   ★ 取走即清空（peer 那边 drain）：同一句提醒每轮重复，只会变成背景噪音。
            #   ★ 用 user 角色而不是 system：这是"有人在对话里插话"，
            #     不是规则变更。规则的入口是 charter，两者不能混。
            if peer is not None:
                try:
                    _pnotes = peer() or []
                except Exception as _e:
                    _pnotes = []
                    rec("peer_failed", error="%s: %s" % (type(_e).__name__, _e))
                if _pnotes:
                    _ptext = "；".join(str(x) for x in _pnotes)[:1200]
                    msgs.append(Msg("user",
                        "（旁边有人在实时复核你刚才的话，指出：%s\n"
                        "如果它对，修正你的做法或说法；如果它不对，说明理由 —— "
                        "但不要用「这不重要」把它放过。）" % _ptext))
                    rec("peer", count=len(_pnotes), text=_ptext[:400],
                        note="测试者的批注已经投给执行者（它下一句话会考虑这件事）")

            # ★★ 人中途插话（2026-10-26，steer）。
            #   和 peer 的区别：peer 是"旁边有人复核你刚才的话"，steer 是"人要你换个方向"。
            #   两者都是往上下文里插一条 user 消息，但**措辞必须不同**：
            #   把复核当成指令去执行会跑偏；把指令当成建议去参考会忽略。
            #   同样取走即清空 —— 同一句重复十遍只会变成背景噪音。
            if inbox is not None:
                try:
                    _inotes = inbox() or []
                except Exception as _e:
                    _inotes = []
                    rec("steer_failed", error="%s: %s" % (type(_e).__name__, _e))
                if _inotes:
                    _itext = "\n".join(str(x) for x in _inotes)[:1200]
                    msgs.append(Msg("user",
                        "（人在中途补了一句，**优先级高于你原来的计划**：\n%s\n"
                        "按这个调整。如果它和你已经做过的事冲突，就明说冲突在哪、"
                        "你打算怎么收，别悄悄改。）" % _itext))
                    rec("steer", text=_itext[:400],
                        note="人的插话已经进上下文（下一次开口就会看到）")

            if getattr(session, "dir", None) and any(getattr(message, "image_action", None) for message in msgs):
                from .image_evidence import drop_revoked_images

                drop_revoked_images(Path(session.dir).name, msgs)
            if session is not None:
                from .working_context import append_updates
                append_updates(session, 'executor', msgs)
            from .subtasks import result_messages_for_request
            _call_msgs = result_messages_for_request(session, msgs)
            if attached_images:
                from .image_evidence import drop_revoked_images

                drop_revoked_images(Path(session.dir).name, attached_images)
                position = next((index + 1 for index, msg in enumerate(_call_msgs) if msg is image_anchor), len(_call_msgs))
                _call_msgs = [*_call_msgs[:position], *attached_images, *_call_msgs[position:]]
            # ★ 把 should_stop **当参数**交给 provider，让它别把主线程闷在 socket 里。
            #   实测：不交的话，信号到达后要等这次 HTTP 读完才被处理，延迟 4.13 秒
            #   （= 卡住的剩余时间，最坏到 timeout=300 秒）⇒ "能暂停"只是"能等"。
            #   为什么不做成 provider 的属性：实测同一进程里跑父子两个 agent 时，
            #   后开的那条循环会把前一条的回调覆盖掉 —— 停一个可能把另一个也停了。
            # ★ 发送前把"这一份到底是哪些东西组成的"记下来（2026-10-27 用户要的
            #   上下文面板）。为什么在这里量而不是面板打开时现算：现算就是第二套口径
            #   —— 注入了没有、工具死了几个、MCP 连上没有，都要重新判断一遍，
            #   只要有一处判断不同，人看到的就是一个**看起来很像**的数字。
            #   ★ schema 也只算这一次，三次调用共用：省一次序列化，而且保证
            #     "量到的"和"发出去的"是同一份。
            _schemas = registry.schemas()
            if review and review.active:
                _schemas = review.schemas(_schemas)
            try:
                # Context size follows the actual dialogue and tool schemas.
                CTX.write(getattr(session, "dir", None), _call_msgs if attached_images else msgs, _schemas,
                          cap_tokens=(ctx_limit or compact_at or 0), turn=turn)
            except Exception:
                # 面板没数据是可以接受的；让这一轮任务因此失败不行。
                pass
            public_stream = review_notes.PublicStream(forward_delta) if audit_enabled and on_delta else None
            try:
                # ★ 流式只在「调用方给了 on_delta」且「provider 声明支持」时才走。
                #   两个条件缺一个就退回整轮 —— 不能因为加了新能力就把原路改掉，
                #   原路是 505 条验收踩着的那条。
                with review.model_call() if review and review.active else nullcontext():
                    if on_delta is not None and getattr(provider, "supports_stream", False):
                        reply = provider.chat_stream(_call_msgs, _schemas, should_stop=stop_model,
                                                     on_delta=public_stream.add if public_stream else forward_delta)
                    else:
                        reply = provider.chat(_call_msgs, _schemas, should_stop=stop_model)
            except TypeError as _e:
                # 自定义 provider 可能是老签名。别把整个任务崩掉，退回不带回调那种调法，
                # 但**要说出来** —— 静默降级等于悄悄丢掉"能停"这个能力。
                if "should_stop" not in str(_e):
                    raise
                if review and review.active:
                    return finish_review(turn, 'provider_no_stop')
                rec("provider_no_stop", note="这个 provider 的 chat() 不收 should_stop，"
                                            "本次调用期间无法中断")
                reply = provider.chat(_call_msgs, _schemas)
            except ReviewLimit as error:
                return finish_review(turn, str(error))
            except Interrupted:
                # 这次调用的钱已经花掉了（请求发出去过），但人不用再等。
                # 记账上没有 usage（没读到响应），所以不要假装这一轮零成本。
                rec("interrupted", turn=turn,
                    note="模型调用期间被要求停止；这一次的 token 已经花掉")
                if review and review.timed_out():
                    return finish_review(turn, 'deadline')
                paused = pause_or_handoff(turn - 1, "模型调用期间收到停止请求")
                if paused is not None:
                    return paused
                continue
            if public_stream:
                public_stream.flush()
            if any(getattr(message, "image_action", None) for message in _call_msgs):
                from .image_evidence import confirm_model_images

                confirm_model_images(rec, _call_msgs, [*msgs, *attached_images], "executor",
                                     input_images=getattr(reply, "input_image_actions", None))
            audit_notes = []
            if audit_enabled:
                reply.text, audit_notes = review_notes.split(reply.text)
            from .context_manager import adopt as adopt_context
            adopt_context(session, 'executor', _call_msgs)
            # ★ 每次模型调用定稿之前，先把本次尚未发出的增量冲出去。
            #   否则最后一截会拖到下一轮：llm/say 已经定稿，旧 chunk 随后又贴回来，
            #   界面上就出现「执行者…」的重复残片，看起来像结束后才补出思考流。
            if on_flush is not None:
                try:
                    on_flush()
                except Exception:
                    pass          # 展示缓冲失败不能推翻已经拿到的模型回复
            # ★ 把接口用量记进轨迹。
            #   理由：业务方问"执行者在这个环节是否合法、额度为什么动得少"——
            #   如果没有 usage，"有没有真调模型"就只能靠我口头保证。
            #   记下来之后，这一条就是可查的事实。
            _u = getattr(reply, "usage", None) or {}
            _rs = getattr(reply, "reasoning", "") or ""
            if _u or _rs:
                rec("llm", turn=turn, model=getattr(provider, "model", ""),
                    prompt_tokens=_u.get("prompt_tokens"),
                    completion_tokens=_u.get("completion_tokens"),
                    total_tokens=_u.get("total_tokens"),
                    prompt_cache_hit_tokens=_u.get("prompt_cache_hit_tokens"),
                    prompt_cache_miss_tokens=_u.get("prompt_cache_miss_tokens"),
                    # ★ 思考内容进事件流，界面才能「可见但折叠」（对齐 Codex 的 reasoning summary）。
                    #   截断到 3000 字：完整 reasoning 可能上万字，写进 events 会让文件爆掉，
                    #   而 Codex 显示的本来也是摘要不是全文。超了不静默——用 more 标出来。
                    reasoning=_rs[:3000],
                    reasoning_more=len(_rs) > 3000,
                    reasoning_chars=len(_rs))

            if runtime_completion and getattr(reply, 'finish_reason', '') in {'length', 'max_tokens'}:
                if review and review.active:
                    return finish_review(turn, 'model_output_limit')
                final = reply.text or ''
                msgs.append(Msg('assistant', content=final))
                rec('model_output_truncated', role='executor', text=final[:20000],
                    discardedToolCalls=len(reply.tool_calls or []))
                return _done('budget', turn, why='模型输出被截断，未执行本次工具请求；保留已有内容')

            # ── 要调工具 ──────────────────────────────
            if reply.wants_tool:
                # ★ 模型调工具时说的话也要记 —— 否则事后只能看到"调了什么"，
                #   看不到它"为什么调"，而后者才是测试者要审的东西。
                if (reply.text or "").strip() or audit_enabled:
                    _say_txt = tool_prelude(reply.text, reply.tool_calls)
                    # ★ 不再截到 500 字。原来流式时能看到全文、事件里只有前 500 字，
                    #   于是"它说完了"和"我看到的"对不上 —— 人分不清是模型输出坏了
                    #   还是我们截的。要截就截得足够大，并且**明确标出来**。
                    rec("say", role='executor',turn=turn,text=_say_txt[:20000], toolPrelude=True,
                        toolPreludeFallback=not bool((reply.text or "").strip()),
                        truncated=len(_say_txt) > 20000, full_len=len(_say_txt))
                # ★★ 一条回复只能对应**一个** assistant 消息（带全部 tool_calls）。
                #    之前写成"先追加文字、再追加工具调用" ⇒ 连着两个 assistant，
                #    第一个没带 reasoning_content ⇒ DeepSeek 400：
                #    「The reasoning_content in the thinking mode must be passed back」。
                #    报错信息看起来像"少回传了思考"，实际是"消息被拆成了两条"。
                # ★ 转换抽到 provider 里了（2026-10-29）：`plan.py` 自己拼过一遍，
                #   漏了 `type: "function"` 和 `function` 那层包装 ⇒ 422
                #   「messages[2]: missing field 'type'」。那个报错指不到真正的原因。
                msgs.append(Msg("assistant", content=reply.text, reasoning=reply.reasoning,
                                tool_calls=assistant_tool_calls(reply.tool_calls, turn)))

                # ★★ 先分类，再执行（2026-10-23 修）。
                #   原来是在 for 里逐个判：撞上第一个需要人点头的就 return，
                #   于是**同一条回复里剩下的调用静默消失** —— 实测：一条回复要 2 个工具，
                #   第 1 个撞 consumed 档，第 2 个（只读、完全安全）一次都没被执行，
                #   轨迹里也一个字都没有。
                #   危害不是"少做了一件事"，是**状态对不上**：assistant 消息里带着 N 个
                #   tool_call，结果里 0 个。现在这一轮就结束所以不显，一做 resume 就是坏档。
                #   ⇒ 全部预检；有任何一条要点头，就整条回复都不执行（保持干净），
                #     但**每一个都记进轨迹**，人能看到"它这一轮其实还想做几件事"。
                def record_unstarted():
                    for call in msgs[-1].tool_calls[:]:
                        msgs.append(Msg("tool", json.dumps({"ok": False,
                            "execution_state": "cancelled_before_start"}), tool_call_id=call["id"]))

                _blocked = []
                _call_actions = {id(call):uuid.uuid4().hex for call in reply.tool_calls}
                _b_sig = {}
                for _c in reply.tool_calls:
                    _t = registry.get(_c.get("name"))
                    if _t is None or not _t.danger or _c.get("name")=="ask_human":
                        continue
                    if getattr(session, "dir", None):
                        from . import permissions as _AP
                        _owner = _TOOLS.set_call(dict(runId=_run_id, sessionId=Path(session.dir).name,
                            actionId=_call_actions[id(_c)], tool=_c.get("name")))
                        try:
                            _AP.authorize(dict(kind="tool", tool=_c.get("name"), arguments=_c.get("args") or {},
                                cwd=str(_pol.root)), human_only=_t.zone=="consumed")
                        except Problem:
                            record_unstarted()
                            raise
                        finally:
                            _TOOLS.reset_call(_owner)
                        continue
                    if allow_danger:
                        continue
                    # ★★ 人批过的**这一次**就放行（2026-10-26）。不认许可的话，
                    #   MCP 这类"每次都要点头"的工具就是每调一次停一轮 ——
                    #   人烦到最后会把整条关掉，那比"没这道闸"更糟。
                    _sg = ""
                    try:
                        if getattr(_t, "sig_of", None):
                            _sg = _t.sig_of(_c.get("args") or {}) or ""
                    except Exception:
                        _sg = ""
                    if _sg:
                        try:
                            from . import grants as _G0
                            if _G0.take(_sg) is not None:
                                rec("grant_used", action_id=_call_actions[id(_c)], role="executor", tool=_c.get("name"), sig=_sg, approvalRefs=[_sg])
                                continue
                        except Exception:
                            pass
                    _blocked.append(_c)
                    _b_sig[id(_c)] = _sg
                if _blocked:
                    for _c in reply.tool_calls:
                        _t = registry.get(_c.get("name"))
                        _zone = getattr(_t, "zone", None) if _t is not None else None
                        _needs = _t is not None and _t.danger and not allow_danger
                        rec("not_run", action_id=_call_actions[id(_c)], role="executor", state="waiting_approval" if _needs else "cancelled_before_start", approvalRefs=[_b_sig[id(_c)]] if _b_sig.get(id(_c)) else [], tool=_c.get("name"), args=_c.get("args") or {},
                            zone=_zone,
                            why=("要人点头" if _needs else
                                 "同一条回复里有别的调用要人点头，整条一起停下"))
                    _b0 = _blocked[0]
                    _b0t = registry.get(_b0.get("name"))
                    _b0sig = _b_sig.get(id(_b0)) or ""
                    # ★ 带上 sig/label：界面拿到才有「允许这一次」那个按钮
                    #   （2026-10-26）。不带的话，MCP/consumed 档只能"每次重问"。
                    rec("needs_human", action_id=_call_actions[id(_b0)], role="executor", tool=_b0.get("name"), args=_b0.get("args") or {},
                        zone=_b0t.zone if _b0t else None,
                        blocked=len(_blocked), total=len(reply.tool_calls),
                        sig=_b0sig, gkind=("mcp" if str(_b0.get("name") or "").startswith("mcp__")
                                           else ""),
                        label=("%s（%s 档，要人点头）"
                               % (_b0.get("name"),
                                  (_b0t.zone if _b0t else "?"))))
                    record_unstarted()
                    return _done("needs_human", turn,
                                 why="%d 个调用里有 %d 个落在需要人点头的档（%s）；"
                                     "整条回复都没执行，%d 个全部记为未执行"
                                     % (len(reply.tool_calls), len(_blocked),
                                        _b0t.zone if _b0t else "?", len(reply.tool_calls)))

                # ── 这一批调用：**先分类，再执行** ─────────────────────
                #   为什么必须"先分类"：并发之后执行顺序不再等于调用顺序，
                #   而 stuck 判据（同工具同参数连续 N 次）是**依赖顺序**的。
                #   所以顺序判据必须在动手之前跑完，否则同一个批里判出来的结果不可信。
                #   顺带统一了一件事：stuck 和 needs_human 现在都是"整条不执行"，
                #   不再出现"前两条已经写了文件、第三条才发现它在原地打转"。
                _jobs = []
                for i, call in enumerate(reply.tool_calls):
                    _name = call.get("name")
                    _args = call.get("args") or {}
                    # ★ 拿不到 name 时要能查。实测踩过：测试夹具用了 OpenAI 原始格式
                    #   `{"function": {"name": ...}}`，而这里期望 `{"name": ..., "args": ...}`
                    #   ⇒ 原来报"没有这个工具：None"，看起来像"工具没注册"，
                    #   实际是"消息格式不对"。两种错法查起来差很远。
                    _bad = ""
                    if not _name:
                        _bad = ("这个 tool_call 没有 name 字段（拿到的是 %s）。"
                                "是不是用了 OpenAI 原始格式 function.name？"
                                "这里要的是 {id, name, args}"
                                % (json.dumps(call, ensure_ascii=False)[:120]))
                    _cid = call.get("id") or "c%d_%d" % (turn, i)
                    _sig = (_name, json.dumps(_args, sort_keys=True, ensure_ascii=False))

                    if _sig == last_sig:
                        same += 1
                    else:
                        same, last_sig = 1, _sig
                    if same > max_same and not execution_core and not (_name == "run_project_check" and closed_verification):
                        rec("stuck", tool=_name, args=_args, repeat=same)
                        status = "stuck"
                        record_unstarted()
                        return _done(status, turn,
                                     why="同工具同参数连续 %d 次" % same)

                    _jobs.append({"i": i, "name": _name, "args": _args, "cid": _cid,
                                  "action_id": _call_actions[id(call)],
                                  "bad": _bad, "tool": registry.get(_name)})
                review_boundary = bool(role_coordinator and any(j["name"] == "run_project_check"
                                                               and not j["args"].get("capture_changes") for j in _jobs)
                                       and role_coordinator.pause_check_for_feedback())
                if review_boundary:
                    same, last_sig = 0, None

                # ★★ 它要问人 ⇒ **立刻停**，一个都不执行。
                #   这是"不限轮数"的必要配套：轮数放开之后，如果它缺信息还继续猜，
                #   会朝错的方向跑很远，而且越远越难纠。停下来问是最便宜的纠偏。
                #   也和"不可逆动作先停"同一条：宁可停一次，不要错一路。
                for _j in _jobs:
                    if _j["name"] == "ask_human":
                        _q = str((_j["args"] or {}).get("question") or "")[:500]
                        # ★ 候选的规则只有一套，放在 tools.normalize_options 里 ——
                        #   这条路径**先于**工具执行就 return 了，所以工具函数里那份
                        #   校验永远不会被调用到（2026-10-26 实测：写了等于没写）。
                        #   两处各写一份就是"第二套规则悄悄生效"的老坑。
                        _opts, _why = _TOOLS.normalize_options((_j["args"] or {}).get("options"))
                        rec("ask_human", question=_q,
                            options=_opts or None,
                            options_dropped=_why or None)
                        record_unstarted()
                        return _done("needs_human", turn,
                                     why="它在问你：%s" % _q[:300])

                _batch_wait = _threading.Event()
                from .check_sessions import pending as check_pending, reused as check_reused

                def _exec(_j):
                    """跑一个工具，**永不让异常逃出来**（一个炸了不能带崩整批）。

                    ★ 停止钩子挂在**当前线程**上，跑完还原（2026-10-27）。
                      为什么不在外层挂一次：readonly 工具是并发分发的，
                      它们在**线程池的 worker 线程**里跑，主线程挂的钩子它们读不到。
                      所以"谁的调用、谁挂钩子"，一次只影响这一次执行。
                    """
                    if _tool_cancel.is_set() or _batch_wait.is_set() or stop_model():
                        rec("action", action_id=_j["action_id"], tool=_j["name"], state="cancelled_before_start")
                        return ({"ok": False, "error": "因暂停未执行",
                                 "execution_state": "cancelled_before_start"}, 0)
                    ordinary_check = _j["name"] == "run_project_check" and not _j["args"].get("capture_changes")
                    if ordinary_check and review_boundary:
                        rec("action", action_id=_j["action_id"], tool=_j["name"], role="executor",
                            state="cancelled_before_start", reason="先处理本批独立复核发现")
                        return ({"ok": False, "code": "review_batch_pending", "execution_state": "cancelled_before_start",
                                 "error": "检查尚未启动。先处理下一步汇总的独立复核发现，再提交检查。"}, 0)
                    if ordinary_check and handoff_due(lead=35):
                        rec("action", action_id=_j["action_id"], tool=_j["name"], role="executor",
                            state="cancelled_before_start")
                        return ({"ok": False, "code": "review_window_reserved",
                                 "error": "剩余时间留给候选送审，请用已有回执交接",
                                 "verification": {"status": "not_run", "revision": _j["args"].get("expected_revision")}}, 0)
                    _prev = _TOOLS.set_stop(lambda: _tool_cancel.is_set() or bool(_stop_step and _stop_step())
                                            or ordinary_check and handoff_due() or review and review.timed_out())
                    identity = dict(runId=_run_id, actionId=_j["action_id"],
                                    sessionId=Path(session.dir).name if getattr(session, "dir", None) else None,
                                    tool=_j["name"], role="executor")
                    if task_input := getattr(session, "task_input", None):
                        identity.update(logicalTaskId=task_input["id"], demandRevision=task_input["revision"])
                    if execution_core:
                        identity['nativeExecutor'] = True
                    if _j["name"] in {"inspect_worker_plugins", "delegate_plugin_worker"}:
                        from .task_core_models import model_identity
                        primary = getattr(provider, "chain", [provider])[0]
                        identity["parentModelIdentity"] = model_identity(getattr(primary, "base_url", ""),
                                                                        getattr(primary, "model", ""))
                    if review and review.active:
                        identity['completionReviewDeadline'] = review.deadline
                    _call_token = _TOOLS.set_call(identity)
                    try:
                        rec("action", action_id=_j["action_id"], tool=_j["name"], state="running", summary=_TOOLS.describe(_j["name"], _j["args"]), role="executor", args=_j["args"], location=str(Path.cwd()), startedAt=time.time(), turn=turn)
                        value = _exec_one(_j)
                        result = value[0]
                        if (execution_core and _j["name"] in {"inspect_code", "read_project_resource"}
                                and isinstance(result, dict) and "_harnessCodePage" in result):
                            # Preserve the original result used by evidence and
                            # work records. The source travels with the model copy.
                            result = dict(result)
                            _j["codePageSource"] = result.pop("_harnessCodePage")
                            value = result, value[1]
                        if tool_waits(result):
                            _batch_wait.set()
                        reused_check = check_reused(result)
                        background_check = _j['name'] == 'run_project_check' and check_pending(result) and not reused_check
                        if (review and background_check and result['execution']['state'] == 'starting'
                                and isinstance(resource_id := _j['args'].get('resource_id'), str)):
                            review.preparing_resources.add(resource_id)
                        state = ("unknown" if background_check and result['execution']['state']=='unknown' else
                                 "running" if background_check else
                                 "waiting_approval" if isinstance(result, dict) and result.get("needs_human") else
                                 "cancelled_before_start" if isinstance(result, dict) and result.get("code") in {
                                     "verification_closed", "completion_review_budget"} else
                                 "unknown" if isinstance(result, dict) and result.get("ok") is False and (result.get("state")=="unknown" or result.get("execution_state")=="unknown" or result.get("code")=="unknown") else
                                 "failed" if isinstance(result, dict) and result.get("ok") is False else "completed")
                        from . import evidence as _EVIDENCE
                        observed = dict(backgroundPending=True) if background_check else dict(finishedAt=time.time(),
                            outputEvidence=_EVIDENCE.capture(session, _j["action_id"], result, tool=_j["name"]))
                        _j['evidence'] = observed.get('outputEvidence')
                        impact = (dict(reused=True, operationId=result['operationId'],
                                       evidenceActionId=result.get('evidenceActionId')) if reused_check
                                  else _EVIDENCE.impact(result))
                        rec("action", action_id=_j["action_id"], tool=_j["name"], state=state, role="executor", turn=turn, **observed, error=result.get("error") if isinstance(result,dict) else None, impact=impact, approvalRefs=(result.get("approvalRefs",[]) + ([result["grant_sig"]] if result.get("grant_sig") else [])) if isinstance(result,dict) else [])
                        return value
                    except Exception as error:
                        rec("action", action_id=_j["action_id"], tool=_j["name"], state="unknown", role="executor", finishedAt=time.time(), error=str(error))
                        raise
                    finally:
                        _TOOLS.reset_call(_call_token)
                        _TOOLS.set_stop(_prev)

                def _exec_one(_j):
                    _t0 = time.time()
                    _tool = _j["tool"]
                    if _j.get("bad"):
                        _out = {"ok": False, "error": _j["bad"]}
                    elif _tool is None:
                        _out = {"ok": False,
                                "error": "没有这个工具：%s" % _j["name"],
                                "hint": "看 registry.names() 里有没有；"
                                        "名字对不上是最常见的成因"}
                    else:
                        # ★★ 门禁装在**调用之前**，不是之后。
                        #   装在之后就只能记录"它做了"，而发布出去的产品需要的是
                        #   "它没做"。事件里记 denied，人在界面上看得见。
                        # ★ 带上这个工具**自己声明的类别**（2026-10-27）：
                        #   门禁不再靠"名字在不在某张名单里"判断 —— 那份名单
                        #   和注册处分处两地，加工具的人只要不改它就会漏。
                        #   现在单一真源是 `Tool(kind=...)`，漏声明的后果是
                        #   "这个工具用不了"（当场能看见），不是"悄悄绕过"。
                        _ok, _why = _pol.check(_j["name"], _j["args"],
                                               kind=getattr(_j["tool"], "kind", ""))
                        if not _ok:
                            _out = {"ok": False, "denied": True,
                                    "error": "被权限策略拒绝：%s" % _why,
                                    "hint": "换一个在允许范围内的做法。"
                                            "确实需要，得让人把这一轮的 policy 放宽。"}
                            rec("denied", tool=_j["name"], why=_why, args=_j["args"])
                        else:
                            # ★★ 改文件之前先留原件（2026-10-26）。
                            #   放在门禁**之后**：被拒的动作根本没执行，不该留快照
                            #   （留了会让人在撤回列表里看到一堆没发生过的"改动"）。
                            #   ★ 为什么在 loop 这一层而不是 tools 里：
                            #     `edit_file` 走的是 stage→swap 镜像（有 .bak），
                            #     而 `write_file` 是**直接写盘、没有 .bak** ——
                            #     两个入口两套退路，加第三个写类工具时又会漏。
                            #     在这一层挂一次，将来新增的写类工具自动被覆盖。
                            _snap_why = ""
                            if _j["name"] in ("write_file", "edit_file"):
                                # ★★ 快照失败**不再静默**（2026-10-27 深审）。
                                #   原来这里是 `except: pass` —— 磁盘满 / 会话目录不可写 /
                                #   文件超大 / 原文件读不出来，都会变成
                                #   "改成功了，但撤不回去，而且谁都不知道"。
                                #   人以为有安全垫，其实没有 —— 这比明说"没有底稿"更坏。
                                #   ⇒ 不挡这次改动（挡了会让大文件、无会话目录的正常活全废），
                                #     但**必须留下痕迹**：事件 + 工具结果 + 给模型的话。
                                try:
                                    _sn = _SNAP.take(_j["name"],
                                                     (_j["args"] or {}).get("path") or "",
                                                     turn=turn)
                                    if _sn:
                                        _snap_of[_j["i"]] = _sn.get("n")
                                        # 没有 file 字段 = 只记了指纹没存内容（超大/读不出）
                                        if _sn.get("error"):
                                            _snap_why = str(_sn.get("error"))
                                except Exception as _e:
                                    _snap_why = "%s: %s" % (type(_e).__name__, _e)
                                if _snap_why:
                                    try:
                                        rec("snapshot_failed", tool=_j["name"],
                                            path=(_j["args"] or {}).get("path") or "",
                                            why=_snap_why)
                                    except Exception:
                                        pass
                            try:
                                if (role_coordinator and _tool.kind in {"write", "exec"}
                                        and role_coordinator.blocks_write(_j["args"].get("resource_id"))):
                                    _out = dict(ok=False, code="review_feedback_pending",
                                                error="当前版本有阻塞复核结果，先接收下一步反馈再继续写入")
                                else:
                                    if (role_coordinator and _j["name"] == "run_project_check"
                                            and not _j["args"].get("capture_changes")):
                                        # Run this after earlier serial writes, not when classifying the whole batch.
                                        role_coordinator.prepare_project_check(_j["args"].get("resource_id"))
                                    if review and review.active and _j["name"] == "run_project_check":
                                        _out = review.run_check(_tool, execution_core.root, _j["args"])
                                    else:
                                        _out = _tool(_j["args"])
                            except ReviewLimit:
                                _out = {"ok": False, "code": "completion_review_budget",
                                        "execution_state": "cancelled_before_start",
                                        "error": "收尾复核的定向检查额度已到，请按已有回执结案。"}
                            except Problem as _e:
                                _out = {"ok":False, "code":_e.code, "error":_e.message}
                            except Exception as _e:
                                _out = {"ok": False,
                                        "error": "%s: %s" % (type(_e).__name__, _e)}
                            if _j["i"] in _snap_of:
                                try:
                                    _SNAP.seal(_snap_of[_j["i"]])
                                except Exception as _e:
                                    rec("snapshot_failed", tool=_j["name"], why=str(_e))
                            # ★ 改动成功但没底稿 ⇒ 在结果里说明白。
                            #   工具结果会进模型上下文，也会进事件流：
                            #   下一个人查"为什么撤不回来"时能直接看到原因。
                            if _snap_why and isinstance(_out, dict) and _out.get("ok"):
                                _old_note = str(_out.get("note") or "")
                                _out["rollback"] = "none"
                                _out["note"] = (("这次改动**没有撤回底稿**（%s）；"
                                                 "要退回去只能靠 git 或人手备份。" % _snap_why)
                                                + ("　" + _old_note if _old_note else ""))
                    return _out, int((time.time() - _t0) * 1000)

                # ★ 只有标了 readonly 的才并发。默认 False，所以新工具忘了标
                #   的代价是"少并行一次"，而不是"两个写操作同时动一个文件还不报错"。
                _par = [_j for _j in _jobs
                        if _j["tool"] is not None
                        and getattr(_j["tool"], "readonly", False)]
                _par_i = {_j["i"] for _j in _par}
                _seq = [_j for _j in _jobs if _j["i"] not in _par_i]
                _results: dict = {}
                _t_batch = time.time()
                _stopped_mid_tools = False

                def _await(_f, _deadline):
                    """等一个工具，但**每 0.1 秒看一眼是不是有人叫停**。

                    返回 (好了吗, 结果, 是被叫停了吗)。
                    ★ 这是"按了暂停很久才停"的第二半修复（第一半在 run_check 内部
                      杀进程树）：原来这里是 `_exec(_j)` 直接阻塞，主线程闷在里面，
                      control.json 一次都没被读。
                    """
                    nonlocal _stopped_mid_tools
                    while True:
                        try:
                            return True, _f.result(timeout=0.1), False
                        except cf.TimeoutError as error:
                            # A settled worker can itself raise TimeoutError.
                            if _f.done() and not _f.cancelled() and _f.exception() is error:
                                return True, ({"ok": False, "execution_state": "unknown",
                                               "error": "工具回执超时，请核对动作证据"}, 0), False
                            stopping = bool(_stop_step and _stop_step())
                            expired = bool(_deadline and time.time() > _deadline or review and review.timed_out())
                            if stopping or expired:
                                if not _tool_cancel.is_set():
                                    rec("tool_cancelling", turn=turn,
                                        reason="timeout" if expired else "requested",
                                        note="已请求停止，等待正在执行的工具确认结束；尚未停止")
                                _tool_cancel.set()
                                _stopped_mid_tools = True
                                # Python cannot kill running threads. Preserve ownership
                                # until the tool settles; never publish a false terminal state.
                        except cf.CancelledError:
                            return True, ({"ok": False, "error": "因暂停未执行",
                                           "execution_state": "cancelled_before_start"}, 0), False
                        except Exception as _e:
                            return True, ({"ok": False,
                                           "execution_state": "unknown",
                                           "error": "工具回执缺失，请核对动作证据：%s" % _e}, 0), False

                # ★ 批次等待也可以不设（None / 0 = 一直等）。
                #   留这个口是因为单条命令已经由"静默才杀"管着了，
                #   再压一层总时长只会把"慢但活着"的工具又杀掉一次。
                _deadline = (_t_batch + float(tool_timeout)) if tool_timeout else 0.0
                _snap_of: dict[int, int] = {}     # 调用序号 → 这是第几次改动（给界面和撤回用）

                if _par:
                    _ex = cf.ThreadPoolExecutor(max_workers=min(8, len(_par)) if parallel_tools else 1)
                    try:
                        _futs = {_ex.submit(_exec, _j): _j for _j in _par}
                        for _f, _j in list(_futs.items()):
                            _fin, _val, _stop = _await(_f, _deadline)
                            if _fin:
                                _results[_j["i"]] = _val
                                if _stopped_mid_tools:
                                    for pending in _futs:
                                        pending.cancel()
                            elif _stop:
                                _stopped_mid_tools = True
                                break
                            else:
                                # Defensive fallback; _await normally waits for settlement.
                                _results[_j["i"]] = (
                                    {"ok": False,
                                     "error": "并发执行超时（%ds）" % tool_timeout}, 0)
                        for _f, _j in _futs.items():
                            if _j["i"] not in _results:
                                _results[_j["i"]] = (
                                    {"ok": False, "error": "因暂停未执行"}, 0)
                    finally:
                        _ex.shutdown(wait=True, cancel_futures=True)
                else:
                    for _j in _par:
                        _results[_j["i"]] = _exec(_j)

                # 串行的（写文件等有副作用的）也丢进线程等 ——
                # ★ 不是为了并发（它们是串行的，一个等一个），而是为了让**主线程空出来**
                #   看停止请求。直接调用等于把主线程抵押给这个工具。
                if _seq and not _stopped_mid_tools and not _batch_wait.is_set():
                    _ex2 = cf.ThreadPoolExecutor(max_workers=1)
                    try:
                        for _j in _seq:
                            _f = _ex2.submit(_exec, _j)
                            _fin, _val, _stop = _await(_f, _deadline)
                            if _fin:
                                _results[_j["i"]] = _val
                                if _stopped_mid_tools or _batch_wait.is_set():
                                    break
                            elif _stop:
                                _stopped_mid_tools = True
                                break
                            else:
                                _results[_j["i"]] = (
                                    {"ok": False,
                                     "error": "执行超时（%ds）" % tool_timeout}, 0)
                    finally:
                        _ex2.shutdown(wait=True, cancel_futures=True)

                # ★ 按**原调用顺序**回填消息。接口要求 tool 消息和 tool_calls 一一对应，
                #   乱序会被服务端拒掉（400），而那个 400 看起来跟"并发"毫无关系。
                # ★ 暂停时也照样回填 —— 没执行的标成"因暂停未执行"。
                #   不回填的话，存档里 tool_calls 和 tool 消息对不上，
                #   下次续跑要发出去的时候就是 400，而人只会看到"续跑坏了"。
                # ★★ 工具自己说「要人」⇒ 整轮停（2026-10-26 泛化）。
                #   原来只认 `ask_human` 这个**工具名**（在分类阶段特判），于是
                #   沙箱这类"跑起来才知道要不要问"的闸只能再加一个特判。
                #   改成看**结果**：任何工具回 `needs_human: true` 都走同一条路 ——
                #   一条规则，而不是每加一个要人点头的工具就改一次循环。
                waiting = next((j for j in _jobs
                                if tool_waits(_results.get(j["i"], (None, 0))[0])), None)
                images = []
                for _j in _jobs:
                    if _j["i"] not in _results:
                        rec("action", action_id=_j["action_id"], tool=_j["name"],
                            state="cancelled_before_start", role="executor")
                    out, _ms = _results.get(_j["i"]) or ({
                        "ok": False, "execution_state": "cancelled_before_start",
                        "error": "因等待用户未执行" if waiting else "因暂停未执行",
                    }, 0)
                    # Metadata extraction must not mutate the canonical result
                    # used by audit, the work recorder and downstream consumers.
                    if isinstance(out, dict):
                        out = dict(out)
                    if _j["tool"] is None:
                        rec("tool_missing", tool=_j["name"])
                    else:
                        # ★★ diff 要**从消息里摘掉再存盘**（2026-10-26）：
                        #   它会跟着 `msgs.append(Msg("tool", json.dumps(out)))` 一起
                        #   进上下文，而模型不需要看自己刚改的 diff ——
                        #   一段 400 行的改动就是 400 行它已经知道的内容，
                        #   白白吃掉预算，还会让它把注意力放回上一处。
                        _dtxt = ""
                        if isinstance(out, dict):
                            _dtxt = str(out.pop("diff", "") or "")
                        if _dtxt and session is not None:
                            try:
                                _dd = Path(session.dir) / "diff"
                                _dd.mkdir(parents=True, exist_ok=True)
                                # 文件名用**改动编号**，和快照对齐 ——
                                # 撤回时按同一个号找得到"当时改了什么"
                                _dname = "%04d.diff" % int(_snap_of.get(_j["i"]) or 0)
                                (_dd / _dname).write_text(_dtxt, encoding="utf-8")
                            except OSError:
                                _dname = ""
                        else:
                            _dname = ""
                        tool_output = {k: v for k, v in out.items() if k not in {"image", "imageView"}} if isinstance(out, dict) else out
                        _payload = json.dumps(tool_output, ensure_ascii=False)
                        # ★ 摘要由工具层写（tools.describe），界面直接显示。
                        #   为什么不放前端：前端拿到的是同一份 args，两边各写一套规则
                        #   就等于"第二套规则悄悄生效"（本项目已踩 6 次）。一处生成，一处显示。
                        _summ = ""
                        if isinstance(out, dict):
                            _summ = str(out.get("summary") or "")
                        if not _summ:
                            try:
                                _summ = _TOOLS.describe(_j["name"], _j["args"])
                            except Exception:
                                _summ = ""
                        background_check = _j['name'] == 'run_project_check' and check_pending(out) and not check_reused(out)
                        rec("tool", action_id=_j["action_id"], tool=_j["name"], args=_j["args"],
                            state=('unknown' if out['execution']['state'] == 'unknown' else 'running') if background_check else None,
                            backgroundPending=background_check,
                            ok=bool(out.get("ok")) if isinstance(out, dict) else None,
                            summary=_summ,
                            # ★ "这是第几次改动"——界面据此显示，撤回也按它定位。
                            #   没有它，人只能说"撤回上次"，而"上次"是哪次全靠猜。
                            snap=_snap_of.get(_j["i"]),
                            # ★ 改动的规模 + 存盘文件名：界面上显示 `+12 −3`，
                            #   点开看全文（全文不进事件，一条 400 行的 diff 足以撑爆日志）。
                            diff=({"add": out.get("diff_add"), "del": out.get("diff_del"),
                                   "file": _dname}
                                  if (_dname and isinstance(out, dict)) else None),
                            out=_brief(tool_output, _j["name"]),
                            ctx_chars=len(_payload[:4000]),
                            elapsed_ms=_ms, call_id=_j["cid"],
                            parallel=_j["i"] in _par_i,
                            # ★ 记下这笔操作作用在哪个路径上。
                            #   没有它，轨迹里只有"读了一次"、没有"读了哪个"——
                            #   测试者就没法核"你说你读了 12 个文件"这句话。
                            path=(out.get("path") or out.get("written")
                                  or out.get("edited")) if isinstance(out, dict) else None)
                        # ── PostToolUse 钩子（2026-10-26）──
                        # ★ 只**观察**，不改行为：hook 的输出进事件流，工具结果照旧。
                        #   "hook 能否否决工具"是另一件事（PreToolUse），
                        #   而那会和 policy 变成两套门禁 —— 谁赢没想清楚之前不接。
                        # ★ 参数只走 stdin 的 JSON，绝不拼进命令行：
                        #   拼了就等于"模型能执行任意命令"。
                        try:
                            _hk = [] if waiting or _stopped_mid_tools else _HOOKS.run("PostToolUse", {
                                "event": "PostToolUse", "tool": _j["name"],
                                "args": _j["args"],
                                "ok": bool(out.get("ok")) if isinstance(out, dict) else None,
                                "path": (out.get("path") or out.get("written")
                                         or out.get("edited")) if isinstance(out, dict) else None,
                            }, Path.cwd())
                            for _h in _hk:
                                rec("hook", **_h, note=_HOOKS.brief(_h))
                        except Exception:
                            pass        # hook 出问题不能带崩这一轮
                    # ★★ 图片必须**摘出来**再单独发一条 user 消息。
                    #   混在工具结果的 JSON 里会被当成普通长字符串处理，
                    #   而截断的 base64 不是"少看一点图"，是一张坏图（解码失败，直接报错）。
                    _img = None
                    if isinstance(out, dict) and out.get("image"):
                        _img = out.pop("image")
                        out.pop("imageView", None)
                    # ★★ 长结果**不盲截**（2026-10-27）：超过软上限就落盘 + 给引用，
                    #   见 `_tool_msg_payload` 的说明。原来这里是 `[:4000]` ——
                    #   截断的 JSON 结构是坏的，而报错的头一条常常就在后面。
                    from .tool_results import project as project_tool_result
                    _projection = project_tool_result(_j['tool'], out, _j['args'])
                    _model_view = _projection.value
                    if execution_core or _projection.metadata['state'] != 'rendered':
                        rec('tool_projection', action_id=_j['action_id'], tool=_j['name'], **_projection.metadata)
                    _model_out = dict(_model_view, _harnessEvidence={"actionId":_j["action_id"],"runId":_run_id}) if isinstance(_model_view,dict) else _model_view
                    if execution_core:
                        if isinstance(_model_out, dict) and _j.get("codePageSource"):
                            _model_out["_harnessCodePage"] = _j["codePageSource"]
                        from .tool_spill import payload as retained_tool_payload
                        _tool_content = retained_tool_payload(_model_out, session,
                            action_id=_j['action_id'], run_id=_run_id, tool_name=_j['name'],args=_j['args'],
                            page_policy=getattr(_j['tool'],'result_page',None),
                            recoverable=registry.get('read_action_evidence') is not None
                                and 'read_action_evidence' in registry.names()
                                and registry.get('use_tool_group') is not None,
                            record=rec, renderer=_projection.metadata['renderer'])
                    else:
                        _tool_content = _tool_msg_payload(_model_out, session, turn, _j['i'], action_id=_j['action_id'],
                            bounded_code_page=getattr(_j['tool'],'result_page',None)=='code' or _j['name'] in {'search_project_code','inspect_code','read_project_resource'},
                            bounded_requirements_page=getattr(_j['tool'],'result_page',None)=='requirements' or _j['name']=='read_task_requirements')
                    msgs.append(Msg('tool', _tool_content, tool_call_id=_j['cid']))
                    if _img and _img.get("data_url"):
                        from .image_evidence import model_message

                        images.append(model_message(_img, _j["action_id"], _run_id))

                msgs.extend(images)
                if role_coordinator:
                    role_coordinator.observe(_jobs, _results)
                if execution_core:
                    observed_jobs = [job for job in _jobs if not check_reused(_results.get(job['i'], (None, 0))[0])]
                    execution_core.observe(observed_jobs, _results)
                    if same > max_same:
                        from .working_context import append_update
                        append_update(msgs, 'repeat-warning', '同一工具同参数连续出现 ' + str(same) +
                            ' 次。核对已取得的证据和未解假设，再决定继续、改变路线或报告受阻；不会自动派助手。')

                if len(_jobs) > 1:
                    # 这一条是"并行到底省了多少"的唯一直接证据：
                    # wall_ms 明显小于各工具 elapsed_ms 之和 ⇒ 真的并发了。
                    rec("tools_batch", total=len(_jobs), parallel=len(_par),
                        serial=len(_seq),
                        wall_ms=int((time.time() - _t_batch) * 1000),
                        note="只读工具并发、其余串行；结果按原调用顺序回填")

                if review and review.timed_out() and not waiting:
                    return finish_review(turn, 'deadline')
                if (_stopped_mid_tools or handoff_due()) and not waiting:
                    # ★ 工具跑到一半被叫停 ⇒ **当场停**，不再进下一轮模型调用。
                    #   原来这里只能等工具自己跑完，再进下一轮才看停止请求 ——
                    #   人看到的是"按了暂停，很久才停"，而且那一次模型调用还要花钱。
                    rec("paused_mid_tools", turn=turn,
                        note="工具执行期间收到停止请求；未执行的工具已标记为「因暂停未执行」，"
                             "消息已补齐，可以接着续跑")
                    paused = pause_or_handoff(turn - 1, "工具执行期间收到停止请求（未执行的步骤已标记）")
                    if paused is not None:
                        return paused
                    continue
                if waiting:
                    result = _results[waiting["i"]][0]
                    why = emit_tool_wait(rec, result, role="executor",
                                         action_id=waiting["action_id"], tool=waiting["name"],
                                         args=waiting["args"], zone=getattr(waiting["tool"], "zone", None))
                    return _done("needs_human", turn, why=why)
                boundary = verification_boundary([
                    (job["name"], _results.get(job["i"], (None, 0))[0]) for job in _jobs
                ])
                if boundary and boundary == closed_verification and not runtime_completion:
                    if _CORRECTIONS.deliver(session, msgs, _run_id, turn):
                        closed_verification = None
                        continue
                    if role_coordinator:
                        from .execution_completion import ready as execution_ready
                        if not execution_ready(role_coordinator):
                            from .execution_completion import wait_for_assistance
                            if wait_for_assistance(role_coordinator, should_stop):
                                completion_reminders = 0
                                closed_verification = None
                                msgs.append(Msg('user', '在途复核已有结果或补丁，请核对下一上下文中的回执继续处理。等待未宣称验收通过；实现与自检完成后提交 ready。'))
                                continue
                            completion_reminders += 1
                            if completion_reminders >= 3:
                                return _done("paused", turn, why="重复检查没有扩大覆盖，且尚未声明完整交付；保留回执")
                            msgs.append(Msg("user", "重复检查回执不代表任务完成。核对剩余要求；已完整实施时先 report_execution_status ready，再总结交付。"))
                            closed_verification = None
                            continue
                    final = "执行已收尾。当前版本已有检查回执，重复检查未扩大验证范围。请以对应版本的回执为准，未覆盖的要求仍待验证。"
                    verification = _call_verify(verify, actions)
                    if verification.get("blocked"):
                        last_verify = verification
                        return _done("needs_human", turn, why=str(verification.get("reason") or "验收判据待核对"))
                    last_verify = {"ok":False, "reason":final}
                    rec("verify_unmet", reason=final)
                    rec("say", role="executor", text=final, roleSummary=True, attempt_id=_execution_attempt_id)
                    msgs.append(Msg("assistant", content=final))
                    if role_coordinator:
                        if children := _join_children():
                            if children.get("unknown"):
                                return _done("needs_human", turn, why="子任务状态无法确认，不继续或重放")
                            continue
                        _checkpoint(turn)
                        decision, reason = role_coordinator.delivery_gate(final, turn)
                        if decision == "repair" and not (should_stop and should_stop()):
                            repairing = True
                            review_boundary = True
                            closed_verification = None
                            continue
                        if decision != "pass":
                            return _done("paused", turn, why=reason)
                    return _done("paused" if executor_only and not getattr(role_coordinator, "serial_review", False) else "success", turn, why=final)
                if boundary or any(job["name"] not in {"read_check_output", "read_action_evidence"} for job in _jobs):
                    closed_verification = boundary
                continue

            # ── 模型说"做完了" ⇒ **不许信** ─────────────────
            if not (reply.text or "").strip():
                # ★★ 空回复不是"做完了"（2026-10-27 故障注入逮到，之前一直判 success）：
                #   有工具、还有轮次，模型既没调工具也没写字 —— 判成功的话，界面上是
                #   「跑完了」+ 一片空白，而人以为它做了事。这属于最难发现的那类：
                #   **没有报错、没有红字、结论还是"成功"**。
                #   finish_reason 一起记：length = 预算被吃光；stop = 它自己就这么回的。
                rec("error", error="模型这一轮既没有调用工具，也没有给出正文",
                    finish_reason=str(getattr(reply, "finish_reason", "") or ""))
                return _done("failed", turn,
                             why="模型没有给出正文（finish_reason=%s）—— "
                                 "没有内容就不算做完了"
                                 % (str(getattr(reply, "finish_reason", "") or "未知")))
            if execution_core and not role_coordinator:
                # A proposed final reply can race a child. Give the parent its
                # actual result before publishing a final or evaluating delivery.
                children = _join_children(native_only=True, before_delivery=Msg("assistant", content=reply.text))
                if children:
                    if should_stop and should_stop():
                        return _done("paused", turn, why="收到停止请求，保留已取得的子任务回执")
                    if children.get("unknown"):
                        if review and review.timed_out():
                            review_reason = 'deadline'
                            return _done("needs_human", turn, why="收尾复核等待子任务到期，保留已确认的回执")
                        return _done("needs_human", turn, why="子任务状态无法确认，不继续或重放")
                    continue
            if review is not None:
                if (should_stop and should_stop()) or (_stop_step and _stop_step()):
                    return _done("paused", turn, why="收到停止请求，保留检查回执")
                if _CORRECTIONS.deliver(session, msgs, _run_id, turn):
                    continue
                if prepare_final_review(turn, reply.text or ''):
                    continue
                if review.timed_out():
                    return finish_review(turn, 'deadline')
                review_reason = 'final_reply'
            if (reply.text or "").strip() and review_reason != 'final_reply':
                _say_txt = reply.text.strip()
                # ★ 不再截到 500 字。原来流式时能看到全文、事件里只有前 500 字，
                #   于是"它说完了"和"我看到的"对不上 —— 人分不清是模型输出坏了
                #   还是我们截的。要截就截得足够大，并且**明确标出来**。
                rec("say", role='executor',turn=turn,text=_say_txt[:20000], roleSummary=True,
                    attempt_id=_execution_attempt_id, reviewNotes=audit_notes,
                    truncated=len(_say_txt) > 20000, full_len=len(_say_txt))
            # ★★ 模型已经说了话，就先把这段话记成 final（2026-10-28 实测）：
            #   原来 `final = reply.text` 只在**判据通过**那条分支里赋值，
            #   于是判据没过时 out.md 是空的 —— 界面上看到的是"它一个字没说"，
            #   而事件流里明明有一条 say。这两种结论对人完全相反。
            #   "判据没通过"是结论问题，不是"它没说话"。
            final = reply.text or final
            if review_reason != 'final_reply':
                msgs.append(Msg("assistant", content=reply.text))

            if role_coordinator:
                from .execution_completion import ready as execution_ready
                if not execution_ready(role_coordinator):
                    from .execution_completion import wait_for_assistance
                    if wait_for_assistance(role_coordinator, should_stop):
                        completion_reminders = 0
                        msgs.append(Msg('user', '在途复核已有结果或补丁，请核对下一上下文中的回执继续处理。remaining只列执行者未完成的工作；等待复核不必重复输出进展，也不表示验收已通过。'))
                        continue
                    completion_reminders += 1
                    rec("execution_continuation", reason="completion_not_declared", turn=turn)
                    if completion_reminders >= 3:
                        return _done("paused", turn, why="执行者未提交当前版本的完整交付声明；保留进度，未进入阻塞式复核")
                    msgs.append(Msg("user", "这是中间进展，尚未声明当前版本已实现全部要求。继续完成剩余工作；"
                        "准备最终交付时先用 report_execution_status(status='ready',summary=...,remaining=[])。"
                        "仍有待办时用 in_progress；真实缺少用户决策才 ask_human。不要把未完成摘要直接当交付。"))
                    continue

            v = _call_verify(verify, actions)
            last_verify = v
            rec("verify", ok=bool(v.get("ok")), reason=str(v.get("reason", ""))[:200])

            # ★★ 实时核验（2026-10-29 身份改成 `check` = 执行者的**自检**）。
            #   它原来标成 `benni`（测试者），于是界面上写"测试者在过程中复核" ——
            #   而按三条 loop 串行的模型，测试者是执行者停下来**之后**才登场的另一条线。
            #   自检属于执行层：它看的是"我刚说的这句和我这一轮做过的动作对不对得上"，
            #   用的是这一层自己的判据，所以它留在循环里不会验到过期的东西。
            #   用户的原话：「我之前构想的不是测试者可以一直跟进执行者的输出来做实时核验和反驳的吗？
            #   搞到最后还是只有最终输出完了才会开始读上下文进行反驳？」
            #   ⇒ 收尾那次（review.py）保留，用来看全局；这里补"过程中"，
            #     看的是"它刚说的这句"和"它这一轮真做过的动作"对不对得上。
            #   ★ 异常一律吞掉：复核是锦上添花，不许因为它挂了把正在跑的任务带崩。
            if watch is not None and (reply.text or "").strip():
                try:
                    _notes = watch(reply.text.strip(), actions, v, turn, msgs)
                except Exception as _e:
                    _notes = []
                    rec("watch_failed", error="%s: %s" % (type(_e).__name__, _e))
                if _notes:
                    rec("check", items=_notes,
                        note="自检（第 %d 轮）" % turn)

            # ★ 判据本身不可信 ⇒ 立刻找人，**不让它继续"努力达标"**。
            #   这时候它达的标可能是错的（契约被人改过 / 压根没锁）。
            #   让它接着跑，等于让它朝一个没人认可的目标越走越远。
            if not v.get("ok") and v.get("blocked"):
                rec("blocked", reason=str(v.get("reason") or "")[:300])
                return _done("needs_human", turn,
                             why="判据本身不可信，要人处理（不是 agent 能解决的）：\n%s"
                                 % str(v.get("reason") or "")[:300])

            # A human correction arriving during the final reply gets a new
            # model turn at this boundary; receiving it never means work is done.
            if review is None and _CORRECTIONS.deliver(session, msgs, _run_id, turn):
                continue
            if runtime_completion and role_coordinator:
                # Like a stopping inbox check: consume results already delivered
                # while the final response streamed, without waiting for or
                # launching another helper job merely to satisfy a declaration.
                before_feedback = len(msgs)
                delivery = role_coordinator.feedback(msgs, settling=True)
                from .assistant_patches import pending as pending_patches
                patch_notice = any(str(m.content).startswith('[context-update:assistant-patches:')
                                   for m in msgs[before_feedback:])
                if delivery or patch_notice and pending_patches(role_coordinator):
                    if _checkpoint(turn) and delivery:
                        role_coordinator.acknowledge(delivery)
                    continue
            if role_coordinator:
                if children := _join_children():
                    if children.get("unknown"):
                        return _done("needs_human", turn, why="子任务状态无法确认，不继续或重放")
                    continue
                _checkpoint(turn)
                decision, reason = role_coordinator.delivery_gate(final, turn)
                if decision == "repair" and not (should_stop and should_stop()):
                    repairing = True
                    review_boundary = True
                    closed_verification = None
                    continue
                if decision != "pass":
                    return _done("paused", turn, why=reason)
            if v.get("ok") or runtime_completion:
                status = "success"
                final = reply.text
                return _done(status, turn)

            # ★★ 判据没过：**不再喂回去，直接结束**。
            #
            #   用户的原话：「前面说到的两轮判定，严格按照 claude code 和 codex 的做法去做，
            #   不要我们自己自创」。
            #
            #   同行（Claude Code / Codex）的做法就是：**模型不再调用工具、给出最终回复
            #   就是完成**。它们没有"起草判据 → 判定 → 自动重试"这种循环 ——
            #   测试是 agent 自己决定跑不跑、自己看输出、自己判断。
            #   我们原来那个"判据不过就喂回去一次"，是自创的，而且刚好造成两个后果：
            #     ① 判据本身太严（比如要求全量验收全绿，而它在当前环境本来就红）
            #        ⇒ 每次都白跑一轮，用户看到的正是"基本上每次都要输出两次"；
            #     ② 判据写得松 ⇒ 首轮就过，等于没判。
            #   ⇒ 现在：判据只**如实记录**，不驱动重试。
            #     真没做完，由测试者（复核）去指出来 —— 那是它的活，不是判据的活。
            rec("verify_unmet", reason=str(v.get("reason") or "")[:200],
                note="判据未过；按同行的做法不自动重试，如实记录后交给复核")
            return _done("paused" if executor_only else "success", turn,
                         why="模型给出了最终答复；判据未过（%s）——如实记录，没当通过"
                             % str(v.get("reason") or "")[:120])
        # 循环自然走完 ⇒ 预算用完
        if review and review.active:
            return finish_review(turn, 'run_budget')
        return _done(status, _limit if max_turns > 0 else 0,
                     why="轮数用满 %d 轮（max_turns=%d）" % (_limit, max_turns))

    except Exception as e:
        from .context_manager import ContextBlocked
        from .execution_policy import BudgetExhausted
        from .provider import ProviderError
        if review and review.active and isinstance(e, (BudgetExhausted, ProviderError, ContextBlocked)):
            return finish_review(turn, type(e).__name__)
        if runtime_completion and isinstance(e, ProviderError) and e.kind == 'budget':
            rec('model_output_truncated', role='executor', reason=str(e))
            return _done('budget', turn, why=str(e))
        if isinstance(e, ProviderError) and e.kind == 'recovery_exhausted':
            rec('provider_unavailable', role='executor', reason=str(e))
            return _done('paused', turn, why=str(e))
        if str(getattr(e,"code","")).startswith(("approval_","security_","sandbox_","unsupported_runtime")):
            rec("needs_human", reason="security_boundary", why=str(e))
            return _done("needs_human", 0, why=str(e))
        if isinstance(e, ContextBlocked):
            rec("paused", reason="context_blocked", note=str(e))
            return _done("paused", 0, why=str(e))
        rec("error", error="%s: %s" % (type(e).__name__, e))
        return _done("error", 0, why=str(e))
    finally:
        # ★ 这里**没有**要还原的全局了（2026-10-27）：钩子挂在"执行工具的那个线程"
        #   上，由 _exec 的 finally 摘掉。留着这段空壳只会让人以为还有一份全局状态。
        if _run_permission_token is not None:
            _TOOLS.reset_call(_run_permission_token)


def _call_verify(verify, actions: list[dict]) -> dict:
    """调验证器。**支持让它看执行痕迹** —— 因为"完成"经常需要证据，而不只是绿。

    传一个参数就能拿到 actions：这样验证器可以判"你有没有真的跑过那条命令"，
    而不只是判"最终状态对不对"。
    """
    if not callable(verify):
        return {"ok": True, "reason": ""}
    try:
        import inspect
        n = len(inspect.signature(verify).parameters)
    except (TypeError, ValueError):
        n = 0
    if n >= 1:
        return verify(actions) or {"ok": False, "reason": "验证器没有返回结果"}
    return verify() or {"ok": False, "reason": "验证器没有返回结果"}


def _finish(status, turns, actions, final, verify, trace_path, trace, why="") -> dict:
    res = {"status": status, "turns": turns, "actions": actions,
           "final": final, "verify": verify, "why": why}
    if trace_path:
        p = Path(trace_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # ★ 用 "w" 不用 "a"：一个轨迹文件 = 一次运行。
        #   原来用 "a"，同一个路径跑第二次就在文件里叠第二个 JSON 对象，
        #   于是 json.load 报 "Extra data: line 2 column 1" —— 实测踩到过。
        #   要留多次运行的历史，请用不同的路径（bench 就是每个任务一个文件）。
        with p.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"status": status, "turns": turns, "why": why,
                                "final": final, "verify": verify,
                                "actions": trace}, ensure_ascii=False) + "\n")
    return res


# 单条工具结果**进模型上下文**的软上限（字符）。超过就落盘 + 给引用，不是删内容。
# ★ 为什么还要有上限：真正的硬约束是模型的上下文窗口（撑爆之后这一轮什么都干不了，
#   代价比"分两次读"大得多）。所以这里不删，只换一种交付方式。
TOOL_MSG_MAX = 12000


def _tool_msg_payload(out, sess=None, turn: int = 0, i: int = 0,
                      limit: int = TOOL_MSG_MAX, *, action_id: str | None = None,
                      bounded_code_page: bool = False,
                      bounded_requirements_page: bool = False) -> str:
    """工具结果 -> 进模型上下文的那段 JSON。

    ★ 为什么不能 `[:4000]`（2026-10-27 深审）
      盲截有两个后果，而且都不是"少看一点"：
      ① **截断的是 JSON 文本**，切一半的结果是**结构坏了**（模型得去猜哪里断的）；
      ② 报错的头一条、测试结论、diff 的尾部常常就在 4000 字之后，
         模型看不到，然后**重跑同一条命令**再被截一次 —— 每一轮都在付利息。
    ★ 现在分三档：
      ≤ limit           原样给全；
      > limit 且有会话   全文落盘到 `<会话>/tool-out/`，消息里给**路径 + 字数 + 怎么读**，
                        长字符串字段就地换成一句"见文件"的说明（结构保持完整）；
      > limit 且没会话   只能截，但**明说截了多少、原文在哪**，不许假装是全文。
    """
    try:
        raw = json.dumps(out, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False,
                           "error": "工具结果序列化失败：%s: %s" % (type(e).__name__, e)},
                          ensure_ascii=False)
    # Code tools already bound an explicitly requested page. Do not
    # replace that page with a path unavailable to scoped agents. Context
    # capacity/archiving remains owned by the context manager.
    keep_page = (bounded_code_page and isinstance(out, dict)
                 and (isinstance(out.get('text'), str) and len(out['text'].encode('utf8')) <= 50 * 1024
                      and type(out.get('startLine')) is int and type(out.get('endLine')) is int
                      or isinstance(out.get('matches'),list) and 'fileRevisions' in out and len(raw)<=18000))
    keep_requirements = (bounded_requirements_page and isinstance(out, dict)
                         and isinstance(out.get('text'), str) and len(out['text']) <= 12000
                         and type(out.get('next')) is int and type(out.get('hasMore')) is bool)
    if len(raw) <= limit or keep_page or keep_requirements:
        return raw

    ref = ""
    if sess is not None:
        try:
            d = Path(sess.dir) / "tool-out"
            d.mkdir(parents=True, exist_ok=True)
            fp = d / ((action_id + ".json") if action_id else ("t%03d-%02d.json" % (int(turn or 0), int(i or 0))))
            fp.write_text(raw, encoding="utf-8")
            ref = str(fp)
        except Exception as e:
            ref = ""
            _err = "%s: %s" % (type(e).__name__, e)

    slim = {}
    if isinstance(out, dict):
        for k, v in out.items():
            if isinstance(v, str) and len(v) > 1200:
                slim[k] = "（%d 字，为了不撑爆上下文没放进来，全文见下面 out_ref）" % len(v)
            else:
                slim[k] = v
    else:
        slim = {"ok": True, "value": "（%d 字，全文见 out_ref）" % len(raw)}

    # ★ `pop` 必须带默认值（2026-10-29 实测撞到）：这条路径只在**输出超过 12000 字**
    #   时才会走到，所以平时不炸 —— 一旦炸了，整轮变成 `status=error` / `why='note'`，
    #   而**活其实已经干完了**（执行者改完文件、test 都绿了，就死在这行）。
    #   现象极难对上：job status 显示"失败"、turns=0、verify=null，
    #   而测试者那边说"看起来过"。差的正是这一句。
    _old_note = str(slim.pop("note", "") or "")
    if ref:
        slim["out_ref"] = ref
        slim["out_chars"] = len(raw)
        slim["note"] = ("这条结果共 %d 字，**没截断**：全文写在上面的 out_ref 里。"
                        "要看它就用 read_file 读那个文件（整篇都会给你）；"
                        "只想看某一段就先用 code_search / grep 定位行号，再用 line 读。"
                        % len(raw))
    else:
        slim["out_chars"] = len(raw)
        slim["truncated_chars"] = len(raw) - limit
        slim["note"] = ("这条结果共 %d 字，这里只放了前 %d 字（没落盘，所以拿不到剩下的）。"
                        "要看全，请把命令收窄（加 head / grep / 只查一个文件）。"
                        % (len(raw), limit))
    if _old_note:
        slim["tool_note"] = _old_note        # 工具自己那句话要留着，别被上面这句盖掉
    return json.dumps(slim, ensure_ascii=False)


def _brief(out, tool: str = "", limit: int = 300) -> str:
    """工具的返回压成一句话。压不好的话，看页面的人会把「没找到」读成「出错了」。

    ★ 只存摘要不存全文：轨迹要能被一眼扫完；全文有价值的部分可以靠 args 重跑拿到。

    ★★ 按工具名分派，不按返回的 key 反推是谁。
       原来写的是 `if "content" in out and "chars" in out`，靠形状猜。
       这跟「拿词当结构」是同一个错，只是把字符串换成了 dict 的 key：
       哪天 read_file 的返回也带个 rc、或者 run_check 也返 content，
       两个分支就会互相抢 —— 而且抢错的后果是摘要说错话，
       页面上看着还挺正常，没人会发现。名字是调用方手上就有的确定信息，没理由去猜。
    """
    if isinstance(out, dict):
        if tool == "read_file":
            # ★ 被拒的时候不能写"读到 None 字符"（2026-10-26 真跑逮到的显示 bug）：
            #   那句话读起来像"读到了空内容"，而实际是**根本没读**。
            #   轨迹是给人看的 —— 它说错话比不说话更糟。
            if out.get("ok") is False:
                return "没读成：%s" % str(out.get("error") or out.get("why") or "")[:100]
            head = [x.strip() for x in (out.get("content") or "").split("\n") if x.strip()]
            s = "读到 %s 字符，开头是「%s」" % (
                out.get("chars"), " / ".join(head[:2])[:90])
            if out.get("truncated"):
                s += "（**被截断了，只给了前 %d 字**）" % len(out.get("content") or "")
            return s
        if tool == "run_check":
            body = (out.get("out") or "").strip() or (out.get("err") or "").strip()
            s = "退出码 %s" % out.get("rc")
            if body:
                s += "｜" + body.replace("\n", " ⏎ ")
            return s[:limit]
        for k in ("summary", "text", "stdout", "error", "message"):
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:limit]
        s = json.dumps(out, ensure_ascii=False)
    else:
        s = str(out)
    return s[:limit]


# ───────────────────── 验证器：把验收脚本当判据 ─────────────────────

def make_verify_from_cmd(cmd: list[str], timeout: int = 180):
    """把一条命令的退出码当成"完成没完成"。绿=完成，红=没完成。"""
    import subprocess

    def verify() -> dict:
        try:
            from . import safe_executor
            r = safe_executor.run(cmd,text=True,encoding="utf8",errors="replace",timeout=timeout,isolated=True)
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            return {"ok": r.returncode == 0,
                    "reason": ("退出码 %d；末行：%s" % (r.returncode, tail[0])).strip()}
        except Exception as e:
            return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}
    return verify
