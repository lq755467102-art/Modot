"""Read-only, bounded review projection for the selected conversation round."""

# 主要引用方包括 `evidence.py`。

import json

from . import action_review, evidence, projects
from . import workbench as W


TITLES = {
    "list_plugin_catalog": "查看可安装插件",
    "list_installed_plugins": "核对已安装插件",
    "list_plugins": "核对本轮启用的插件",
    "install_catalog_plugin": "安装插件",
    "install_local_plugin": "安装本地插件",
    "enable_plugin_components": "启用插件组件",
    "inspect_plugin_tools": "读取插件工具清单",
    "call_plugin_tool": "调用插件工具",
    "read_action_evidence": "回查原始动作证据",
    "read_project_resource": "读取项目资料",
    "read_conversation_dialogue": "回看公开对话",
    "read_memory_tree": "读取记忆树",
    "inspect_code": "检查项目代码",
    "write_project_file": "修改项目文件",
    "edit_project_file": "局部编辑项目文件",
    "undo_own_project_write": "撤销本轮自己的写入",
    "run_project_check": "运行项目检查",
    "start_application": "启动本地预览",
    "start_python_debug": "启动 Python 调试",
    "publish_artifact": "保存成果",
}
REPEATABLE = {"read_action_evidence", "read_conversation_dialogue", "read_memory_tree",
              "list_plugin_catalog", "list_installed_plugins", "list_plugins"}
ROLES = ("planner", "executor", "reviewer", "unknown")
MAX_ACTIONS = 120
HOST_METADATA = (REPEATABLE - action_review.REPLAY_TOOLS) | {
    "install_catalog_plugin", "enable_plugin_components", "inspect_plugin_tools",
}


def readable(action, allowed):
    args = action.get("input")
    if not isinstance(args, dict) or args.get("truncated"):
        return False
    ids = args.get("resource_ids", [])
    if not isinstance(ids, list):
        return False
    if "resource_id" in args:
        ids = [args["resource_id"], *ids]
    if ids:
        return all(isinstance(ident, str) and ident in allowed for ident in ids)
    return action.get("tool") in HOST_METADATA


def title(action):
    tool = action.get("tool")
    args = action.get("input")
    if not isinstance(args, dict):
        args = {}
    if tool == "install_catalog_plugin" and args.get("catalog_id") == "github":
        return "安装 GitHub 插件"
    if tool == "read_project_resource":
        kind = (action.get("impact") or {}).get("readKind")
        if label := {"directory": "列出目录", "text": "读取文件正文",
                     "document_locations": "列出文档位置",
                     "table": "读取表格行", "table_sheets": "列出工作表",
                     "video_metadata": "查看视频信息",
                     "video_frames": "视频画面已送入模型" if action.get("imageDelivery") else "读取视频画面",
                     "image": "图片已送入模型" if action.get("imageDelivery") else "读取图片"}.get(kind):
            return label
    return TITLES.get(tool, "工具操作")


def has_test_receipt(action):
    process = (action.get("impact") or {}).get("process") or {}
    return (
        action.get("role") == "reviewer"
        and action.get("tool") == "run_project_check"
        and action.get("state") in {"completed", "failed"}
        and type(process.get("exitCode")) is int
    )


def overview(ident, q, sv):
    root = evidence.directory(ident, sv)
    scope = projects.scope(ident)
    allowed = {item["id"] for item in scope["resources"] if item.get("permission") == "read"}
    requested = (q.get("run") or [""])[0]
    if len(requested) > 128:
        raise W.Problem("invalid_filter", "轮次标识过长")
    with evidence.projection(root) as (db, state):
        recent = db.execute(
            "SELECT id FROM progress_runs ORDER BY position DESC LIMIT 30"
        ).fetchall()
        choices = [row[0] for row in recent]
        runtime_id = W.RT.status(root).get("run_id")
        run_id = requested or runtime_id or (choices[0] if choices else "")
        if run_id and run_id not in choices:
            choices.insert(0, run_id)
        run_times = dict(db.execute(
            "SELECT run, MIN(json_extract(data,'$.startedAt')) FROM actions "
            "WHERE run IN (" + ",".join("?" for _ in choices) + ") GROUP BY run",
            choices,
        )) if choices else {}
        result = dict(runId=run_id, runs=choices, roles=[],
                      runTimes=run_times, actionLabels={},
                      incomplete=bool(state["catchingUp"] or state["gapCount"]
                                      or state["malformedCount"] or state["sequenceConflicts"]))
        if not run_id:
            return result
        run_row = db.execute("SELECT data FROM progress_runs WHERE id=?", (run_id,)).fetchone()
        if not run_row:
            return dict(result, incomplete=True)
        run = json.loads(run_row[0])
        rows = db.execute(
            "SELECT data FROM actions WHERE run=? ORDER BY position DESC LIMIT ?",
            (run_id, MAX_ACTIONS + 1),
        ).fetchall()
        result["incomplete"] |= len(rows) > MAX_ACTIONS
        valid = {}
        for row in reversed(rows[:MAX_ACTIONS]):
            action = json.loads(row[0])
            if action.get("sourceUnverified") or action.get("identityKind") != "action":
                result["incomplete"] = True
                continue
            try:
                evidence._verify_source(root, action, db)
            except W.Problem:
                result["incomplete"] = True
                continue
            valid[action["id"]] = action
        code_resources = action_review.allowed_resources(ident)
        for action in valid.values():
            target = action_review.target_hint(action, code_resources)
            label = title(action) + (" · " + target if target else "")
            result["actionLabels"][action["id"]] = dict(label=label, startedAt=action.get("startedAt"))
        for role in ROLES:
            actions = [a for a in valid.values() if (
                a.get("role") == role if role != "unknown" else a.get("role") not in ROLES
            )]
            notes = []
            recorded = run.get("reviewNotes", {}).get(role)
            if recorded:
                try:
                    evidence._verify_source(root, {"sourceRefs": [recorded["source"]["record"]]}, db)
                except W.Problem:
                    recorded = None
                    result["incomplete"] = True
            for note in recorded["items"] if recorded else []:
                refs = note["actions"]
                checked = bool(refs) and all(
                    ref in valid and readable(valid[ref], allowed)
                    and (role == "reviewer" or valid[ref].get("role") == role)
                    for ref in refs
                )
                independent = checked and role == "reviewer" and any(has_test_receipt(valid[ref]) for ref in refs)
                notes.append(dict(
                    text=note["text"] if checked else "角色说明缺少当前可核对的动作引用。",
                    actions=refs if checked else [],
                    basis="test" if independent else "record" if checked else "unverified",
                ))
            phase = run.get("roles", {}).get(role, {})
            if not (actions or notes or (phase.get("source") and phase.get("state") not in {"unknown", "not_run"})):
                continue
            groups = []
            for action in actions:
                tool, status = action["tool"], action["state"]
                previous = groups[-1] if groups else None
                if (tool in REPEATABLE and previous and previous["tool"] == tool
                        and previous["state"] == status and not action.get("approvalRefs")
                        and previous["repeatable"]):
                    previous["actions"].append(action["id"])
                else:
                    groups.append(dict(tool=tool, title=title(action), state=status,
                                       actions=[action["id"]], repeatable=not bool(action.get("approvalRefs"))))
            # Keep blocking results and changes visible before routine inventory reads.
            priority = sorted(range(len(groups)), key=lambda i: (
                groups[i]["state"] not in {"failed", "waiting_approval", "unknown"},
                groups[i]["tool"] in REPEATABLE,
                -i,
            ))
            featured = [groups[i] for i in sorted(priority[:3])]
            result["roles"].append(dict(
                role=role, phase=phase.get("state"),
                reason=phase.get("reason") if phase.get("state") == "skipped" or
                role == "reviewer" and phase.get("state") in {"failed", "paused", "timed_out", "waiting_human"} else None,
                count=len(actions), hiddenGroups=len(groups) - len(featured), facts=featured, notes=notes,
                independent=any(has_test_receipt(action) and readable(action, allowed) for action in actions),
                blocked=any(a.get("tool") == "read_action_evidence" and a.get("state") == "failed" for a in actions),
            ))
        if projects.scope(ident)["revision"] != scope["revision"]:
            raise W.Problem("scope_changed", "资料范围已变化，请重新核对摘要", 409)
        return result
