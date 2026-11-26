"""Historical role entry, loaded only outside the native L0 route.

Keeps old serial/parallel policies local; this is not the future plugin host.
"""
# 主要引用方包括 `bin/run-task.py`。

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from . import control as CTRL, execution_policy as E, loop as LOOP, provider as PR
from . import scoped_roles
from .scoped_roles import RoleWaiting, registry as draft_registry, recent_public_dialogue
from .provider import Msg
from .core_runtime import command as core_command
from .entry_material import agreement_context, _interrupt_context, resume_requirements

def _plan_first(task: str, sess, *, parts=()) -> str:
    """L2/L4：先跑一次**规划者**，把它那句方向交给执行者。

    ★ 这是"规划者接入对话"的那一步（2026-10-29）。
      它原来只在 `job.py`（多轮任务）里上场，界面的单轮对话不经过它 ——
      用户选了 L2 却在界面上看不到规划者。

    ★ 模型档案用 `plan`（2026-10-29）：**配了就用选的，没配就借用执行者的**
      （兜底在 `provider.py` 里，不在这一层写死）。
      用户原话「能把执行者的 key 暂时借用过去吗」——
      借用是兜底，不是把规划者永远绑在执行者的模型上。

    ★ 它说的话**进事件流**（`delta`，`role="planner"`）：
      界面上该看到"规划者说了什么"，不是只在后台跑了一下。
    """
    from loop import execution_policy as _EXECUTION, scoped_roles
    if _EXECUTION.binding(Path.cwd()):
        try:
            plan=scoped_roles.run(Path.cwd(),sess,'planner',task,parts=parts,check_allowance=1)
            from loop.filelock import atomic_json
            atomic_json(Path(sess.dir)/'memo.json',dict(task=task,plan=plan))
            return plan
        except (scoped_roles.RoleWaiting, PR.Interrupted):
            raise
        except Exception as error:
            print('规划者未完成：'+str(error),flush=True)
            return ''
    import tempfile
    attempt_id=uuid.uuid4().hex
    if sess is not None:
        sess.emit("role_phase",attempt_id=attempt_id,role="planner",state="running",reason="规划调用已开始")
    try:
        memo = Path(tempfile.mkdtemp()) / "memo.json"
        memo.write_text(json.dumps({"task": task}, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(
            core_command("plan.py", "--plan", "--memo", str(memo), "--provider", "plan"),
            capture_output=True, timeout=180,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except Exception as e:
        if sess is not None:
            sess.emit("role_phase",attempt_id=attempt_id,role="planner",state="failed",error=str(e)[:1000])
        print("规划者：没跑成（%s）—— 这一轮按没有规划者跑" % e, flush=True)
        return ""
    out = (r.stdout or b"").decode("utf-8", "replace")
    plan, reasoning = "", ""
    for ln in out.splitlines():
        if ln.startswith("PLAN "):
            try:
                _j = json.loads(ln[5:]) or {}
                plan = str(_j.get("plan") or "").strip()
                reasoning = str(_j.get("reasoning") or "").strip()
            except Exception:
                plan, reasoning = "", ""
    if not plan:
        if sess is not None:
            sess.emit("role_phase",attempt_id=attempt_id,role="planner",state="failed",reason="规划进程没有返回可用方向",exitCode=r.returncode)
        print("规划者：没给出方向（退出码 %s）—— 这一轮按没有规划者跑" % r.returncode,
              flush=True)
        return ""
    if sess is not None:
        try:
            # ★ 方向要**落盘**（2026-10-30 千行工程实测）：
            #   测试者 `_plan_text()` 读的是 `memo.json`，而规划者原来只把方向
            #   追加给执行者、没有写给测试者 —— 于是测试者说「没有计划可对照」，
            #   只能核「说的和现场是否一致」，核不了「做的是不是要求的」。
            #   写进会话目录之后，续跑也能读到同一份方向。
            _memo_p = Path(sess.dir) / "memo.json"
            _prev = {}
            try:
                if _memo_p.exists():
                    _prev = json.loads(_memo_p.read_text(encoding="utf-8")) or {}
            except Exception:
                _prev = {}
            _prev.update({"task": task, "plan": plan, "reasoning": reasoning})
            _memo_p.write_text(json.dumps(_prev, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        try:
            # ★ 思考也要进流（2026-10-29）：不然界面上规划者像"凭空给了个方向"。
            if reasoning:
                sess.emit("agent_thought_chunk", sessionUpdate="agent_thought_chunk", kind="think", turn=0, role="planner", text=reasoning)
            sess.emit("say", role="planner", text=plan)
        except Exception:
            pass
    if sess is not None:
        sess.emit("role_phase",attempt_id=attempt_id,role="planner",state="completed" if r.returncode==0 else "failed",reason="规划方向已返回" if r.returncode==0 else "规划产出存在，但进程异常退出",exitCode=r.returncode)
    return plan


def _plan_text(contract_dir: str = "", sess_dir=None) -> str:
    """取得本轮计划。用户任务会在复核交接中单独传递。

    取值的优先级（谁定的计划就用谁的）：
      ① 契约的 `intent`（第四档，人锁过）
      ② 会话目录 `memo.json` 里保存的规划方向
      ③ 都没有 ⇒ 空串。
    """
    try:
        if contract_dir:
            _o = json.loads((Path(contract_dir) / "done.json").read_text(encoding="utf-8"))
            _t = str((_o or {}).get("intent") or "").strip()
            if _t:
                return _t
    except Exception:
        pass
    try:
        if sess_dir is not None:
            _m = json.loads((Path(sess_dir) / "memo.json").read_text(encoding="utf-8"))
            _p = (_m or {}).get("plan")
            if isinstance(_p, dict):
                _p = _p.get("plan") or ""
            _t = str(_p or "").strip()
            if _t:
                return _t
    except Exception:
        pass
    return ""


def _benni(trace, workdir: Path, task_text: str, final_text: str, sess,
           ctx: int = 0, root=None, *, user_task="", contract_dir=None, dialogue=(), agreement=None, retest=None):
    """跑一次测试者复核，结果记进事件流。

    ★ 这个东西是干嘛的：测试者是**另一家模型**读这一轮的流水和结论，
      指出"哪句话缺证据"。它不能自己判赢，只能说"这里缺什么，你看算不算"。
      没有它，"自我说做完了"就只能靠信 —— 而整套设计的目的就是**不靠信**。

    ★ 没配凭据就**明确跳过并说出来**，不静默。静默跳过会让人以为
      "审过了，没问题"，那比没审更糟。
    """
    from loop import execution_policy as _EXECUTION, scoped_roles
    if root is not None and _EXECUTION.binding(root):
        try:
            criteria = []
            if contract_dir:
                try:
                    done = json.loads((Path(contract_dir) / "done.json").read_text(encoding="utf-8"))
                    if isinstance(done.get("done"), list):
                        criteria = done["done"]
                except (OSError, ValueError, AttributeError):
                    pass
            return scoped_roles.review(root, sess, task_text, final_text, workdir,
                                       user_task=user_task, criteria=criteria, dialogue=dialogue,
                                       agreement=agreement, retest=retest)
        except scoped_roles.RoleWaiting as error:
            return error
        except scoped_roles.ReviewTimedOut as error:
            sess.emit('benni', attempt_id=sess.scoped_role_attempts["reviewer"],
                      ok=False, timedOut=True, reason=str(error))
            return 'timed_out'
        except PR.Interrupted:
            sess.emit('benni',attempt_id=getattr(sess,'scoped_role_attempts',{}).get('reviewer'),ok=False,paused=True,reason='用户请求停止，复核未完成')
            return 'paused'
        except Exception as error:
            sess.emit('benni',attempt_id=getattr(sess,'scoped_role_attempts',{}).get('reviewer'),ok=False,reason='复核未完成：'+str(error)[:1000])
            return 'failed'
    import subprocess
    attempt_id = uuid.uuid4().hex
    out = workdir / ("benni-" + attempt_id + ".md")
    legacy = workdir / "benni-model.md"
    def emit_review(**fields):
        if sess:
            sess.emit("benni",attempt_id=attempt_id,**fields)
    try:
        if legacy.exists():
            if legacy.is_symlink() or not legacy.is_file():
                raise ValueError("兼容复核报告必须为普通文件")
            legacy.replace(workdir / ("benni-history-" + attempt_id + ".md"))
        legacy.write_text("本轮尚未生成新的复核意见；历史报告已单独保留。",encoding="utf-8")
    except (OSError,ValueError) as error:
        if sess:
            sess.emit("role_phase",attempt_id=attempt_id,role="reviewer",state="failed",reason="无法准备本轮复核产物："+str(error)[:500])
        return
    intent = workdir / ("benni-intent-" + attempt_id + ".json")
    try:
        intent.write_text(json.dumps({"plan": task_text, "userTask": user_task, "recentDialogue": dialogue}, ensure_ascii=False),
                          encoding="utf-8")
    except Exception:
        pass
    cmd = core_command("review.py", "--benni-out", str(out), "--provider", "review", unbuffered=True)
    # ★ 分母显式传（来自 config 的 context_limit，唯一真源）。
    #   不给的话测试者只能写"上限没传进来，只报绝对数" —— 那是实话，但没解决问题。
    if ctx:
        cmd += ["--ctx", str(int(ctx))]
    # ★ 副本要从**这一轮的工作区**拷（2026-10-28）：不传的话 review.py 默认
    #   拷仓库根，而任务可能跑在别的目录 —— 那它审的就是一个空仓库。
    if root is not None:
        cmd += ["--root", str(root)]
    # ★ 不许「没轨迹就退回 review.py 的默认值」。
    #   那个默认值指向仓库里的旧文件（runs/loop-render.jsonl），
    #   于是测试者会拿**别人的上一件事**去审，还交出一份格式完全正常的批注 ——
    #   错得一点声音都没有，比不跑坏得多。实测就是这么审了 10 月 23 日那一轮。
    if not trace or not Path(trace).exists():
        _why = ("这一轮没有轨迹（trace 为空或文件不存在），测试者不跑 —— "
                "宁可没审，也不要拿别人的流水冒充这一轮")
        if sess:
            emit_review( ok=False, skipped=True, reason=_why)
        print("测试者：跳过 —— %s" % _why[:80])
        return
    cmd += ["--trace", str(trace)]
    if intent.exists():
        cmd += ["--intent", str(intent)]
    criteria_path = Path(contract_dir or workdir) / "done.json"
    if criteria_path.exists():
        cmd += ["--done", str(criteria_path)]
    # ★ 改动清单在**会话目录**里（run-task 收尾时写在那儿），不是产物目录。
    #   第一版按 workdir 找 —— 于是 `--changes` 一次都没传进去，提示词里也就没有
    #   那一节。**"接了"和"接上了"是两件事**，这条只有真跑一次才看得出来。
    #   两个位置都试，先会话目录再产物目录（老会话只有后者）。
    _sdir = getattr(sess, "dir", None) if sess is not None else None
    for _base in ([_sdir] if _sdir else []) + [workdir]:
        _cj = Path(_base) / "changes.json"
        if _cj.exists():
            cmd += ["--changes", str(_cj)]
            break
    # ★ 把拼好的提示词留一份（2026-10-27）：测试者说"这里缺证据"的时候，
    #   人得能翻出"它当时到底看到了什么"。不留就只有结论，没法复核。
    cmd += ["--prompt-out", str((_sdir or workdir) / "benni-prompt.md")]
    # ★ 先发一条"它在看" —— 测试者要跑 1~3 分钟（另一家模型 + 工具循环），
    #   这期间界面静悄悄的话，人会以为"测试者压根不存在"（用户就是这么说的）。
    if sess:
        try:
            emit_review( ok=None, running=True,
                      reason="测试者在读这一轮的流水（另一家模型，1~3 分钟）")
        except Exception:
            pass
    try:
        # ★ 180 秒不够：测试者要走工具循环，最多 6 轮模型调用，每轮几十秒。
        #   实测 180 秒正好卡在最后一步，于是**每一轮都是 skipped** ——
        #   批注永远不会出现，而现象是"测试者消失了"。
        #
        # ★★ 改成 Popen 边跑边转事件（2026-10-29）：原来 `subprocess.run` 把它
        #   的输出**全收完**才回来，于是 1~3 分钟里界面只有开头那一条"测试者在读流水"，
        #   之后突然冒出结论 —— 用户说的「完全不知道这个流程还有没有在动」就是这段。
        #   它自己的每轮都 print 进度（"测试者第 N 轮：给了 6 个工具，它要调 2 个"），
        #   那些话本来就在，只是没人转发。
        #   `-u` 必须加：管道里的 Python 默认块缓冲，不加的话这些 print 会
        #   攒到进程结束才一次性吐出来 —— 转发就成了"最后一次性刷 20 行"，等于没做。
        _review_env = dict(os.environ)
        if _sdir:
            _review_env["HARNESS_CONTROL"] = str(CTRL.path(_sdir))
        pr = subprocess.Popen(cmd,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", bufsize=1,
                              env=_review_env)
    except Exception as e:
        if sess:
            emit_review( ok=False, skipped=True, reason="测试者没跑起来：%s" % e)
        return

    # 超时靠看门狗，不靠 run(timeout=) —— 换成 Popen 之后那个参数就没了。
    # ★ 不叫 `_watch`：这个名字已经被秒表占了（验收当场抓到）。
    def _benni_watchdog():
        try:
            pr.wait(timeout=420)
        except Exception:
            try:
                pr.kill()
            except Exception:
                pass

    threading.Thread(target=_benni_watchdog, daemon=True).start()

    # ★ 只转**有信息量的**那几行（2026-10-29）：它一共 36 处 print，
    #   全转的话一次复核刷二十来行"测试者 …"，比没有更吵。
    #   挑的是"走到第几步"和"哪里出事了" —— 那两类才是"还在不在动"的答案。
    def _worth(line: str) -> bool:
        s2 = line.strip()
        if not s2:
            return False
        if ("第 " in s2 and "轮" in s2) or s2.startswith(("✗", "⚠", "测试者模型", "测试者的工作副本")):
            return True
        return "副本" in s2 and "开成" in s2

    _collected: list[str] = []
    try:
        for _ln in (pr.stdout or []):
            _s2 = (_ln or "").rstrip()
            if not _s2:
                continue
            _collected.append(_s2)
            if sess and _worth(_s2):
                try:
                    emit_review( ok=None, running=True, reason=_s2[:220])
                except Exception:
                    pass
    except Exception:
        pass
    try:
        pr.wait(timeout=60)
    except Exception:
        pass
    # 后面几处读 pr.stdout / pr.stderr 的，统一给同一个字符串（stderr 已合并）。
    _all_out = "\n".join(_collected)
    try:
        pr.stderr = ""
        if pr.stdout:
            pr.stdout.close()
        pr.stdout = _all_out
    except Exception:
        pass

    # 测试者在执行步边界主动退出：不能拿上一轮的旧报告冒充这一轮已复核。
    if pr.returncode == 6:
        if sess:
            emit_review( ok=False, paused=True,
                      reason="测试者已在当前执行步结束后暂停；进度留给下一次收揽")
        print("测试者：已在执行步边界暂停")
        return "paused"

    # ★ 只要产出文件在且非空就算成功，**不看退出码** ——
    #   review.py 写完 benni-model.md 之后还会跑一个附带步骤，
    #   那一步失败会把退出码带成 1，于是"测试者明明出了结果"被记成"跳过"。
    #   实测就是这么误判的（事件里写着 skipped=True，reason 却是它的正常 stdout）。
    if out.exists() and out.stat().st_size > 0:
        txt = out.read_text(encoding="utf-8", errors="replace")
        legacy.write_text(txt,encoding="utf-8")
        if sess:
            emit_review( ok=True, body=txt[:20000], reportRef=out.name, reportHash=hashlib.sha256(txt.encode("utf-8")).hexdigest())
            # ★ 测试者的判断也进**正文流**（2026-10-29 用户：「say 里面不止有
            #   执行者的输出，规划者的和测试者的我也要能看到」）。
            #   原来它只出现在旁边的批注列里 —— 正文流里完全看不到测试者说了什么。
            #   现在按角色发一条 delta，界面上和执行者的话按时间顺序排。
            try:
                sess.emit("say", role="reviewer", text=txt[:6000])
            except Exception:
                pass
        print("测试者复核：%s" % out)
        # ★ 正文在、但退出码不是 0 ⇒ 它写完正文之后崩了（2026-10-28 实测踩到：
        #   一次 NameError 让结构化记录没写出来，而这里只看"文件在不在"，
        #   于是"记录没写"这件事一点声音都没有 —— 人看到的只是"这一轮没有记录"，
        #   看不出是代码炸了）。
        #   不推翻"这一轮有批注"这个结论，但**必须把崩溃说出来**。
        if pr.returncode != 0:
            _tail = [x for x in (pr.stderr or pr.stdout or "").strip().splitlines() if x.strip()][-3:]
            if sess:
                emit_review( ok=False, warn="复核写完正文后异常退出",
                          rc=pr.returncode, tail=_tail)
            print("⚠ 测试者写完正文后异常退出（rc=%s）—— 结构化记录可能没写出来"
                  % pr.returncode)
            for _l in _tail:
                print("   %s" % _l[:160])
        return
    if pr.returncode == 3:
        _why = ("测试者要单独配一份凭据（HARNESS_REVIEW_*），最好是**另一家**模型 —— "
                "同厂同源的复核独立性为零，只能验管道不能采信")
    elif pr.returncode == 4:
        # ★ 入口是好的、调用失败了（2026-10-27 分开）。原来这两种共用一个退出码，
        #   于是网络抖一下，界面上就写着"去配凭据"—— 指错了一层，
        #   人会照着它去翻一个根本没问题的配置。
        _why = ("测试者这次调用失败（凭据是配好的）：%s"
                % ((pr.stderr or pr.stdout or "").strip()[:200] or "没留下原因"))
    else:
        _why = ((pr.stderr or pr.stdout or "").strip()[:200] or "测试者没跑成")
    if sess:
        emit_review( ok=False, skipped=True, reason=_why)
    print("测试者：跳过 —— %s" % _why[:100])


def _skip_reviewer_for_simple_chat(thread, result, contract_dir, min_reads, min_chars, changes) -> bool:
    """Skip model review only when this conversation has no checkable work."""
    if not thread or not thread.get("projectless") or contract_dir or result.get("status") != "success":
        return False
    if int(min_reads or 0) or int(min_chars or 0):
        return False
    if any(row.get("act") in {"tool", "action", "needs_human", "ask_human", "not_run"}
           for row in result.get("actions", [])):
        return False
    if not changes or not changes.get("ok"):
        return False
    return not any(changes.get(key) for key in ("changed", "added", "removed"))


# 在显式启用的旧角色执行里接收在途动作，按可用复核提供方建立监听。
def live_watcher(args, session, root, task):
    _rprov = {"p": None, "tried": False}
    _rlock = threading.Lock()

    def _review_prov():
        """另一家模型。没配就返回 None（降级成只做账实核对，不报错）。

        ★ 加锁：主线程和观察者线程都可能第一次调它，
          两个同时初始化会各建一个 provider（多一次握手，还可能撞凭据读取）。
        """
        with _rlock:
            if _rprov["tried"]:
                return _rprov["p"]
            _rprov["tried"] = True
            try:
                from loop.provider import OpenAICompatProvider as _OCP
                _rprov["p"] = _OCP(profile="HARNESS_REVIEW")
            except Exception:
                _rprov["p"] = None
            return _rprov["p"]

    # ★★ 这是**执行者 loop 内部的自检**，不是测试者（2026-10-29 用户定的"方案 2"）。
    #   它原来发的是一条 `benni`（测试者）事件，于是界面上显示"测试者在过程中批注"，
    #   而按三条 loop 串行的模型，测试者是执行者跑完之后才登场的另一条线。
    #   一个身份两个职能，人和模型都分不清"这是谁在说话、它属于哪一层"。
    #   ★ 它留在循环里是**对的**：它用的是执行层自己的判据
    #     （"我做的和我说的对得上吗"），不会去验一个还在变的东西。
    #     测试者不行 —— 它验的是"证据够不够"，那必须在执行停下来之后才成立。
    _livew = None
    if args.live:      # 默认不开 —— 见上面 `--live` 那段说明
        try:
            from loop import live as LV
            from loop import tools as _TL

            # ★ 给**自检**一只"能自己核"的手（2026-10-29 改的身份：它属于执行层，
            #   不是测试层的测试者）。用的是执行者同一个执行器
            #   （同一份只读白名单、可中断、120s 超时），不是另开一条权限通道。
            def _ben_exec(cmd, timeout: int = 120):
                # skip_if_busy：自我正在跑命令时测试者退让 —— 不抢、不排队
                # ★ 传 root（2026-10-26 加白名单时补的）：路径边界要和自我**同一个根**。
                #   靠 cwd 隐式相等是可以，但"两个地方各算一次根"迟早不一致 ——
                #   而不一致的表现是"测试者核不出来的命令，自我明明跑过"。
                return _TL.exec_readonly(cmd, timeout=timeout, skip_if_busy=True,
                                         root=root)

            _livew = LV.Watcher(session, task=task, prov_get=_review_prov,
                                exec_fn=_ben_exec,
                                # ★ "把一条动作还原成能重跑的命令"由 tools 层提供 ——
                                #   它掌握解释器映射和仓库根。live.py 自己再写一套，
                                #   就会出现"跑的是 .sh、复跑用 python"这种白跑。
                                recheck_fn=lambda ev: _TL.recheck_argv(ev, root))
        except Exception as _e:
            print("自检：起不来，这一轮只做收尾复核：%s" % _e)

    return _livew


class LegacyEntry:
    interrupt_errors = (RoleWaiting, PR.Interrupted)

    # 持有旧执行方式的会话、已核对恢复状态和运行设置，供规划、执行及收尾共用。
    def __init__(self, args, session, root, task, task_input, config, previous,
                 input_kind, controller, bound, stop, watch, live):
        self.args, self.session, self.root, self.task = args, session, root, task
        self.task_input, self.config, self.previous = task_input, config, previous
        self.input_kind, self.controller, self.bound = input_kind, controller, bound
        self.stop, self.watch, self.live = stop, watch, live
        self.messages = None
        self.plan = ''
        self.plan_text = ''
        seconds = os.environ.get('HARNESS_HANDOFF_NOTICE_SECONDS', '')
        import time
        self.handoff_at = (time.monotonic() + int(seconds)
                           if not args.no_benni and seconds.isascii()
                           and seconds.isdecimal() and len(seconds) <= 6 else None)
        self.agreement_version = (self.controller.get("contract") or {}).get("version")
        self.recovery = (self.previous.get("roleWait") if self.controller and self.input_kind in {"answer", "resume"}
                     and self.previous.get("status") in {"needs_human", "paused"} else None)
        if (self.recovery and (self.recovery.get("agreementVersion") != self.agreement_version
                           or (self.recovery["role"] == "planner" and not self.args.plan_first)
                           or (self.recovery["role"] == "reviewer" and self.args.no_benni))):
            self.recovery = None
        self.session.role_recovery = self.recovery
        self.resume_review = bool(self.recovery and self.recovery["role"] == "reviewer")


    def _role_checkpoint(self, role, receipts):
        checkpoint = dict(role=role, receipts=list(receipts), agreementVersion=self.agreement_version)
        if role == "reviewer":
            checkpoint.update(executorRun=getattr(self.session, "review_executor_run", None) or self.session.run_id,
                              reviewRun=self.session.run_id, plan=self.plan_text)
        return checkpoint

    # 将角色暂停记录与实际执行轮次和约定版本绑定。
    def save_role_stop(self, role, status, reason, receipts=(), question=None, conflict_id=None):
        checkpoint = self._role_checkpoint(role, receipts)
        state = dict(status=status, why=reason, task=self.task, roleWait=checkpoint,
                     waitQuestion=bool(question), waitConflictId=conflict_id)
        if role == "planner":
            state.update(msgs=[m.dump() for m in (self.messages or [Msg("user", self.task)])],
                         final="")
        self.session.save(**state)
        self.session.emit("end", role=role, status=status, why=reason)

    # 区分等待用户与暂停，保留已发生的工具回执和下次恢复所需消息。
    def interrupted(self, error, messages, task):
        self.messages = messages
        waiting = isinstance(error, RoleWaiting)
        status = 'needs_human' if waiting else 'paused'
        self.save_role_stop('planner', status, str(error), error.receipts if waiting else (),
                            error.question if waiting else None, error.conflict_id if waiting else None)
        return dict(status=status, turns=0, final='', why=str(error), actions=[])


    # 从恢复记录定位已执行轮次，让待续接复核沿原证据进行。
    def resume_result(self, messages):
        self.messages = messages
        self.session.review_executor_run = self.recovery["executorRun"]
        if self.args.plan_first:
            self.session.emit("role_phase", role="planner", state="completed",
                       reason="沿用已确认的规划", sourceRunId=self.session.review_executor_run)
        self.session.emit("role_phase", role="executor", state="completed",
                   reason="恢复测试者，沿用已有执行结果", sourceRunId=self.session.review_executor_run)
        self.session.save(msgs=[m.dump() for m in self.messages])
        r = dict(status="success", turns=0, final=self.previous.get("final", ""), actions=[],
                 why="恢复等待中的测试者，保留上一轮执行结果")
        return r

    # 按选择的兼容模式处理规划、角色工具注册、复核配置及恢复入口。
    def prepare(self, registry, system, task_agent, task_parts, messages, followup,
                display, interrupt, contract_dir, recent_dialogue):
        self.messages = messages
        _brief = agreement_context(str(contract_dir), planner_notes=True) if contract_dir else ""
        self.plan = ""
        # ★ 跑完把没发完的增量发掉：最后一截常常不到 160 字，
        #   不冲的话，那一截字**只会出现在文件里、不会出现在事件流里**。
        # ★ 含规划者的档位：先规划，再执行（2026-10-29 接上）。
        #   它的话进事件流（`delta` / `role=planner`），界面上看得到；
        #   它的方向追加进执行者的任务里 —— 不是只跑一下给人看。
        # 有新用户要求或插话时重新规划；审批和暂停后的续跑沿用当前方向。
        new_request = bool(self.args.resume and self.args.say and self.input_kind in {"new", "amend", "answer_amend"})
        self.plan_now = not self.args.resume or interrupt or new_request or self.recovery or (
            self.config and self.config["crew"] == "l4"
        )
        if self.args.plan_first and not self.plan_now:
            self.session.emit("role_phase", role="planner", attempt_id=uuid.uuid4().hex,
                       state="skipped", reason="沿用已保存方向，继续当前任务")
        if self.args.plan_first and self.plan_now:
            _ptask = self.task
            _plan_parts = list(task_parts)
            if new_request or self.controller or self.recovery:
                _ptask = "本轮用户要求：\n" + (display or self.args.say or self.task)
                _plan_parts = [("task", _ptask)]
                if self.task and not self.controller:
                    original = "\n\n原任务：\n" + self.task[:12000]
                    _ptask += original
                    _plan_parts.append(("dialogue", original))
            if interrupt:
                interrupt = "\n\n" + _interrupt_context(interrupt) + "\n请重新收揽上下文，给出这一轮的做法。"
                _ptask += interrupt
                _plan_parts.append(("handoff", interrupt))
            if self.controller:
                from loop.context_manager import json_field_parts
                material, parts = json_field_parts([
                    ("contract", self.controller["contract"], "agreement"),
                    ("priorProgress", self.controller.get("priorProgress"), "handoff"),
                    ("checks", self.controller.get("checks"), "handoff"),
                ])
                _ptask += "\n\n已锁定任务书与推进记录：\n" + material
                _plan_parts.extend(parts)
            elif _brief:
                _ptask += "\n\n" + _brief
                _plan_parts.append(("agreement", _brief))
            if recent_dialogue:
                excerpt = "\n\n近期公开对话：\n" + json.dumps(recent_dialogue, ensure_ascii=False)
                _ptask += excerpt
                _plan_parts.append(("dialogue", excerpt))
            self.plan = _plan_first(_ptask, self.session, parts=_plan_parts)
            if self.plan:
                direction = "\n\n规划者给的方向（以用户要求为准）：\n" + self.plan
                baseline_receipts = scoped_roles.check_handoff(self.root, self.session, "planner")
                if baseline_receipts:
                    direction += ("\n\n规划阶段基线回执（匹配当前项目版本且能回答同一问题时复用；"
                                  "新版本的定点验证和独立复核另取回执）：\n"
                                  + json.dumps(baseline_receipts, ensure_ascii=False))
                if self.messages:
                    advice = direction.strip()
                    if followup is not None:
                        self.messages.insert(self.messages.index(followup),
                                            Msg("assistant", advice, context_parts=(("plan", advice),)))
                    else:
                        self.messages.append(Msg("user", advice, context_parts=(("plan", advice),)))
                else:
                    task_agent += direction
                    task_parts.append(("plan", direction))
        if _brief:
            if self.messages:
                self.messages.append(Msg("user", _brief, context_parts=(("agreement", _brief),)))
            else:
                task_agent += "\n\n" + _brief
                task_parts.append(("agreement", _brief))
        self.session.save(roleWait=None)
        if self.config and self.config["crew"] == "l0":
            system += (
                 "\n本轮由你负责从需求到验证。多步任务先概括下一步，并在新证据出现时调整。"
                 "把独立的搜索和读取放在同一批，按文件版本依次修改。"
                 "如需最小复现，用 publish_artifact 保存文本脚本，再用 run_project_check 在隔离副本运行。"
                 "用户给出明确的必验要求时，用 inspect_delivery 获取 revision，"
                 "用 record_delivery_evidence 登记 operation=requirement、required=true、text 和 target（kind 与 id）。"
                 "检查完成后登记 operation=coverage、kind=automatic、requirementId、当前 target、"
                 "真实 actionId、note 和 expectedRevision。缺少证据须如实报告未验证，不能声称通过。"
                 "修改后交付前再次用 inspect_delivery 检查 requiredRebind 与 requiredPending。"
                 "旧必验项沿用原 id，在最新目标版本跑检查后重新绑定；相似的非必验项不能替代。"
                 "收尾说明具体改动、当前版本的检查证据和未覆盖项。检查回执只证明实际运行的范围。")
            system = resume_requirements(self.session, self.task_input, system, None, self.messages)
        return system, task_agent, task_parts

    # 经 `loop.py` 执行主任务，已启用的复核监听按当前运行接收过程。
    def run(self, task, provider, registry, verify, **options):
        return LOOP.run(task, provider, registry, verify,
                        peer=self.live.drain if self.live is not None else None,
                        handoff_notice_at=self.handoff_at,
                        defer_outcome=bool(self.config and not self.args.no_benni),
                        executor_only=bool(self.config and self.config['crew'] == 'l0'),
                        **options)

    # 关闭本轮持有的后台监听并输出其已观察统计。
    def close(self):
        if self.live is not None:
            self.live.stop()
            stats = self.live.stats()
            print("测试者（并行）：看了 %d 次，指出 %d 处（动作 %d 条%s）"
                  % (stats['calls'], stats['notes'], stats['actions'],
                     '，已到成本上限' if stats['capped'] else ''))

    def _review(self, r, trace, out, recoverable, *, retest=None):
        if self.controller and recoverable:
            # A killed reviewer can resume from this completed executor handoff.
            receipts = self.recovery["receipts"] if self.resume_review else ()
            self.session.save(roleWait=self._role_checkpoint("reviewer", receipts),
                              waitQuestion=False, waitConflictId=None)
        return _benni(trace, out.parent, self.plan_text, str(r.get("final") or ""), self.session,
                      ctx=int(self.cfg.get("context_limit") or 0), root=self.root,
                      user_task=self.display or self.args.say or self.task, contract_dir=self.contract_dir,
                      dialogue=self.recent_dialogue, agreement=self.controller.get("contract"), retest=retest)

    # 综合本轮输出、实际变更、复核状态和作用域检查，再由会话运行入口决定收尾。
    def finish_review(self, r, trace, out, changes, scope_bad, run_execution,
                      scope_violations, stop_hooks):
        ok = r['status'] == 'success' and out.exists()
        from . import snapshot as _SNAP
        if getattr(self.args, "no_benni", False):
            try:
                self.session.emit("benni", ok=False, skipped=True, skipKind="role_not_requested",
                           reason="本轮档位未启用测试者")
            except Exception:
                pass
            print("测试者：本轮档位未启用")
        else:
            # ★ 它在**等人回答**时不跑复核（2026-10-30 用户：「agent 停下来问人，
            #   整个 loop 都应该停住，但依旧又跑完了流程」）。
            #   原来这里只打印一句"停下来问你"，然后**照样跑复核** ——
            #   于是人还没回答，测试者已经替他把这一轮"复核完"了。
            #   而"还没做完"和"做完了要复核"是两件事。
            if r["status"] in ("needs_human", "paused"):
                try:
                    self.session.emit("benni", ok=False, skipped=True,
                               reason=("它在等你回答：还没做完，谈不上复核"
                                       if r["status"] == "needs_human" else
                                       "已在执行步边界暂停：先保存进度，不继续跑测试者"))
                except Exception:
                    pass
                print("测试者：跳过（%s）" % ("它在等人回答" if r["status"] == "needs_human"
                                          else "已在执行步边界暂停"))
            elif not self.resume_review and _skip_reviewer_for_simple_chat(
                E.binding(self.root) if self.config is not None else None,
                r, self.contract_dir, self.args.min_reads, self.args.min_chars, changes,
            ):
                _why = "本轮仅为无工具、无改动的对话；没有独立可核查的测试内容"
                self.session.emit("role_phase", role="reviewer", attempt_id=uuid.uuid4().hex,
                           state="skipped", skipKind="no_checkable_work", reason=_why)
                self.session.emit("benni", ok=False, skipped=True, skipKind="no_checkable_work", reason=_why)
                print("测试者：跳过（%s）" % _why)
            else:
                self.plan_text = self.recovery.get("plan", "") if self.resume_review else self.plan
                if not self.plan_text and ((self.args.plan_first and not self.plan_now) or self.contract_dir):
                    self.plan_text = _plan_text(str(self.contract_dir or ""), self.session.dir)
                _bstatus = self._review(r, trace, out, ok and not scope_bad)
                repair_turns = self.args.max_turns - r["turns"] if self.args.max_turns else 0
                if (isinstance(_bstatus, dict) and _bstatus["verdict"] == "issues"
                        and run_execution and r["status"] == "success" and not scope_bad
                        and (not self.args.max_turns or repair_turns > 0)
                        and not self.watch() and scoped_roles.candidate_versions(self.root) == _bstatus["versions"]):
                    retest = dict(candidateId=_bstatus["candidateId"], versions=_bstatus["versions"],
                                  findings=_bstatus["findings"],
                                  checks=[argv for item in _bstatus["findings"] for argv in item["checks"]])
                    if self.controller:
                        self.session.save(roleWait=None)
                    self.session.emit("review_repair", role="executor", candidateId=retest["candidateId"],
                               state="running", findings=retest["findings"])
                    messages = LOOP.repair_msgs([Msg.load(item) for item in self.session.load()["msgs"]])
                    messages.append(Msg("user", "系统转交测试者在上一候选发现的问题。核对证据后，只修复确认的缺陷；"
                                        "保持原要求和已授权范围。完成后交给测试者复验。\n"
                                        + json.dumps(retest["findings"], ensure_ascii=False)))
                    original_turns = r["turns"]
                    r = run_execution(messages, repair_turns)
                    r["turns"] += original_turns
                    ok = r["status"] == "success"
                    self.session.emit("review_repair", role="executor", candidateId=retest["candidateId"],
                               state=r["status"])
                    changes = _SNAP.changes(self.root, self.session.dir)
                    if changes.get("ok"):
                        (self.session.dir / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2),
                                                                encoding="utf-8")
                        scope_bad = scope_violations(changes, self.args.scope, self.root)
                        self.session.emit("changes", **{kind: [item["path"] for item in changes[kind]][:60]
                                                  for kind in ("changed", "added", "removed")},
                                   scanned=changes.get("scanned"))
                    if ok and not scope_bad:
                        _bstatus = self._review(r, trace, out, True, retest=retest)
                if isinstance(_bstatus, RoleWaiting):
                    r.update(status="needs_human", why=str(_bstatus))
                    self.save_role_stop("reviewer", r["status"], r["why"], _bstatus.receipts,
                                   _bstatus.question, _bstatus.conflict_id)
                    ok = False
                elif _bstatus == "paused":
                    r.update(status="paused", why="测试者当前执行步已完成，进度已存档")
                    self.save_role_stop("reviewer", r["status"], r["why"])
                    ok = False
                elif self.resume_review and _bstatus in ("failed", "timed_out"):
                    r.update(status="paused", why="恢复测试时未取得结论，保留原执行结果")
                    self.save_role_stop("reviewer", r["status"], r["why"], self.recovery["receipts"])
                    ok = False
                elif self.resume_review:
                    self.session.save(status="success", why=r["why"], roleWait=None, waitQuestion=False, waitConflictId=None)
                    self.session.emit("end", role="reviewer", status="success", why=r["why"])
        if run_execution and self.config and not self.args.no_benni:
            state = dict(status=r["status"], why=r["why"], turns=r["turns"])
            if self.controller and r["status"] not in {"paused", "needs_human"}:
                state.update(roleWait=None, waitQuestion=False, waitConflictId=None)
            self.session.save(**state)
            self.session.emit("end", status=r["status"], why=r["why"], turns=r["turns"], settledRoles=True)
            stop_hooks(r)
        return r, ok, scope_bad
