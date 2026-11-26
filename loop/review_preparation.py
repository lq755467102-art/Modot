"""One bounded early preparation attempt using the existing reviewer queue/tools.

The model chooses commands and whether to reuse an environment. Preparation
records observations; it cannot submit a candidate verdict or create defects.
"""
# 主要引用方包括 `executor_workflow.py`。
import json
from pathlib import Path
import time
import uuid

from . import change_history as H, evidence, scoped_roles, workbench as W
from .filelock import FileLock
from .provider import Interrupted
from .tools import Registry, reset_call, set_call, set_stop


# 执行者可在正式候选产生前，为指定资源启动有限的只读复核准备。
def start(owner, resource_id):
    if getattr(owner, "serial_review", False):
        return
    if not owner.executor_led or not owner.background_reviewer or owner._review_stop():
        return
    with owner.lock:
        records = owner.state.setdefault("reviewPreparation", {})
        previous = records.get(resource_id)
        if previous and previous.get("runId") == owner.session.run_id or owner.state["candidates"]:
            return
        row = dict(id="preparation-" + uuid.uuid4().hex, resourceId=resource_id, runId=owner.session.run_id,
                   state="queued", checks={}, criteria=[], attempts=1, reason="提前准备测试入口、用例和可复用材料")
        records[resource_id] = row
        owner.save()
    # The main writer never waits. A candidate gets priority at the next
    # preparation model/tool boundary; no second simultaneous reviewer LLM.
    owner.queue.submit(run, owner, row)


# 以当前检出版本和 `scoped_roles.py` 的时限执行预读。
# 正式候选出现即结束准备，结果仅作后续审查材料。
def run(owner, row):
    until = time.monotonic() + min(90, scoped_roles.review_seconds())
    if owner.review_deadline is not None:
        until = min(until, owner.review_deadline)

    # 准备时窗、执行者停止和正式候选生成共用结束条件。
    def stopped():
        return owner._review_stop() or time.monotonic() >= until or bool(owner.state["candidates"])

    # 给准备回执区分关闭、候选接管和时限到达。
    def stop_reason():
        if owner._review_stop():
            return owner.stop_reason()
        if owner.state['candidates']:
            return dict(kind='candidate_ready', text='候选已到达，结束提前准备并交接已有材料')
        return dict(kind='preparation_deadline', text='提前准备窗口结束，保留已有材料与回执')

    token = set_call(owner._identity("reviewer", "prepare_review_environment"))
    prior_stop = set_stop(stopped)
    try:
        if stopped():
            raise Interrupted("候选已到达或准备窗口结束")
        _, _, ws, _ = owner._resource(row["resourceId"])
        with FileLock(W.writer_lock_path(ws["path"]), check=lambda: owner._ensure_running()):
            owner._resource(row["resourceId"])
            with H.database() as db:
                snapshot_id, manifest = H.snapshot(db, ws, check=lambda: owner._ensure_running())
        with owner.lock:
            row.update(state="running", snapshot=snapshot_id, sourcePath=ws["path"], revision=manifest["workspaceRevision"])
            owner.save()
        tools, _ = owner._review_tools(row, None, manifest=manifest, check_until=until - 5)
        allowed = {"inspect_code", "search_project_code", "read_task_requirements", "read_conversation_dialogue",
                   "read_memory_tree", "run_project_check", "read_check_output", "publish_review_fixture", "review_test_environment"}
        registry = Registry()
        for schema in tools.schemas():
            if schema["name"] in allowed:
                registry.add(tools.get(schema["name"]))
        prompt = ("提前准备测试，尚未进入候选验收。工程背景：\n" + json.dumps(owner.task_brief(), ensure_ascii=False)
                  + "\n当前可读的是派发时冻结源码。自行选择读取配置、最小探测、准备用例；不要假定执行者预期正确。"
                  "优先查看已有环境，是否复用由你判断；environment_id=new 可新建，返回的 process.environmentId 用于后续复用。"
                  "可复用文件写入 os.environ['MODOT_TEST_ENV']，源码区和临时目录会复位。"
                  "准备结果供后续候选使用，不证明候选通过。已有足够材料时直接交接准备说明和缺口，避免重复探测。")
        text = scoped_roles.run(owner.root, owner._role_session(), "reviewer", prompt, tools=registry,
                                should_stop=stopped, stop_reason=stop_reason, preparing=True, review_limit=max(1, int(until - time.monotonic())),
                                binding_fields=dict(preparationId=row["id"]))
        with owner.lock:
            row.update(state="completed", text=evidence.redact(text)[:4000])
    except Interrupted:
        with owner.lock:
            cause = stop_reason()
            row.update(state="interrupted", text=cause['text'], stopKind=cause['kind'])
    except Exception as error:
        with owner.lock:
            row.update(state="failed", text=evidence.redact(str(error))[:600])
    finally:
        with owner.lock:
            owner.save()
        owner.session.emit("review_preparation", resourceId=row["resourceId"], state=row["state"], preparationId=row["id"])
        reset_call(token)
        set_stop(prior_stop)
