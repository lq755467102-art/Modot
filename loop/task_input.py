"""Route a public input before selecting any role checkpoint."""

# 主要引用方包括 `executor_workflow.py` 和 `executor_core.py`。

import re
import hashlib
import json


def requirements_hash(material):
    # Workflow bookkeeping is not a change in what the user asked for.
    semantic = {key: material[key] for key in ("taskId", "revision", "entries", "requirements") if key in material}
    return hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def effective(previous, binding, original, incoming, checkpoint):
    """Persist user requirements and answers separately from compressed chat."""
    from . import evidence
    same = binding["kind"] != "new" and previous.get("taskId") == binding["id"]
    entries = list(previous.get("entries", [])) if same else []
    if not entries:
        entries.append(dict(kind="goal", text=evidence.redact(original)))
    if incoming and binding["kind"] not in {"new", "resume"}:
        entry = dict(kind=binding["kind"], text=evidence.redact(incoming))
        if checkpoint.get("waitQuestion"):
            entry["question"] = evidence.redact(checkpoint["waitQuestion"])
        entries.append(entry)
    material = dict(taskId=binding["id"], revision=binding["revision"], entries=entries)
    # 同一逻辑任务保留已整理要求；新增输入另标 normalizationPending，恢复本身不生成新要求。
    if same:
        for key in ("requirements", "normalizationHistory", "normalizedAt"):
            if key in previous:
                material[key] = previous[key]
        material["normalizationPending"] = (previous.get("normalizationPending", False)
            if binding["kind"] == "resume" else bool(incoming))
    return dict(material, contentHash=requirements_hash(material))


def effective_text(material):
    history = "\n\n".join("[" + entry["kind"] + "]\n" +
        ("待答问题：" + str(entry["question"]) + "\n" if entry.get("question") else "") + entry["text"]
        for entry in material["entries"])
    # 当前有效/撤销要求先于历史来源展示，待整理的新输入也保留，避免旧计划重新变成当前指令。
    if material.get("requirements"):
        active = "\n".join(i["id"] + ": " + i["text"] for i in material["requirements"] if i["status"] == "active")
        withdrawn = "\n".join(i["id"] + ": " + i["text"] for i in material["requirements"] if i["status"] == "withdrawn")
        pending = ("\n新增输入尚待整理，须同时满足或据明确更正更新以上义务：\n" + material["entries"][-1]["text"]
                   if material.get("normalizationPending") else "")
        return "当前有效要求：\n" + active + "\n已撤销（不再执行）：\n" + withdrawn + pending + "\n历史来源（不得重新当作当前指令）：\n" + history
    return history

def requirements_page(material, after=0):
    """One plain-text view for native and legacy readers; stored JSON stays intact."""
    from .workbench import Problem
    material = material or {}
    text = effective_text(dict(material, entries=material.get('entries') or []))
    if type(after) is not int or not 0 <= after <= len(text):
        raise Problem('invalid_cursor', '需求读取位置无效')
    end = min(after + 12000, len(text))
    return dict(ok=True, text=text[after:end], next=end, hasMore=end < len(text),
                taskId=material.get('taskId'), revision=material.get('revision'),
                contentHash=material.get('contentHash') or requirements_hash(material),
                entryCount=len(material.get('entries') or []))


AMENDMENT = r"(?:补充|更正|amend)\s*[：:]"
CONTINUATION = re.compile(
    r"(?:(?:继续|恢复)(?:上一(?:个)?任务|上次任务|上一轮|上轮任务)?|continue|resume)"
    r"(?:\s*[,，:：]\s*(.*))?", re.I | re.S)
NEW_TASK = re.compile(r"(?:新任务|new task|换(?:一?个)?(?:任务|话题)|换题)(?:\s*[,，:：]|$)", re.I)


def split_answer(text):
    match = re.search(r"\n\s*" + AMENDMENT, text, re.I)
    if not match:
        return text, None
    return text[:match.start()].strip(), text[match.end():].strip()


def _goal_positions(messages, goal):
    if not goal:
        return []
    positions = []
    for index, message in enumerate(messages):
        text = message.get("content")
        if message.get("role") == "user" and isinstance(text, str) and (
                text == goal or any(text[left:right] in (goal, "[goal]\n" + goal)
                                    for name, left, right in message.get("context_spans", ())
                                    if name in ("task", "dialogue"))):
            positions.append(index)
    return positions


def _logged_start(directory, logical_run, messages):
    from . import event_index, evidence
    if not logical_run or directory is None or not (directory/'events.jsonl').is_file():
        return None
    with evidence.projection(directory) as (db, _):
        row = db.execute('SELECT data FROM progress_runs WHERE id=?', (logical_run,)).fetchone()
        ref = json.loads(row[0]).get('goalSource', {}).get('record', {}) if row else {}
    if 'offset' not in ref:
        return None
    rows, _, _ = event_index.page(directory/'events.jsonl', after_position=ref['offset']-1, limit=128)
    if (not rows or rows[0].get('sourceOffset') != ref['offset'] or
            rows[0].get('run_id') != logical_run or rows[0].get('act') != 'user'):
        return None
    user = rows[0]
    goal = user.get('recordText') or (user.get('text') if not user.get('truncated') else None)
    positions = _goal_positions(messages, goal)
    if len(positions) == 1:
        return positions[0]
    # Identical user wording can start different tasks. Join the original run's
    # call ID to its owning user message instead of choosing a textual match.
    calls = {row['call_id'] for row in rows if row.get('act') == 'tool' and row.get('call_id')
             and row.get('run_id') == logical_run and not (row.get('agent_id') or row.get('childId'))}
    owners = set()
    user_position = None
    for index, message in enumerate(messages):
        if message.get('role') == 'user':
            user_position = index
        if user_position in positions and any(call.get('id') in calls for call in message.get('tool_calls') or []):
            owners.add(user_position)
    return owners.pop() if len(owners) == 1 else None


def message_start(checkpoint, kind, directory=None):
    """Index in the retained thread, independent of the model's working messages."""
    messages = checkpoint.get("msgs") or []
    if kind == "new":
        return len(messages)
    binding = checkpoint.get("taskInput") or {}
    if "messageStart" in binding:
        start = binding["messageStart"]
        if type(start) is not int or not 0 <= start <= len(messages):
            raise ValueError("当前任务的消息起点与会话记录不符")
        return start
    entries = (checkpoint.get("effectiveRequirements") or {}).get("entries") or []
    goal = next((item.get("text") for item in entries if item.get("kind") == "goal"), None)
    matches = _goal_positions(messages, goal)
    if len(matches) == 1:
        return matches[0]
    logged = _logged_start(directory, binding.get('id') or checkpoint.get('run_id'), messages)
    if logged is not None:
        return logged
    # With several potential user turns and no reliable join, expose the originals
    # through history and restore the effective requirement separately.
    turns = [i for i, msg in enumerate(messages) if msg.get('role') == 'user']
    return turns[0] if len(turns) == 1 else len(messages)


def lost_reads(checkpoint, current, directory, start):
    """Check scope before reusing messages that can quote earlier tool output."""
    from . import projects
    previous = (checkpoint.get("taskInput") or {}).get("readBindings")
    if previous is None:
        previous = projects.retained_run_reads(directory, checkpoint.get("run_id"), "executor")
    if previous is None:
        # A legacy checkpoint without a request scope can still identify a
        # resource in the original call. Unattributable outputs stay archived.
        previous = {}
        for msg in checkpoint.get("msgs", [])[start:]:
            for call in msg.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"]["arguments"])
                    ids = args.get("resource_ids", [])
                    refs = ([args["resource_id"]] if "resource_id" in args else []) + ids
                    if not refs or not all(isinstance(rid, str) for rid in refs):
                        return True
                    previous.update(dict.fromkeys(refs))
                except (ValueError, TypeError, AttributeError, KeyError):
                    return True
    return not projects.covers_reads(current, previous)


def _previous_public_input(directory, run_id, seq):
    from . import event_index
    from .context_archive import dialogue_source, public_turn

    if not run_id:
        return ""
    # Existing checkpoints predate the sequence pointer. Only inspect a bounded
    # tail for those records, and omit the quote if its source cannot be confirmed.
    cursor = (dict(after=seq - 1, before=seq + 1, limit=1)
              if type(seq) is int and seq > 0 else dict(tail=True, limit=256))
    try:
        rows, _, _ = event_index.page(directory / "events.jsonl", **cursor)
    except OSError:
        return ""
    for row in reversed(rows):
        if row.get("run_id") == run_id and row.get("act") == "user" and public_turn(row, 1):
            return dialogue_source(row)[0][:500]
    return ""


def previous_public_exchange(checkpoint, directory):
    """Return one bounded public exchange and the scope needed to see it."""
    from . import projects
    from .evidence_redaction import redact

    final = checkpoint.get("final")
    if (checkpoint.get("status") != "success" or not checkpoint.get("public_reply_persisted")
            or not isinstance(final, str) or not final.strip()):
        return None
    task = checkpoint.get("taskInput") or {}
    run_id = checkpoint.get("run_id")
    bindings = task.get("readBindings")
    if bindings is None:
        bindings = projects.retained_run_reads(directory, run_id, "executor")
    if bindings is None:
        return None
    user = _previous_public_input(directory, run_id, task.get("userMessageSeq"))
    final = redact(final)
    if len(final) > 2000:
        final = final[:800] + "\n[…]\n" + final[-1200:]
    return json.dumps({"上一条用户原话": user[:500], "上一轮公开答复": final}, ensure_ascii=False), bindings


def explicit_task_switch(text):
    text = text.strip()
    continuation = CONTINUATION.fullmatch(text)
    directions = continuation.group(1) if continuation else None
    return bool(NEW_TASK.match(directions or text))


def classify(text, checkpoint, *, steering=False):
    text = text.strip()
    if explicit_task_switch(text):
        return "new"
    if steering or re.match("^" + AMENDMENT, text, re.I):
        return "amend"
    status = checkpoint.get("status")
    # 仅有明确待答问题才把普通输入视为答复；否则继续按新任务/修订规则分类。
    if status == "needs_human" and checkpoint.get("waitQuestion"):
        if split_answer(text)[1]:
            return "answer_amend"
        return "answer"
    continuation = CONTINUATION.fullmatch(text)
    if checkpoint.get("run_id") and continuation:
        return "amend" if continuation.group(1) else "resume"
    return "new"


# 答复、修订和恢复可沿用逻辑任务身份；只有修订递增需求版本，纯恢复不重置已有要求。
def bind(previous, kind, run_id, checkpoint):
    same = kind in {"answer", "answer_amend", "amend", "resume"} and checkpoint.get("run_id")
    return {
        "id": (previous.get("id") or checkpoint["run_id"]) if same else run_id,
        "revision": (previous.get("revision", 1) + int(kind in {"answer_amend", "amend"})) if same else 1,
        "kind": kind,
        "previousRunId": checkpoint.get("run_id"),
        "previousStatus": checkpoint.get("status"),
        "questionRunId": checkpoint.get("run_id") if kind in {"answer", "answer_amend"} else None,
    }
