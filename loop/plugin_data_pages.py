"""Host v1 declarative application pages and authorize selected draft examples."""
# 主要引用方包括 `plugin_contributions.py` 和 `plugin_runtime.py`。
from pathlib import Path
import re

from . import plugin_contributions as catalog, plugins, projects, workbench as W

PAGE_BYTES = 512 * 1024  # Same byte limit as plugin manifests.
PAGE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def _state(package, page_id):
    return package.get("dataPages", {}).get(page_id, {})


def contribution(package, page_id):
    """Project readiness without reading package files during catalog pagination."""
    recorded = package["files"].get(f"data-pages/{page_id}.json")
    supported = recorded is not None and 0 < recorded["bytes"] <= PAGE_BYTES
    state = _state(package, page_id)
    enabled = supported and state.get("enabled") is True
    result = dict(kind="page", localId=page_id, controlledBy=[], independentlySwitchable=supported,
                  supported=supported, enabled=enabled, status="enabled" if enabled else "disabled")
    if not supported:
        return dict(result, status="declared-only", reason="page_descriptor_invalid" if recorded else "page_descriptor_missing")
    return dict(result, pageRevision=state.get("revision", 0), **({"pageTitle": state["title"]} if state.get("title") else {}))


def _identity(values):
    fingerprint, revision, page_revision = (values.get(key) for key in ("fingerprint", "revision", "pageRevision"))
    if (not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint)
            or type(revision) is not int or not 1 <= revision <= 2**53 - 1
            or type(page_revision) is not int or not 0 <= page_revision <= 2**53 - 1):
        raise W.Problem("plugin_page_arguments", "插件页面的安装身份或修订无效")
    return fingerprint, revision, page_revision


def _current(ident, page_id, identity, *, enabled, data=None):
    fingerprint, revision, page_revision = identity
    package = catalog.current_install(ident, fingerprint, revision, data=data)
    if page_id not in catalog.declarations_for(package)["pages"]:
        raise W.Problem("plugin_page_missing", "此安装没有该页面", 404)
    state = _state(package, page_id)
    if enabled and not state.get("enabled"):
        raise W.Problem("plugin_page_disabled", "该插件页面已停用", 403)
    if page_revision != state.get("revision", 0):
        raise W.Problem("plugin_page_changed", "页面启停状态已变化，请重新打开", 409)
    return package


def _response(package, page_id):
    return dict(id=package["id"], fingerprint=package["fingerprint"], revision=package["revision"],
                pageId=page_id, pageRevision=_state(package, page_id).get("revision", 0))


def _text(value, length):
    # Reject lone surrogate escapes, which JSON can parse but the UI transport cannot encode.
    return (isinstance(value, str) and 1 <= len(value) <= length
            and not re.search(r"[\ud800-\udfff]", value))


def _descriptor(package, page_id):
    row = contribution(package, page_id)
    if not row["supported"]:
        raise W.Problem("plugin_page_missing", "安装包没有可读取的 v1 页面描述", 404)
    relative = f"data-pages/{page_id}.json"
    if Path(package["root"]).resolve() != plugins.directory().resolve() / ("package-" + package["id"]):
        raise W.Problem("plugin_changed", "页面的保留安装目录已变化", 409)
    try:
        value = catalog.checked_json(package, relative)
    except catalog.ManifestError as error:
        if error.code == "plugin_changed":
            raise W.Problem("plugin_changed", "页面文件与原安装不一致", 409) from error
        raise W.Problem("plugin_page_invalid", "页面描述含重复字段", 422) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise W.Problem("plugin_page_invalid", "页面描述无法读取或不是有效的 JSON", 422) from error

    required = {"version", "packageKey", "pageId", "title", "description"}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {"examples", "actions"} or type(value["version"]) is not int or value["version"] != 1
            or value["packageKey"] != catalog.declarations_for(package)["packageKey"] or value["pageId"] != page_id
            or not _text(value["title"], 80) or not _text(value["description"], 600)):
        raise W.Problem("plugin_page_invalid", "页面描述与 v1 契约或安装身份不匹配", 422)

    examples = value.get("examples", [])
    actions = value.get("actions", [])
    if (not isinstance(examples, list) or len(examples) > 8
            or any(not isinstance(item, dict) or item.keys() != {"title", "text"}
                   or not _text(item["title"], 80) or not _text(item["text"], 2000) for item in examples)
            or actions not in ([], ["append-to-draft"])):
        raise W.Problem("plugin_page_invalid", "页面示例或宿主动作不符合 v1 契约", 422)
    return dict(value, examples=examples, actions=actions)


def _draft_owner(conversation_id):
    owner = projects.conversation(conversation_id)
    if owner.get("archived") or owner.get("readonlyEvidence") or owner.get("subtaskOf"):
        raise W.Problem("plugin_page_draft_unavailable", "此会话的草稿不可编辑", 403)


def route(ident, page_id, tail, method, query, body):
    if not PAGE_ID.fullmatch(page_id):
        raise W.Problem("plugin_page_arguments", "插件页面标识无效")
    fields = {"fingerprint", "revision", "pageRevision"}

    if method == "GET" and not tail:
        if (set(query) != fields or any(len(query[key]) != 1 for key in fields)
                or any(not re.fullmatch(r"[0-9]{1,16}", query[key][0]) for key in ("revision", "pageRevision"))):
            raise W.Problem("plugin_page_arguments", "读取页面需要完整安装身份与页面修订")
        values = {key: query[key][0] for key in fields}
        values.update(revision=int(values["revision"]), pageRevision=int(values["pageRevision"]))
        identity = _identity(values)
        package = _current(ident, page_id, identity, enabled=True)
        page = _descriptor(package, page_id)
        _current(ident, page_id, identity, enabled=True)
        return 200, dict(_response(package, page_id), page=page)

    if method == "POST" and not query:
        if not tail and set(body) - {"_request_id"} == fields | {"enabled"} and type(body["enabled"]) is bool:
            identity = _identity(body)
            with plugins.store_write():
                data = plugins.state()
                package = _current(ident, page_id, identity, enabled=False, data=data)
                before = _state(package, page_id)
                target = body["enabled"]
                if target != before.get("enabled", False):
                    title = _descriptor(package, page_id)["title"] if target else before.get("title")
                    package.setdefault("dataPages", {})[page_id] = dict(
                        enabled=target, revision=identity[2] + 1, title=title)
                    plugins.save(data)
                return 200, dict(_response(package, page_id), enabled=target)

        if tail == ["draft"] and set(body) - {"_request_id"} == fields | {"conversationId", "exampleIndex"}:
            identity = _identity(body)
            owner, index = body["conversationId"], body["exampleIndex"]
            if not isinstance(owner, str) or not 1 <= len(owner) <= 128 or type(index) is not int or index < 0:
                raise W.Problem("plugin_page_arguments", "草稿归属或示例位置无效")
            package = _current(ident, page_id, identity, enabled=True)
            page = _descriptor(package, page_id)
            if "append-to-draft" not in page["actions"] or index >= len(page["examples"]):
                raise W.Problem("plugin_page_action_denied", "该页面没有授权的草稿示例", 403)
            _draft_owner(owner)
            _current(ident, page_id, identity, enabled=True)
            return 200, dict(_response(package, page_id), conversationId=owner, exampleIndex=index,
                             **page["examples"][index])

    raise W.Problem("plugin_page_arguments", "页面接口的动作或字段无效")
