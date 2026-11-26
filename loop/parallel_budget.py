"""Durable reservations in the original run's logical-request ledger.

One root lock serializes parent and child charges. Unknown children retain their
reservation; transport retries remain outside this logical-request accounting.
"""

# 主要引用方包括 `subtasks.py` 和 `execution_policy.py`。

import json
from pathlib import Path
from .filelock import FileLock, atomic_json


def _read(target):
    target = Path(target)
    if target.stat().st_size > 32768:
        raise ValueError("budget ledger exceeds bound")
    return json.loads(target.read_text(encoding="utf8"))


def _review_remaining(row):
    return max(0, row.get("reviewerReserve", 0) - row["byRole"].get("reviewer", 0))


def _remaining(row):
    if not row["config"]["modelCalls"]:
        return float("inf")
    reserved = sum(
        a["limit"] - a["used"]
        for a in row.get("allocations", {}).values()
        if not a["terminal"]
    )
    return row["config"]["modelCalls"] - row["usedCalls"] - reserved - _review_remaining(row)


def remaining(target):
    target = Path(target)
    with FileLock(target.with_suffix(".lock")):
        return _remaining(_read(target))


def reserve(target, task_id, calls):
    from . import workbench as W

    target = Path(target)
    with FileLock(target.with_suffix(".lock")):
        row = _read(target)
        allocations = row.setdefault("allocations", {})
        if task_id in allocations:
            if allocations[task_id]["limit"] != calls:
                raise W.Problem("budget_changed", "子任务额度已分配，不能改写", 409)
            return
        if (
            type(calls) is not int
            or not 0 <= calls <= 1000000
            or calls == 0 and row["config"]["modelCalls"] != 0
            or len(allocations) >= 8
            or calls > _remaining(row)
        ):
            raise W.Problem(
                "insufficient_budget", "父轮次剩余额度不足或子任务超过8项", 409
            )
        allocations[task_id] = dict(limit=calls, used=0, terminal=False)
        atomic_json(target, row)


def consume(target, role, task_id=None, *, review_unit=False, defect_retest=False):
    from .execution_policy import BudgetExhausted

    target = Path(target)
    with FileLock(target.with_suffix(".lock")):
        row = _read(target)
        allocation = row.get("allocations", {}).get(task_id) if task_id else None
        if task_id and (
            not allocation
            or allocation["terminal"]
            or allocation["limit"] and allocation["used"] >= allocation["limit"]
        ):
            raise BudgetExhausted("子任务分配额度耗尽或已结束")
        reviewing = not task_id and role == "reviewer" and row.get("reviewerReserve", 0) > 0
        remaining = _review_remaining(row) if reviewing else _remaining(row)
        if reviewing and defect_retest and not review_unit and remaining == 0:
            # A repaired candidate still needs independent evidence. Use spare
            # parent calls without taking reservations from children.
            remaining = _remaining(row)
        if review_unit and remaining <= 4:
            raise BudgetExhausted("剩余复核调用保留给交付验收")
        if (not task_id and remaining <= 0) or (row["config"]["modelCalls"] and row["usedCalls"] >= row["config"]["modelCalls"]):
            reason = "本轮额度已使用或保留给子任务，不自动补充"
            if reviewing:
                reason = "测试者独立额度已用完，复核未完成"
            elif not task_id and _review_remaining(row):
                reason = "规划和执行可用额度已用完，剩余调用保留给测试者"
            row.update(state="exhausted", exhaustedRole=role)
            atomic_json(target, row)
            raise BudgetExhausted(reason)
        if allocation:
            allocation["used"] += 1
        key = f"child:{task_id}:{role}" if task_id else role
        row["usedCalls"] += 1
        row["byRole"][key] = row["byRole"].get(key, 0) + 1
        atomic_json(target, row)
        return row


def release(target, task_id, confirmed):
    if not confirmed:
        return
    target = Path(target)
    with FileLock(target.with_suffix(".lock")):
        row = _read(target)
        row["allocations"][task_id]["terminal"] = True
        atomic_json(target, row)


def reopen(target, task_id):
    """Explicit same-delegation resume. Re-reserve unused calls, never reset use."""
    from . import workbench as W
    target=Path(target)
    with FileLock(target.with_suffix('.lock')):
        row=_read(target);allocation=row.get('allocations',{}).get(task_id)
        if not allocation:
            raise W.Problem('budget_unknown','原分配记录缺失，不能重置额度',409)
        if allocation['limit'] and allocation['used']>=allocation['limit']:
            raise W.Problem('insufficient_budget','此委派额度已耗尽',409)
        if allocation['terminal'] and allocation['limit']-allocation['used']>_remaining(row):
            raise W.Problem('insufficient_budget','已释放额度已被使用，未重置计数',409)
        allocation['terminal']=False
        atomic_json(target,row)


def unspent(target, task_id, run_id):
    """Consult the parent ledger, which is charged before the child's ledger."""
    target = Path(target)
    with FileLock(target.with_suffix(".lock")):
        row = _read(target)
        allocation = row.get("allocations", {}).get(task_id)
        return row.get("runId") == run_id and bool(allocation and allocation.get("used") == 0)
