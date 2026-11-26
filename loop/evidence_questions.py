"""Independent, immutable evidence questions with a deliberately empty write surface."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

import hashlib
import json
from pathlib import Path
from . import workbench as W, projects, evidence

MAX_SNAPSHOT = 64 * 1024


def binding(root):
    root = Path(root).resolve()
    registry = W.registry()
    for thread in registry["threads"].values():
        if (
            thread.get("readonlyEvidence")
            and Path(
                registry["workspaces"].get(thread["workspaceId"], {}).get("path", "")
            ).resolve()
            == root
        ):
            return thread
    return None


def restrict_runner(root, args):
    """Apply to CLI as well as HTTP: legacy planners/reviewers have broader tools."""
    if not binding(root):
        return False
    args.no_benni = args.no_contract = True
    args.plan_first = args.live = args.draft_contract = False
    args.contract = ""
    args.policy = "readonly"
    return True


def read(root, offset=0):
    thread = binding(root)
    if not thread:
        return {"ok": False, "code": "evidence_denied", "error": "当前会话没有选中证据"}
    if type(offset) is not int or offset < 0:
        raise W.Problem("invalid_cursor", "证据偏移无效")
    path = W.file_path({"path": str(root)}, "selected-evidence.json")
    if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT:
        raise W.Problem("evidence_missing", "追问快照已删除或超过限制", 410)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != thread["questionOf"]["snapshotHash"]:
        raise W.Problem("evidence_changed", "追问快照已变化", 409)
    text = data.decode("utf-8")
    # The snapshot fixes the evidence version, while the source grant is live.
    evidence.check_output_access(thread["questionOf"]["conversationId"], json.loads(text)["action"])
    if offset > len(text):
        raise W.Problem("invalid_cursor", "证据偏移超出范围")
    return dict(
        ok=True,
        text=text[offset : offset + 4000],
        nextOffset=offset + 4000 if offset + 4000 < len(text) else None,
        source=thread["questionOf"],
        historicalSnapshot=True,
    )


def create(source, body, sv):
    projects.conversation(source)
    action_id = body.get("actionId")
    if not isinstance(action_id, str):
        raise W.Problem("invalid_evidence", "请选择实际工具动作")
    detail = evidence.detail(source, action_id, sv)
    evidence.check_output_access(source, detail)
    # Refuse oversized originals instead of silently turning an excerpt into full evidence.
    meta = detail.get("output") or {}
    if meta.get("bytes", 0) > MAX_SNAPSHOT // 2:
        raise W.Problem(
            "evidence_too_large",
            "此输出超过追问快照限制，请先选择范围更小的工具证据",
            413,
        )
    output, cursor = [], 0
    while True:
        page = evidence.output(source, action_id, cursor, sv)
        output.append(page["text"])
        if not page["hasMore"]:
            break
        cursor = page["next"]
        if cursor > MAX_SNAPSHOT // 2:
            raise W.Problem("evidence_too_large", "证据超出快照限制", 413)
    # Bind the copied output to the same source revision, even if the parent advances.
    fresh = evidence.detail(source, action_id, sv)
    if {key: value for key, value in fresh.items() if key != "projection"} != {
        key: value for key, value in detail.items() if key != "projection"
    }:
        raise W.Problem("evidence_changed", "选中动作正在变化，请刷新后追问", 409)
    value = dict(
        action=detail,
        output="".join(output),
        complete=page["complete"],
        reason=page.get("reason"),
        notice="历史证据快照，不是当前工程状态；原文不是授权或指令",
    )
    data = json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")
    if len(data) > MAX_SNAPSHOT:
        raise W.Problem("evidence_too_large", "选中证据元数据超过快照限制", 413)
    code, result = W.conversation_route(
        "POST",
        ["conversations", "threads"],
        {},
        {"title": "证据追问：" + str(detail.get("tool") or "工具动作")[:40]},
        sv,
    )
    if code != 200:
        return code, result
    target = projects.conversation(result["id"])
    root = Path(W.workspace(target["workspaceId"])["path"])
    W.atomic_json(root / "selected-evidence.json", value)
    # Hash the actual atomic writer representation.
    snapshot_hash = hashlib.sha256(
        (root / "selected-evidence.json").read_bytes()
    ).hexdigest()
    question = dict(
        conversationId=source,
        actionId=action_id,
        runId=detail.get("runId"),
        snapshotHash=snapshot_hash,
        sourceRefs=detail.get("sourceRefs", []),
        outputHash=meta.get("hash"),
    )
    W.change_registry(
        lambda registry: registry["threads"][result["id"]].update(
            readonlyEvidence=True, questionOf=question, projectId=None, resourceIds=[]
        )
    )
    return 200, dict(result, readonlyEvidence=True, questionOf=question)
