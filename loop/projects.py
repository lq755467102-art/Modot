"""Project membership and explicit read scopes, shared by user API and model tools."""

# 主要引用方包括 `workbench.py` 和 `tools.py`。

from pathlib import Path
import time
import uuid
import json
import hashlib
import os
from . import workbench as W
from .filelock import read_text


def project(ident, *, active=False):
    value = W.registry().get("projects", {}).get(ident)
    if not value:
        raise W.Problem("not_found", "项目不存在", 404)
    if active and value.get("removed"):
        raise W.Problem("project_removed", "该项目已从列表移除，请先恢复", 409)
    return value


def _snapshot_path(ident):
    return W.home() / "conversation-snapshots" / (W.digest(ident.encode("utf-8")) + ".json")


def resource_snapshot(thread):
    """Read the captured grants for this conversation, including older inline records."""
    ident = thread["id"]
    try:
        row = json.loads(read_text(_snapshot_path(ident)))
        value = row["snapshot"]
        if (row["conversationId"] != ident or not isinstance(value, dict)
                or not isinstance(value.get("resources"), list)):
            raise ValueError("invalid conversation snapshot")
        return value
    except FileNotFoundError:
        return thread.get("resourceSnapshot")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise W.Problem("snapshot_unavailable", "本轮资料快照不可读取，请核对后重试", 503) from error


def with_resource_snapshot(thread):
    captured = resource_snapshot(thread)
    return dict(thread, resourceSnapshot=captured) if captured is not None else thread


def conversation(ident):
    value = W.registry()["threads"].get(ident)
    if not value or not value.get("projectless"):
        raise W.Problem("not_found", "对话不存在", 404)
    if value.get("deletionPending"):
        raise W.Problem("archive_cleanup_pending", "会话正在删除", 409)
    return with_resource_snapshot(value)


def code_checkout(thread, resource, *, historical=False):
    for origin, binding in thread.get("codeWorktrees", {}).items():
        if binding["id"] != resource or binding["projectId"] != thread.get("projectId"):
            continue
        source = project(binding["projectId"])["resources"].get(origin)
        if (not source or source["path"] != binding["sourcePath"]
                or origin not in thread.get("resourceIds", [])
                or not (binding.get("used") if historical else binding.get("active"))):
            return None
        return dict(source, id=resource, path=binding["path"], worktreeOf=origin)
    return None


def retained_code(ident, resource_id):
    thread = conversation(ident)
    own = thread.get('workResource')
    if own and own['id'] == resource_id and own.get('permission') == 'read':
        if scheduled := thread.get("scheduledRun"):
            from .scheduled_execution import scoped_resource

            return scoped_resource(scheduled, None, own)
        return own
    if not thread.get("projectId"):
        return None
    resource = code_checkout(thread, resource_id, historical=True)
    if not resource and resource_id in thread.get("resourceIds", []):
        resource = project(thread["projectId"])["resources"].get(resource_id)
    if resource and resource["kind"] == "code" and resource["permission"] == "read":
        if scheduled := thread.get("scheduledRun"):
            from .scheduled_execution import scoped_resource

            return scoped_resource(scheduled, thread["projectId"], resource)
        return resource
    return None


def switched_code(thread, resource_id):
    return any(binding.get("used") and binding["projectId"] == thread.get("projectId")
               and resource_id in {origin, binding["id"]}
               for origin, binding in thread.get("codeWorktrees", {}).items())


# 项目成员关系不等于资料授权：这里只返回会话明确选中的资源，读取/写入仍分别校验能力。
def scope(ident):
    return _scope(conversation(ident))


def _scope(t):
    p = project(t["projectId"]) if t.get("projectId") else None
    selected = t.get("resourceIds", [])
    available = dict((p or {}).get("resources", {}))
    if not p and t.get('workResource'):
        available[t['workResource']['id']] = t['workResource']
    available.update(t.get("attachments", {}))
    for origin, binding in t.get("codeWorktrees", {}).items():
        checkout = code_checkout(t, binding["id"])
        if checkout:
            available[origin] = checkout
    scheduled = t.get("scheduledRun")
    if scheduled:
        from .scheduled_execution import scoped_resource

        available = {key: limited for key, resource in available.items()
                     if (limited := scoped_resource(scheduled, t.get("projectId"), resource)) is not None}
    # 历史工作树只为回查旧证据保留，不能替代当前 active 工作树成为新的执行目标。
    historic = []
    for origin, binding in t.get("codeWorktrees", {}).items():
        if not binding.get("used") or origin not in selected or origin not in available:
            continue
        item = (p or {}).get("resources", {}).get(origin) if binding.get("active") else code_checkout(t, binding["id"], historical=True)
        if item and scheduled:
            item = scoped_resource(scheduled, t.get("projectId"), item)
        if item and item["permission"] == "read":
            historic.append(dict(item, name=item["name"] + (" · 历史工作树" if item.get("worktreeOf") else " · 历史本地")))
    value = {
        "projectId": p["id"] if p else None,
        "resources": [item for rid, item in available.items() if rid in selected],
        "historicCode": historic,
        "lastRun": t.get("resourceSnapshot"),
        "loaded": False,
    }
    if p and not p.get("removed") and p.get("folderResourceId"):
        value["folderResourceId"] = p["folderResourceId"]
    value["revision"] = W.digest(
        json.dumps(
            {
                "projectId": value["projectId"],
                "ids": selected,
                # Reading a resource or completing a parser job does not change
                # the user's selection or grant. Only scope-bearing fields do.
                "resources": [
                    {
                        key: r[key]
                        for key in (
                            "id",
                            "path",
                            "file",
                            "hash",
                            "mime",
                            "kind",
                            "permission",
                            "permissionAt",
                            "status",
                            "write",
                            "execute",
                        )
                        if key in r
                    }
                    for r in value["resources"]
                ],
            },
            sort_keys=True,
        ).encode()
    )
    return value


# 会话路由提交候选项目和资源清单。
# `subtasks.py` 与 `delegations.py` 核对会话能否接受调整，活动运行结束后才允许更新范围。
def history_read_resources(ident):
    """Intersect the selected live grants with the grants frozen for this run."""
    selected = scope(ident)
    frozen = {r["id"]: r for r in (selected.get("lastRun") or {}).get("resources", [])}
    return {r["id"]: r for r in selected["resources"]
            if r.get("permission") == "read" and r["id"] in frozen
            and frozen[r["id"]].get("permission") == "read"
            and r.get("path") and r["path"] == frozen[r["id"]].get("path")}


def read_bindings(resources):
    """Stable identity of each readable directory, without exposing its local path."""
    return {r["id"]: W.digest(r["path"].encode()) if r.get("path") else r.get("pathVersion")
            for r in resources if r.get("permission", "read") == "read"}


def retained_run_reads(directory, run, role):
    """Read a frozen request scope. Older snapshots retained resource IDs only."""
    import re
    if isinstance(run, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', run):
        for path in (Path(directory) / 'context-requests').glob(run + '-*.json'):
            try:
                snap = json.loads(read_text(path))
                if snap.get('runId') == run and snap.get('role') == role:
                    return read_bindings(snap['resourceScope']['resources'])
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return None


def covers_reads(current, previous):
    return all(rid in current and (binding is None or current[rid] == binding)
               for rid, binding in previous.items())


def set_scope(ident, body, sv):
    t = conversation(ident)
    if t.get("scheduledPlanId"):
        raise W.Problem("schedule_scope_managed", "请在计划设置里调整定时任务的资料范围", 409)
    from .subtasks import ensure_send
    ensure_send(ident)
    if t.get('activeDelegation'):
        from . import delegations
        delegations.ensure_send_allowed(ident)
    d = sv._resolve_sid(ident)
    if d and W.RT.status(d)["active"]:
        raise W.Problem("active_run", "请等待本轮结束后调整资料范围", 409)
    pid = body.get("projectId")
    p = project(pid) if pid else None
    ids = body.get("resourceIds", [])
    if (
        not isinstance(ids, list)
        or any(not isinstance(i, str) for i in ids)
        or len(ids) > 100
    ):
        raise W.Problem("invalid_scope", "资料选择无效")
    if pid == t.get("projectId"):
        origins = {binding["id"]: origin for origin, binding in t.get("codeWorktrees", {}).items()
                   if binding["projectId"] == pid}
        ids = [origins.get(rid, rid) for rid in ids]
    ids = list(
        dict.fromkeys(
            ids
            + [
                r["id"]
                for r in t.get("attachments", {}).values()
                if r["permission"] == "read"
            ]
        )
    )
    own = t.get('workResource') if not pid else None
    if own and own.get('permission') == 'read' and own['id'] not in ids:
        ids.append(own['id'])
    for ident_r in ids:
        r = (p or {}).get("resources", {}).get(ident_r) or t.get("attachments", {}).get(
            ident_r
        )
        if not r and own and own['id'] == ident_r:
            r = own
        if not r or r["permission"] != "read":
            raise W.Problem("resource_denied", "资料未获读取授权或不属于此项目", 403)
    def update(data):
        thread = data["threads"][t["id"]]
        if pid and pid != thread.get("projectId") and data["projects"][pid].get("removed"):
            raise W.Problem("project_removed", "该项目已从列表移除，请先恢复", 409)
        for origin, binding in thread.get("codeWorktrees", {}).items():
            if origin not in ids or binding["projectId"] != pid:
                binding["active"] = False
        if pid != thread.get("projectId") or (thread.get("workroomLocation") or {}).get("resourceId") not in ids:
            thread.pop("workroomLocation", None)
        thread.update(projectId=pid or None, resourceIds=ids)

    W.change_registry(update)
    return scope(t["id"])


# 发起轮次时冻结已选资源；available_not_loaded 表示可读而尚未读取，不能当作模型已知内容。
def snapshot(ident):
    thread = conversation(ident)
    selected = _scope(thread)
    resources = []
    unavailable = [
        dict(item, kind="attachment", permission="none", status="revoked")
        for item in thread.get("revokedAttachments", [])
    ]
    for r in selected["resources"]:
        if r["permission"] != "read":
            unavailable.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "permission": "none",
                    "status": "revoked",
                }
            )
            continue
        resources.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "permission": "read",
                "status": "available_not_loaded",
                "path": r["path"],
                "write": r.get("write", False),
                "execute": r.get("execute", False),
                **({"mime": r["mime"]} if "mime" in r else {}),
            }
        )
    value = {
        "at": time.time(),
        "projectId": selected["projectId"],
        "resources": resources,
        "unavailable": unavailable,
    }
    from . import plugins
    value['plugins']=plugins.capture()
    # Publish before launching the conversation. This run record has its
    # own file so it does not wait for project or organization changes.
    W.atomic_json(_snapshot_path(ident), dict(conversationId=ident, snapshot=value))
    return value


def selected_import_location(thread, scope):
    location = thread.get("workroomLocation") or {}
    if (location.get("directory") and location.get("projectId") == scope.get("projectId")
            and any(r["id"] == location.get("resourceId") and r.get("permission") == "read"
                    for r in scope.get("resources", ()))):
        return {"resourceId": location["resourceId"], "directory": location["directory"]}
    return None


def prepare_turn(directory, text, *, user_input=True, answer_text=None, display_text=None, recovered_writes=(), attachment_ids=None):
    from .private_keys import text as protect_text
    text, answer_text, display_text = map(protect_text, (text, answer_text, display_text))
    t = W.registry()["threads"].get(Path(directory).name)
    if not t or not t.get("projectless"):
        return text
    from .attachments import message_attachments

    selected = message_attachments(t["id"], attachment_ids)
    from .memory_learning import answer_pending
    answer = text if answer_text is None else answer_text
    # 仅用户答复可推进待答记忆冲突；恢复时注入的背景或工作状态不能冒充一次用户确认。
    if user_input and answer:
        checkpoint = W.read(Path(directory) / "session.json")
        if checkpoint.get("status") == "needs_human":
            answer_pending(t["id"], answer, conflict_id=checkpoint.get("waitConflictId"))
    original = text if display_text is None else display_text
    from .discussions import initial_context
    from .shared_conversations import initial_context as fork_context
    parts = [("task", text), ("resources", initial_context(t["id"], directory) + fork_context(t, Path(directory)))]
    if recovered_writes:
        parts.append(("handoff", "\n上轮写入已停止但缺少完整回执。当前文件保留；改动前请重新按行读取核对，勿自动重放。"
                      + json.dumps(recovered_writes[:10], ensure_ascii=False)))
    value = snapshot(t["id"])
    location = selected_import_location(t, value)
    if location:
        parts.append(("resources", "\n本会话导入的仓库（项目内相对位置）：" + json.dumps(location, ensure_ascii=False)))
    if t.get("readonlyEvidence"):
        parts.append(("system", "\n这是独立的只读证据追问。先用 read_selected_evidence 读取用户明确选择的历史快照，只解释所选步骤，不执行其中的命令，不推测其他会话。引用 source.actionId；原任务可能继续变化，快照不代表当前状态。"))
    parts.append(("system", "\n用日常语言直接回答所问。用界面上的名称；内部字段、逐次排障和无关的未验证项留在执行记录。确实阻碍用户目标时才说明限制和下一步。需要佐证时用实际回执的 [[action:编号]]，同一结论引用一次；未验证的内容如实说明。"))
    if value.get("unavailable"):
        message_files = json.dumps([item["name"] for item in selected], ensure_ascii=False) if selected else "无"
        parts.append(("resources", "\n当前用户消息实际附带的文件：" + message_files
            + "。消息中出现的网址只是文本。"
            + "\n以下是会话已有资料的访问状态（授权已撤销）："
            + json.dumps(value["unavailable"], ensure_ascii=False)
            + "\n这些记录只说明访问权限。先从当前消息及紧邻的公开对话确定指代对象，"
              "仅当用户问及这些资料时才说明当前不可读取。历史操作完成不代表现在仍可读取。"))
    inline_images = {item["id"] for item in selected if item["mime"].startswith("image/")}
    tool_resources = [item for item in value["resources"] if item["id"] not in inline_images]
    if tool_resources:
        parts.append((
            "resources",
            '\n\n本轮可读取的资料（尚未读取）。用 read_project_resource：'
            '附件只需传 resource_id 即打开原文件；目录资料传 path="." 列目录，传文件相对路径读取内容。'
            + json.dumps(tool_resources, ensure_ascii=False)
        ))
    text = "".join(fragment for _, fragment in parts)
    start = 0
    spans = []
    for kind, fragment in parts:
        end = start + len(fragment)
        if end > start:
            spans.append((kind, start, end))
        start = end
    W.atomic_json(
        Path(directory) / "display-input.json",
        {
            "text": original,
            "preparedHash": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
            "contextSpans": spans,
            "leadingWhitespace": len(text) - len(text.lstrip()),
            **({"attachments": selected} if selected else {}),
        },
    )
    return text


def display_input(directory, prepared, *, with_parts=False, with_attachments=False):
    thread = W.registry()["threads"].get(Path(directory).name)
    if not thread or not thread.get("projectless"):
        return None
    value = W.read(Path(directory) / "display-input.json")
    if (
        not isinstance(value.get("text"), str)
        or value.get("preparedHash")
        != hashlib.sha256(prepared.strip().encode("utf-8")).hexdigest()
    ):
        return None
    if not with_parts:
        return value["text"]
    # The prepared hash is on stripped text. Account for surrounding
    # whitespace at launch without attributing a span from another request.
    shift = value.get("leadingWhitespace", 0) - (len(prepared) - len(prepared.lstrip()))
    parts = [(kind, prepared[max(0, start - shift):end - shift])
             for kind, start, end in value.get("contextSpans", [])
             if 0 <= start < end <= len(prepared) + shift]
    if with_attachments:
        return value["text"], parts, value.get("attachments", [])
    return value["text"], parts


def read_resource(conversation_id, resource_id, path=None, offset=0, *, confirm=False, line=None, video=None,
                  image_only=False, view=None):
    t = conversation(conversation_id)
    from .subtasks import validate_child
    validate_child(conversation_id, resource_id)
    r = next((item for item in scope(conversation_id)["resources"] if item["id"] == resource_id), None)
    # Recheck live permission as well as the last launched scope. Revocation is immediate.
    snap = t.get("resourceSnapshot")
    ids = [i["id"] for i in snap["resources"]] if snap else t.get("resourceIds", [])
    if (
        not r
        or resource_id not in ids
        or r["permission"] != "read"
    ):
        raise W.Problem("resource_denied", "此资料不在本轮可读取范围内", 403)
    root = Path(r["path"])
    if not root.is_dir() or str(root.resolve()) != r["path"]:
        raise W.Problem("resource_moved", "资料目录已移动或链接目标变化", 409)
    w = {"path": r["path"]}
    from . import documents, image_evidence, project_image_view, video_frames

    attachment = r["kind"] == "attachment"
    is_video = attachment and r.get("mime") in video_frames.FORMAT_BY_MIME
    if video is not None and not is_video:
        raise W.Problem("unsupported_video", "只能对 MP4 或 WebM 附件指定抽帧范围")
    if attachment and path in (None, "", "."):
        path = r["file"]
    if not isinstance(path, str):
        raise W.Problem("invalid_path", "请提供文件相对路径，或用「.」列出目录")
    candidate = W.file_path(w, path) if attachment else W.agent_file_path(w, path)
    is_image = r.get("mime", "").startswith("image/") or candidate.suffix.lower() in image_evidence.FORMATS
    supports_view = is_image or candidate.suffix.lower() in project_image_view.GCODE_SUFFIXES
    if view is not None and (not supports_view or candidate.is_dir()):
        raise W.Problem("view_invalid", "图片视图支持静态图片或 3D 打印 G-code 文件", 415)
    if image_only and view is None and (not is_image or candidate.is_dir()):
        raise W.Problem("image_invalid", "请选择 PNG、JPEG、WebP 或静态 GIF 图片", 415)
    if r.get("mime") in documents.FORMAT_BY_MIME:
        result = documents.read(r, path, offset)
        current = next((item for item in scope(conversation_id)["resources"] if item["id"] == resource_id), None)
        if not current or current.get("permission") != "read" or current.get("hash") != r.get("hash"):
            raise W.Problem("resource_denied", "此附件已不在本轮可读取范围内", 403)
        if "sheets" in result:
            if "text" in result:
                return dict(result, readKind="table", summary="读取表格行")
            return dict(result, readKind="table_sheets", summary="列出工作表")
        if "text" in result:
            return dict(result, readKind="text", summary="读取文件正文")
        return dict(result, readKind="document_locations", summary="列出文档位置")
    if attachment and (Path(path) != Path(r["file"]) or candidate != root / r["file"]):
        raise W.Problem("attachment_file_mismatch", "此附件引用仅允许读取原文件", 403)
    from . import baseline

    hit = baseline.hit("read", candidate, scoped_app_resource=attachment)
    if hit:
        raise W.Problem(
            "protected_resource",
            "该资料命中系统读取保护规则，请由用户提供必要内容",
            403,
        )
    if is_video:
        if offset:
            raise W.Problem("invalid_cursor", "视频按时间定位，不使用文字偏移")
        return video_frames.read(conversation_id, r, candidate, video)
    if candidate.is_dir():
        if attachment:
            raise W.Problem("resource_changed", "导入资料已发生变化，请重新导入", 409)
        entries = W.files(w, path)
        relative = candidate.relative_to(root).as_posix()
        revision = W.digest(json.dumps([relative, entries], ensure_ascii=False, sort_keys=True).encode('utf8'))
        return dict(entries=entries, resourceId=resource_id, path=relative, listingRevision=revision,
                    readKind="directory", summary="列出目录")
    if is_image or view is not None:
        if snap and not any(item["id"] == resource_id and item["path"] == r["path"] for item in snap["resources"]):
            raise W.Problem("resource_denied", "图片不在本轮读取范围内", 403)
        try:
            if view is None:
                image = image_evidence.read(candidate, limit=2 * 1024 * 1024, expected_hash=r.get("hash"))
            else:
                image = project_image_view.read(candidate, view, expected_hash=r.get("hash"))
        except OSError as error:
            raise W.Problem("image_missing", "无法读取图片文件", 404) from error
        image["source"] = dict(resourceId=resource_id, root=r["path"])
        if view is not None:
            image["source"]["originalHash"] = image["sourceHash"]
            return dict(ok=True, image=image, readKind="image", summary="生成资料视图",
                        sourceSha256=image["sourceHash"], view=image["view"])
        return dict(ok=True, image=image, readKind="image", summary="读取图片")
    if not attachment:
        from .project_activity import confirm_read, watch

        relative = W.agent_file_path(w, path).relative_to(root).as_posix()
        observed = watch(t, r, relative)
        result = W.file_read(w, path)
        in_range = (1 <= line <= result["text"].count("\n") + 1) if line is not None else (
            type(offset) is int and (offset == 0 or 0 < offset < len(result["text"]))
        )
        # 只有实际读到有效位置且版本已确认，才记录项目读取事实；越界读取不构成已读证据。
        if confirm and in_range:
            confirm_read(conversation_id, observed, result["revision"])
            if r["kind"] == "code":
                from .code_read_receipts import record_source

                record_source(result, conversation_id, t.get("projectId"), r, relative)
    else:
        result = W.file_read(w, path)
        if result["revision"] != r["hash"]:
            raise W.Problem("resource_changed", "导入资料已发生变化，请重新导入", 409)
    return dict(result, readKind="text", summary="读取文件正文")


def tool_read_image(root, path, resource_id=None, *, allow_attachment=False, view=None):
    from .tools import current_call

    try:
        call = current_call()
        if not call:
            raise W.Problem("identity_required", "读取原图需要当前会话的动作身份", 403)
        thread = conversation(call["sessionId"])
        if (not thread.get("projectless")
                or Path(W.workspace(thread["workspaceId"])["path"]).resolve() != Path(root).resolve()):
            raise W.Problem("wrong_source", "图片读取不属于当前会话", 403)
        resources = {rid: resource for rid, resource in history_read_resources(thread["id"]).items()
                     if resource["kind"] in {"code", "documents"}
                     or (allow_attachment and rid == resource_id and resource["kind"] == "attachment"
                         and resource.get("mime", "").startswith("image/"))}
        if resource_id is None:
            if len(resources) > 1:
                raise W.Problem("resource_required", "有多个授权目录，请指定图片所属的 resource_id")
            resource_id = next(iter(resources), None)
        if not isinstance(resource_id, str) or resource_id not in resources:
            raise W.Problem("resource_denied", "图片所在目录不在本轮授权范围内", 403)
        if resources[resource_id]["kind"] == "attachment":
            if path not in (None, "", "."):
                raise W.Problem("invalid_path", "读取会话图片附件时只传 resource_id")
            path = None
        elif not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise W.Problem("invalid_path", "请提供项目目录内的图片相对路径")
        return dict(read_resource(thread["id"], resource_id, path, image_only=True, view=view),
                    path=path, summary="生成资料视图" if view is not None else "读取原图")
    except W.Problem as error:
        return dict(ok=False, code=error.code, error=error.message)


# 读取代码行时交给 `code_service.invoke` 走代码检查入口。
# 直接读取资料时 `offset` 表示字符偏移，并由本模块确认会话与资源的关系。
def tool_read(root, resource_id, path=None, offset=None, line=None, line_count=None, video=None):
    if line is not None or line_count is not None:
        if video is not None:
            return {"ok": False, "error": "视频时间范围不能与代码行号一起使用", "code": "invalid_video_range"}
        args = dict(relative_path=path)
        if line is not None: args['line'] = line
        if line_count is not None: args['line_count'] = line_count
        if offset is not None: args['offset'] = offset
        from .code_service import invoke
        return invoke(root, 'inspect', resource_id, **args)
    offset = 0 if offset is None else offset
    if type(offset) is not int or offset < 0:
        raise W.Problem("invalid_cursor", "offset 需要非负整数字符偏移")
    root = Path(root).resolve()
    data = W.registry()
    for t in data["threads"].values():
        w = data["workspaces"].get(t["workspaceId"], {})
        if t.get("projectless") and Path(w.get("path", "")).resolve() == root:
            try:
                result = read_resource(t["id"], resource_id, path, offset, confirm=True, video=video)
                if "text" in result and any(r["id"] == resource_id and r["kind"] == "code"
                                             for r in scope(t["id"])["resources"]):
                    from .code_service import _read_code_page
                    result = _read_code_page(result, dict(offset=offset))
                if "text" in result and "nextOffset" not in result:
                    if (
                        isinstance(offset, bool)
                        or not isinstance(offset, int)
                        or offset < 0
                    ):
                        raise W.Problem("invalid_cursor", "读取偏移无效")
                    text = result["text"]
                    result.update(
                        text=text[offset : offset + 4000],
                        nextOffset=offset + 4000 if offset + 4000 < len(text) else None,
                        totalChars=len(text),
                    )
                result.setdefault("ok", True)
                if (
                    resource_id in t.get("attachments", {})
                    and result["ok"]
                    and ("text" in result or "image" in result)
                ):
                    def record_read(data):
                        thread = data["threads"][t["id"]]
                        current = thread.get("attachments", {}).get(resource_id)
                        if not current or current["permission"] != "read" or resource_id not in thread["resourceIds"]:
                            raise W.Problem("resource_denied", "此附件已不在本轮可读取范围内", 403)
                        current.update(lastReadAt=time.time())

                    W.change_registry(record_read)
                return result
            except W.Problem as e:
                return {"ok": False, "error": e.message, "code": e.code}
    return {"ok": False, "error": "当前会话未选择该资料", "code": "resource_denied"}


def open_folder(raw, confirm=False, *, owned=False, publish=None, importing_session=None):
    """Bind a folder or a checked-out staging directory with one registry write."""
    if confirm is not True:
        raise W.Problem('confirmation', '请选择并确认项目文件夹')
    if not isinstance(raw, str) or not raw.strip():
        raise W.Problem('invalid_path', '请选择项目文件夹')
    try:
        root = Path(raw).resolve(strict=publish is None)
    except (OSError, ValueError, RuntimeError) as error:
        raise W.Problem('invalid_path', '项目文件夹不存在或无法访问') from error
    if publish is None and not root.is_dir():
        raise W.Problem('invalid_path', '请选择文件夹')

    def save(data):
        owners = data.setdefault('projects', {})
        for owner in owners.values():
            if owner.get('kind') == 'subtask':
                continue
            for resource in owner.get('resources', {}).values():
                if resource.get('kind') != 'code':
                    continue
                if os.path.normcase(resource['path']) == os.path.normcase(str(root)):
                    if publish is not None:
                        raise W.Problem("import_destination_exists", "已有项目绑定此目录，请选择其他名称", 409)
                    # Selecting this folder explicitly restores only read access.
                    # Existing write/execute settings and other resources are preserved.
                    if resource.get('permission') != 'read':
                        resource.update(permission='read', permissionAt=time.time(), permissionSource='user')
                    if not owner.get('folderResourceId'):
                        owner['folderResourceId'] = resource['id']
                    if owner.get("removed"):
                        owner.update(removed=False, visibilityRevision=owner.get("visibilityRevision", 1) + 1)
                    return owner
        resource = dict(id=uuid.uuid4().hex, name=root.name or str(root), path=str(root),
                        kind='code', permission='read', permissionAt=time.time(), permissionSource='user',
                        write=owned, execute=owned)
        owner = dict(id=uuid.uuid4().hex, name=resource['name'][:100], created=time.time(),
                     folderResourceId=resource['id'], resources={resource['id']: resource})
        if importing_session:
            thread = data["threads"].get(importing_session)
            if (publish is None or not thread or thread.get("projectId") or
                    any(thread.get(key) for key in ("archived", "readonlyEvidence", "subtaskOf", "scheduledRun"))):
                raise W.Problem("import_destination_changed", "导入期间会话范围已变化", 409)
            thread.update(projectId=owner["id"], resourceIds=[resource["id"], *[
                rid for rid in thread.get("resourceIds", []) if rid in thread.get("attachments", {})]])
        if publish is not None:
            publish(root)
        owners[owner['id']] = owner
        return owner
    return W.change_registry(save)


def attach_repository(ident, body, sv):
    """Bind only the folder explicitly selected by the user, keeping this project's existing roots."""
    from . import baseline
    if body.get("confirm") is not True or not isinstance(body.get("path"), str) or not Path(body["path"]).is_absolute():
        raise W.Problem("confirmation", "请选择要关联的本地仓库")
    t = conversation(ident)
    pid = t.get("projectId")
    if not pid or any(t.get(k) for k in ("readonlyEvidence", "subtaskOf", "scheduledRun", "archived", "activeDelegation")):
        raise W.Problem("workroom_scope", "请从可编辑的项目会话关联仓库", 403)
    from .subtasks import ensure_send
    ensure_send(ident)
    path = Path(body["path"]).resolve(strict=True)
    def validate():
        directory = sv._resolve_sid(ident)
        if directory and W.RT.status(directory)["active"]:
            raise W.Problem("active_run", "请在本轮结束后关联代码目录", 409)
        selected = scope(ident)
        if selected["revision"] != body.get("scopeRevision"):
            raise W.Problem("workroom_changed", "会话范围已变，请重新确认目录", 409)
        enclosing = [item for item in selected["resources"] if item["kind"] == "code"
                     and item["permission"] == "read" and path.is_relative_to(Path(item["path"]))]
        checkout = max(enclosing, key=lambda item: len(Path(item["path"]).parts), default=None)
        active_worktree = checkout and checkout.get("worktreeOf") and path == Path(checkout["path"]) and (path / ".git").is_file()
        if not path.is_dir() or not ((path / ".git").is_dir() or active_worktree) or baseline.hit("read", path):
            raise W.Problem("not_git_repository", "请选择已有本地 Git 仓库的根目录")
        return checkout
    validate()
    def save(data):
        checkout = validate()
        thread = data["threads"][ident]
        owner = data["projects"][pid]
        if thread.get("projectId") != pid or owner.get("removed"):
            raise W.Problem("workroom_changed", "当前项目已改变", 409)
        if checkout:
            base = Path(checkout["path"])
            relative = path.relative_to(base).as_posix() if path != base else ""
            W.file_path_from_root(base, relative)
            thread["workroomLocation"] = dict(projectId=pid, resourceId=checkout["id"], directory=relative)
            return dict(projectId=pid, resourceId=checkout["id"], name=path.name)
        resources = owner["resources"]
        resource = next((r for r in resources.values() if r["kind"] == "code" and Path(r["path"]) == path), None)
        if resource is None:
            resource = dict(id=uuid.uuid4().hex, name=path.name, path=str(path), kind="code", write=False, execute=False)
            resources[resource["id"]] = resource
        if resource.get("permission") != "read":
            resource.update(permission="read", permissionAt=time.time(), permissionSource="user")
        rid = resource["id"]
        if rid not in thread["resourceIds"]:
            thread["resourceIds"].append(rid)
        thread["workroomLocation"] = dict(projectId=pid, resourceId=rid, directory="")
        return dict(projectId=pid, resourceId=rid, name=resource["name"])
    return W.change_registry(save)


def set_visibility(ident, body, sv):
    """Change the original registration while keeping its files and chat associations."""
    revision = body.get("expectedRevision")
    removed = body.get("removed")
    if (set(body) - {"removed", "expectedRevision", "confirm", "_request_id"}
            or type(removed) is not bool or type(revision) is not int or revision < 1):
        raise W.Problem("project_visibility", "请核对项目状态和原登记版本")
    if removed and body.get("confirm") is not True:
        raise W.Problem("confirmation", "请确认从项目列表移除")

    def update(data):
        owner = data["projects"].get(ident)
        if not owner or owner.get("kind") == "subtask":
            raise W.Problem("not_found", "项目不存在", 404)
        if owner.get("visibilityRevision", 1) != revision:
            raise W.Problem("project_visibility_changed", "项目列表状态已变化，请刷新后重试", 409)
        if bool(owner.get("removed")) == removed:
            return owner
        if removed:
            for thread in data["threads"].values():
                if thread.get("projectId") != ident or thread.get("archived"):
                    continue
                directory = sv._resolve_sid(thread["id"])
                if not directory:
                    continue
                runtime = W.RT.status(directory)
                if runtime["active"] or runtime["state"] == "waiting_approval":
                    raise W.Problem("project_active", "请先结束项目中的活动会话，再移除项目", 409)
        owner.update(removed=removed, visibilityRevision=revision + 1)
        return owner

    return W.change_registry(update)


def route(method, parts, body, sv=None):
    if len(parts) == 4 and parts[1:3] == ["imports", "identities"] and sv is not None:
        from . import ssh_identities
        return ssh_identities.route(method, parts[3], body, sv)
    if parts[1:2] == ["imports"]:
        from . import repository_import

        return repository_import.route(method, parts[2:], body)
    if len(parts) == 1:
        if method == "GET":
            projects = [p for p in W.registry().get("projects", {}).values() if p.get("kind") != "subtask"]
            result = {"projects": [p for p in projects if not p.get("removed")]}
            removed = [p for p in projects if p.get("removed")]
            if removed:
                result["removedProjects"] = removed
            return 200, result
        if body.get('createFolder') is True:
            from . import work_folders
            target = work_folders.create('projects', body.get('name'))
            # This newly allocated empty directory is owned by the requested project.
            return 200, open_folder(str(target), True, owned=True)
        if 'path' in body:
            return 200, open_folder(body['path'], body.get('confirm'))
        name = str(body.get("name", "")).strip()[:100]
        if not name:
            raise W.Problem("invalid_name", "请输入项目名称")
        item = {
            "id": uuid.uuid4().hex,
            "name": name,
            "resources": {},
            "created": time.time(),
        }
        W.change_registry(
            lambda data: data.setdefault("projects", {}).__setitem__(item["id"], item)
        )
        return 200, item
    if len(parts) == 3 and parts[2] == "visibility" and method == "POST" and sv is not None:
        return 200, set_visibility(parts[1], body, sv)
    p = project(parts[1])
    if len(parts) == 2 and method == "GET":
        return 200, p
    if len(parts) == 3 and parts[2] == "resources" and method == "POST":
        raw = body.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise W.Problem("invalid_path", "请选择资料目录")
        root = Path(raw).resolve(strict=True)
        if not root.is_dir():
            raise W.Problem("invalid_path", "请选择目录")
        kind = body.get("kind", "documents")
        if kind not in {"documents", "code"}:
            raise W.Problem("invalid_resource", "未知资料类别")
        item = {
            "id": uuid.uuid4().hex,
            "name": root.name,
            "path": str(root),
            "kind": kind,
            "permission": "none",
        }
        W.change_registry(
            lambda data: data["projects"][p["id"]]["resources"].__setitem__(
                item["id"], item
            )
        )
        return 200, item
    if (
        len(parts) == 5
        and parts[2] == "resources"
        and parts[4] == "permission"
        and method == "POST"
    ):
        if parts[3] not in p["resources"]:
            raise W.Problem("not_found", "资料不存在", 404)
        value = body.get("permission")
        if value not in {"none", "read"} or (
            value == "read" and body.get("confirm") is not True
        ):
            raise W.Problem("confirmation", "读取资料需要明确确认")

        def update(data):
            r = data["projects"][p["id"]]["resources"][parts[3]]
            # 读权限与写入/执行分别授予；撤销读取同步关闭后两项，避免留下失去读取范围的执行能力。
            for capability in ("write", "execute"):
                if capability in body and type(body[capability]) is not bool:
                    raise W.Problem("invalid_permission", "权限值必须是布尔值")
                if body.get(capability) is True and (
                    r["kind"] != "code"
                    or value != "read"
                    or body.get("confirm") is not True
                ):
                    raise W.Problem("confirmation", "代码写入/执行需要单独明确授权")
                if capability in body:
                    r[capability] = body[capability]
                if value == "none":
                    r[capability] = False
            r.update(
                permission=value, permissionAt=time.time(), permissionSource="user"
            )
            return r

        return 200, W.change_registry(update)
    raise W.Problem("not_found", "没有这个项目接口", 404)
