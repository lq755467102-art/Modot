"""Installed package contribution declarations and revision-bound catalog pages."""
# 主要引用方包括 `plugins.py` 和 `plugin_runtime.py`。
import base64
from io import BytesIO
import json
from pathlib import Path
import re
from struct import error as StructError
from zlib import error as ZlibError

from . import plugin_packages, workbench
from .plugin_packages import ManifestError


PREVIEW = "modot.catalog-preview"
ICON_THEMES = ("default", "light", "dark")
ICON_BYTES = 128 * 1024
ICON_SIDE = 256


def checked_json(package, relative):
    """Read the exact bytes covered by the host fingerprint and detect duplicate keys."""
    root = Path(package["root"])
    path = plugin_packages.inside(root, "./" + relative)
    expected = package["files"][relative]
    with path.open("rb") as stream:
        raw = stream.read(expected["bytes"] + 1)
    if workbench.digest(raw) != expected["hash"]:
        raise ManifestError("plugin_changed", relative, "文件在校验时变化，请重新读取。")

    return plugin_packages.parse_json(raw, relative)


def add_preview(manifest, report):
    extensions = manifest.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ManifestError("invalid_extensions", "extensions", "extensions 应为 JSON 对象。")
    if PREVIEW not in extensions:
        return
    section = extensions[PREVIEW]
    at = "extensions." + PREVIEW
    if not isinstance(section, dict):
        raise ManifestError("invalid_catalog", at, "目录声明应为 JSON 对象。")
    if type(section.get("version")) is not int or section["version"] != 1:
        raise ManifestError("catalog_version", at + ".version", "当前支持目录声明版本 1。")
    package_key = section.get("packageKey")
    if (not isinstance(package_key, str) or len(package_key) > 128
            or not re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+", package_key)):
        raise ManifestError("package_key", at + ".packageKey", "请提供点分的稳定包标识，使用小写字母、数字和连字符。")
    pages = section.get("pages", [])
    if not isinstance(pages, list) or len(pages) > 128:
        raise ManifestError("page_ids", at + ".pages", "pages 应为最多 128 项的包内页面标识数组。")
    identifiers = set()
    for index, page in enumerate(pages):
        field = f"{at}.pages[{index}]"
        if not isinstance(page, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", page):
            raise ManifestError("page_id", field, "页面标识应为 1 至 64 位，以小写字母开头，可含数字、连字符和下划线。")
        if page in identifiers:
            raise ManifestError("duplicate_page", field, f"包内页面标识 {page!r} 重复。")
        identifiers.add(page)
    icons = section.get("icons", {})
    if (not isinstance(icons, dict) or set(icons) - set(ICON_THEMES)
            or any(not isinstance(path, str) or not 1 <= len(path) <= 256 for path in icons.values())):
        raise ManifestError("invalid_catalog_icons", at + ".icons", "图标仅支持 default、light、dark 三种包内文件声明。")
    extra = set(section) - {"version", "packageKey", "pages", "icons"}
    if extra:
        report["diagnostics"].append({"level": "warning", "code": "unknown_fields", "field": at,
                                      "message": "这些字段未参与核对：" + "、".join(sorted(extra))})
    report["packageKey"] = package_key
    if icons:
        report["icons"] = icons
    report["contributions"].extend({"kind": "page", "localId": page, "status": "declared-only"}
                                   for page in pages)


def page_declarations(package):
    """Portable page names are inert data. No entry point or grant comes from this extension."""
    report = {"contributions": [], "diagnostics": []}
    if package["format"] == "agent-plugins":
        try:
            add_preview(checked_json(package, "plugin.json"), report)
        except ManifestError as error:
            raise workbench.Problem(error.code, f"{error.field} · {error}", 409) from error
    return {"packageKey": report.get("packageKey"), "pages": [row["localId"] for row in report["contributions"]],
            "icons": report.get("icons", {})}


def declarations_for(package):
    declarations = package.get("catalogDeclarations")
    return page_declarations(package) if declarations is None else declarations


def current_install(ident, fingerprint, revision, *, data=None):
    from . import plugins

    package = (plugins.state() if data is None else data)["plugins"].get(ident)
    if not package or package.get("removed"):
        raise workbench.Problem("plugin_missing", "插件已卸载或不存在", 410)
    if package["revision"] != revision or package["fingerprint"] != fingerprint:
        raise workbench.Problem("plugin_changed", "插件安装修订已变化，请刷新贡献目录", 409)
    return package


def icon_bytes(package, declaration):
    from . import plugins

    relative = declaration.removeprefix("./")
    if (not declaration.startswith("./") or "\\" in declaration or ":" in declaration
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or Path(relative).suffix.lower() != ".png"):
        raise workbench.Problem("plugin_icon_invalid", "图标必须指向包内的 PNG 文件", 422)
    version = package["files"].get(relative)
    if not version:
        raise workbench.Problem("plugin_icon_missing", "安装包未保留所声明的图标", 404)
    if version["bytes"] > ICON_BYTES:
        raise workbench.Problem("plugin_icon_invalid", "图标超过 128 KiB", 422)
    try:
        root = plugins.directory().resolve() / ("package-" + package["id"])
        if Path(package["root"]).resolve() != root:
            raise workbench.Problem("plugin_changed", "保留包的路径已变化", 409)
        path = plugin_packages.inside(root, declaration)
        if path.resolve() != path:
            raise workbench.Problem("plugin_changed", "保留图标的路径已变化", 409)
        if not path.is_file():
            raise workbench.Problem("plugin_icon_missing", "已安装图标文件缺失", 404)
        with path.open("rb") as stream:
            raw = stream.read(version["bytes"] + 1)
        if path.resolve() != path or workbench.digest(raw) != version["hash"]:
            raise workbench.Problem("plugin_changed", "已安装图标的内容已变化", 409)
    except workbench.Problem as error:
        if error.code != "plugin_path":
            raise
        raise workbench.Problem("plugin_icon_invalid", "图标路径无法在保留包内核对", 422) from error
    except (OSError, RuntimeError) as error:
        raise workbench.Problem("plugin_icon_missing", "无法读取已安装的图标", 404) from error
    return raw


def decoded_icon(raw):
    try:
        from PIL import Image
    except ImportError as error:
        raise workbench.Problem("plugin_icon_decoder_unavailable", "图标解码组件未安装", 503) from error
    try:
        with Image.open(BytesIO(raw), formats=["PNG"]) as image:
            if any(not 1 <= side <= ICON_SIDE for side in image.size) or image.n_frames != 1:
                raise ValueError("unsupported icon size or frames")
            image.verify()
        # Decode the pixels as well as the container, and omit author metadata.
        with Image.open(BytesIO(raw), formats=["PNG"]) as image:
            rgba = image.convert("RGBA")
        rgba.info.clear()
        output = BytesIO()
        rgba.save(output, format="PNG")
        if output.tell() > ICON_BYTES:
            raise ValueError("normalized PNG exceeds the byte limit")
    except (OSError, SyntaxError, ValueError, OverflowError, StructError, ZlibError, Image.DecompressionBombError) as error:
        raise workbench.Problem("plugin_icon_invalid", "图标须为可完整解码的单帧 PNG，至多 256 × 256 像素、128 KiB", 422) from error
    return rgba.size, output.getvalue()


def read_icon(ident, query):
    fields = ("fingerprint", "revision", "theme")
    if set(query) != set(fields) or any(len(query[field]) != 1 for field in fields):
        raise workbench.Problem("plugin_arguments", "图标读取需要 fingerprint、revision 和 theme")
    fingerprint, revision, theme = (query[field][0] for field in fields)
    if (not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not re.fullmatch(r"[1-9][0-9]{0,15}", revision)
            or theme not in ICON_THEMES):
        raise workbench.Problem("plugin_arguments", "图标版本或主题无效")
    revision = int(revision)
    package = current_install(ident, fingerprint, revision)
    icons = declarations_for(package).get("icons", {})
    selected = theme if theme in icons else "default"
    if selected not in icons:
        raise workbench.Problem("plugin_icon_missing", "插件未声明此主题或通用图标", 404)
    (width, height), raw = decoded_icon(icon_bytes(package, icons[selected]))
    # Replacements, switch changes and same-content reinstalls all advance the revision.
    current_install(ident, fingerprint, revision)
    return dict(id=ident, fingerprint=fingerprint, revision=revision, theme=theme, resolvedTheme=selected,
                mime="image/png", width=width, height=height, data=base64.b64encode(raw).decode("ascii"))


def declarations_for(package):
    declarations = package.get("catalogDeclarations")
    return page_declarations(package) if declarations is None else declarations


def current_install(ident, fingerprint, revision, *, data=None):
    from . import plugins

    package = (plugins.state() if data is None else data)["plugins"].get(ident)
    if not package or package.get("removed"):
        raise workbench.Problem("plugin_missing", "插件已卸载或不存在", 410)
    if package["revision"] != revision or package["fingerprint"] != fingerprint:
        raise workbench.Problem("plugin_changed", "插件安装修订已变化，请刷新贡献目录", 409)
    return package



def installed_package(package):
    result = {key: package[key] for key in ("id", "fingerprint", "revision", "name", "version", "format")}
    declarations = declarations_for(package)
    rows = []

    def component(kind, local_id, switches, *, supported=True, reason=""):
        enabled = any(package.get(field) for field in switches)
        status = "enabled" if enabled else "disabled"
        row = dict(kind=kind, localId=local_id, controlledBy=switches, independentlySwitchable=False,
                   supported=supported, enabled=enabled, status=status if supported else "unsupported")
        if reason:
            row["reason"] = reason
        rows.append(row)
        return row

    if declarations["pages"]:
        from . import plugin_data_pages
        rows.extend(plugin_data_pages.contribution(package, page) for page in declarations["pages"])
    for skill in package["skills"]:
        component("skill", skill["name"], ["skillsEnabled"], supported=not skill["unsupported"],
                  reason=", ".join(skill["unsupported"]))
    for server in package["servers"]:
        row = component("mcp", server["name"], ["remoteEnabled"], supported=server["supported"], reason=server["reason"])
        connection = package.get("connections", {}).get(server["name"])
        row.update(transport=server["transport"], writeEnabled=bool(package.get("writeEnabled")),
                   authentication=("not-applicable" if server["transport"] == "stdio" else
                                   "configured" if connection else "unknown"),
                   availability="requires-inspection" if row["enabled"] and row["supported"] else "unavailable")
    for index, hook in enumerate(package.get("hooks", [])):
        row = component("hook", f"hook-{index + 1}", ["hooksEnabled"])
        row.update(identityScope="installed-version", event=hook["event"], matcher=hook["matcher"], entry=hook["entry"])
    for field, kind, switches in (("contextStores", "context-store", ["contextEnabled"]),
                                  ("workers", "worker", ["workersEnabled"]),
                                  ("runtimeFacts", "runtime-fact", ["skillsEnabled", "remoteEnabled", "hooksEnabled"])):
        for item in package.get(field, []):
            component(kind, item["name"], switches)
    if package.get("memoryProvider"):
        component("memory-provider", package["memoryProvider"]["adapter"], ["memoryEnabled"])
    icons = declarations.get("icons", {})
    if icons:
        result["iconThemes"] = [theme for theme in ICON_THEMES if theme in icons]
    return dict(result, installed=True, sourcePath=package.get("installSource"),
                packageKey=declarations["packageKey"], contributions=rows)


def listing(data, cursor=None):
    # Atomically replaced store is read exactly once by the route. Tombstones keep
    # remove/reinstall and a round-trip switch distinguishable from the old generation.
    packages = data["plugins"]
    versions = sorted((p["id"], p["fingerprint"], p["revision"], p["removed"],
                       sorted((page, state["revision"]) for page, state in p.get("dataPages", {}).items()))
                      for p in packages.values())
    snapshot = workbench.digest(json.dumps(versions, separators=(",", ":")).encode())
    rows = sorted((p for p in packages.values() if not p["removed"]), key=lambda p: (-p["installedAt"], p["id"]))
    start = 0
    if cursor is not None:
        match = re.fullmatch(r"([0-9a-f]{64})\.([1-9][0-9]{0,5})", cursor) if isinstance(cursor, str) else None
        if not match:
            raise workbench.Problem("invalid_cursor", "贡献目录游标格式无效")
        if match[1] != snapshot:
            raise workbench.Problem("plugin_catalog_stale", "贡献目录已变化，请清空游标并从首页重读", 409)
        start = int(match[2])
        if start % 20 or start >= len(rows):
            raise workbench.Problem("invalid_cursor", "该位置不是此快照的下一页")
    end = start + 20
    return dict(plugins=[installed_package(p) for p in rows[start:end]], total=len(rows), snapshot=snapshot,
                hasMore=end < len(rows), nextCursor=f"{snapshot}.{end}" if end < len(rows) else None)
