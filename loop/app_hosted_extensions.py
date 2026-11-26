"""Bind original application extensions to the existing installation and Agent grant registry.

The desktop card supplies the archive. Only the trusted host registry assigns a transport and slot.
No publisher manifest can opt itself into a native application host.
"""
# 主要引用方包括 `plugin_runtime.py` 和 `vsix_upload.py`。

# 由 `plugin_runtime.py` 引用，连接应用子插件 HTTP 路由与 Agent 槽位适配器。真实原包继续由 `plugins.py` 管理。
import base64
import binascii
from contextlib import contextmanager
import json
import re
from pathlib import Path
import tempfile

from . import app_testing, app_formatting, vsix_upload, plugins, workbench as W


# Trusted adapters declare archive compatibility and implement inspection, discovery and invocation.
APPLICATION_SLOTS = {"code-workroom": {app_testing.SLOT: app_testing, app_formatting.SLOT: app_formatting}}


# 返回卡片要用的包 ID、授权版本、启用和运行授权。宿主是否已准备好由槽位的 `runtime` 单独给出。
def row(package):
    """Expose the child and its current grant revision to the parent card."""
    return dict(
        packageId=package["id"],
        revision=package["revision"],
        enabled=package["remoteEnabled"],
        canRun=bool(package.get("writeEnabled")),
        **package["applicationBinding"],
    )


@contextmanager
def _source(parent, slot, body):
    """Stage a byte-verified original using the selected, registered adapter."""
    adapter = APPLICATION_SLOTS[parent][slot]
    if "upload" in body and not set(body) - {"upload", "sha256", "slot", "_request_id"}:
        raw = vsix_upload.finish(parent, slot, body["upload"], body.get("sha256"))
    else:
        if (
            set(body) - {"slot", "archive", "sha256", "_request_id"}
            or not isinstance(body.get("archive"), str)
            or len(body["archive"]) > (adapter.MAX_ARCHIVE + 2) // 3 * 4
        ):
            raise W.Problem("app_archive", "请从应用卡片选择符合当前槽位容量限制的原包。")
        try:
            raw = base64.b64decode(body["archive"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise W.Problem("invalid_testing_vsix", "原包编码无效。") from exc
    binding = dict(parentId=parent, slot=slot, **adapter.inspect_archive(raw))
    if binding["sha256"] != body.get("sha256"):
        raise W.Problem("plugin_changed", "原包与所选文件的指纹不同。", 409)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Parent and slot form part of the immutable identity even if two apps import identical bytes.
        label = binding["extensionId"]
        manifest = dict(
            name=label if len(label) <= 64 else label[:46] + "-" + W.digest(label.encode())[:16],
            version=binding["version"],
            description=binding["name"],
            applicationBinding=binding,
        )
        (root / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
        (root / adapter.ARCHIVE_FILE).write_bytes(raw)
        source = plugins.packages.inspect(str(root))
        ident = W.digest((source["name"] + "\0" + source["fingerprint"]).encode())[:32]
        yield source, ident, binding


def _installation(binding):
    return dict(
        applicationBinding=binding,
        servers=[
            dict(
                name="app-tools",
                transport="app-host",
                supported=True,
                reason="",
                displayURL=binding["parentId"] + " / " + binding["slot"],
                url="",
            )
        ],
    )


def install(parent, slot, body):
    """Importing identical bytes twice reuses this parent's installation."""
    with _source(parent, slot, body) as (source, ident, binding), plugins.store_write():
        data = plugins.state()
        for previous in data["plugins"].values():
            other = previous.get("applicationBinding", {})
            if previous.get("removed") or (
                other.get("parentId"),
                other.get("slot"),
                other.get("extensionId"),
            ) != (parent, slot, binding["extensionId"]):
                continue
            if previous["id"] == ident:
                return row(previous)
            raise W.Problem("plugin_changed", "该子插件已有另一版本，请核对原安装后替换。", 409)
        package = plugins.prepare_install(data, ident, source)
        package.update(_installation(binding))
        data["plugins"][ident] = package
        plugins.save(data)
        return row(package)


def _replace(parent, slot, ident, body):
    upload_keys = {"slot", "sha256", "_request_id", "upload" if "upload" in body else "archive"}
    if (set(body) - upload_keys != {"expectedRevision", "requestId"}
            or not isinstance(body.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", body["sha256"])):
        raise W.Problem("plugin_arguments", "替换需提供旧安装修订、新原包的指纹和请求 ID")
    upload = {key: value for key, value in body.items() if key in upload_keys}

    @contextmanager
    def candidate(previous):
        binding = previous.get("applicationBinding", {})
        if (binding.get("parentId"), binding.get("slot")) != (parent, slot):
            raise W.Problem("app_slot", "原安装不属于此应用和槽位", 403)
        with _source(parent, slot, upload) as (source, target_id, updated):
            if updated["extensionId"] != binding.get("extensionId"):
                raise W.Problem("app_extension_changed", "新原包属于其他扩展，请独立安装", 409)
            yield source, target_id, _installation(updated)

    return plugins.replace_installation(
        ident, body["expectedRevision"], body["requestId"], [parent, slot, body["sha256"]], candidate,
    )


def _scope(query):
    from . import project_workroom

    keys = set(query)
    if (keys not in ({"conversationId", "resourceId"}, {"projectId", "resourceId"})
            or any(len(values) != 1 or not values[0] for values in query.values())):
        raise W.Problem("workroom_scope", "请提供检出目录及其所属项目或会话")
    conversation = query.get("conversationId", [None])[0]
    project = query.get("projectId", [None])[0]
    resource = query["resourceId"][0]
    view = project_workroom.context(conversation, project)
    root = next((row for row in view["roots"] if row["resourceId"] == resource), None)
    if view["status"] != "ready" or not root:
        raise W.Problem("resource_denied", "该检出目录不在当前工作间范围", 403)
    path = Path(root["path"])
    if not path.is_dir() or path.resolve() != path:
        raise W.Problem("resource_moved", "该检出目录不可用，请重新选择", 409)
    return dict(
        projectId=view["project"]["id"] if view["project"] else None,
        conversationId=conversation, resourceId=resource, checkout=root["checkout"],
        scopeRevision=view["scopeRevision"],
    )


def route(parts, method, body, *, query=None):
    if parts and parts[0] in APPLICATION_SLOTS and len(parts) >= 2:
        parent = parts[0]
        slots = APPLICATION_SLOTS[parent]
        if len(parts) == 4 and parts[1] == "children" and parts[3] == "replace" and method == "POST":
            slot = body.get("slot")
            if not isinstance(slot, str) or slot not in slots:
                raise W.Problem("app_slot", "此应用未声明请求的槽位版本", 409)
            return 200, _replace(parent, slot, parts[2], body)
        if parts[1] == "formatting" and app_formatting.SLOT in slots:
            return 200, app_formatting.editor_route(parent, parts[2:], method, body)
        if parts[1:] == ["uploads"] and method == "POST" and isinstance(body.get("slot"), str) and body["slot"] in slots:
            return 200, vsix_upload.transfer(parent, slots[body["slot"]], body)
    if len(parts) != 2 or parts[1] != "children" or parts[0] not in APPLICATION_SLOTS:
        raise W.Problem("app_slot", "没有这个应用子插件入口。", 404)
    parent = parts[0]
    slots = APPLICATION_SLOTS[parent]
    # 应用卡片读取登记状态与宿主可用性。实际安装只由下方的 POST 入口执行。
    if method == "GET":
        scope = _scope(query) if query else None
        listing = dict(
            children=[
                row(p)
                for p in plugins.state()["plugins"].values()
                if not p.get("removed") and p.get("applicationBinding", {}).get("parentId") == parent
            ],
            slots={
                name: dict(available=adapter.runtime() is not None, format="vsix",
                           category=adapter.CATEGORY, maxArchiveBytes=adapter.MAX_ARCHIVE)
                for name, adapter in slots.items()
            },
        )
        if scope is not None:
            listing["scope"] = scope
        return 200, listing
    slot = body.get("slot")
    if method == "POST" and isinstance(slot, str) and slot in slots:
        return 200, install(parent, slot, body)
    raise W.Problem("app_slot", "此应用未登记请求的扩展槽位。", 409)


# 根据 Agent 选择的已安装包和目标服务解析槽位，交由登记的适配器接管调用与后续权限核验。
def client(ident, package, target, validate_live):
    binding = package["applicationBinding"]
    adapter = APPLICATION_SLOTS.get(binding["parentId"], {}).get(binding["slot"])
    if target.get("name") != "app-tools" or not adapter:
        raise W.Problem("app_slot", "应用绑定已变化。", 403)
    return adapter.Client(ident, package, validate_live)
