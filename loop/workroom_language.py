"""Registration and scope of optional workroom editor services."""
# 主要引用方包括 `plugin_runtime.py`。
import base64
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import uuid

from . import plugins, project_workroom, workbench as W
from . import native_language_tools

VERSION = "5.8.3"
URL = "https://registry.npmjs.org/typescript/-/typescript-5.8.3.tgz"
INTEGRITY = base64.b64decode("p1diW6TqL9L07nNxvRMM7hMMw4c5XOo/1ibL4aAIGmSAt9slTE1Xgw5KWuof2uTOvCg9BY7ZRi+GaF+7sfgPeQ==")
PAYLOAD = re.compile(r"^package/(?:LICENSE\.txt|ThirdPartyNoticeText\.txt|lib/(?:typescript\.js|lib(?:\.[a-z0-9.-]+)?\.d\.ts))$")
DEFAULT = {"revision": 0, "installed": False, "enabled": False}
PREFIXES = {"typescript": "ts", "cpp": "cpp", "rust": "rust", "python": "python"}

def record(data, language):
    return data.get("editorLanguages", {}).get(language, DEFAULT)

def public(language, item):
    return {**{k: item[k] for k in DEFAULT}, "id": language,
            "version": item.get("version", VERSION if language == "typescript" else ""),
            "source": item.get("source", "managed" if item["installed"] else None)}

def archive():
    digest = INTEGRITY.hex()
    for cache in (os.environ.get("npm_config_cache"), Path(os.environ["LOCALAPPDATA"]) / "npm-cache" if os.environ.get("LOCALAPPDATA") else None, Path.home() / ".npm"):
        if cache:
            path = Path(cache) / "_cacache" / "content-v2" / "sha512" / digest[:2] / digest[2:4] / digest[4:]
            if path.is_file() and path.stat().st_size <= 8 * 1024**2:
                raw = path.read_bytes()
                if hashlib.sha512(raw).digest() == INTEGRITY:
                    return raw
    try:
        with urlopen(URL, timeout=14) as response:
            raw = response.read(8 * 1024**2 + 1)
    except OSError as exc:
        raise W.Problem("language_download", "无法下载 TypeScript 语言插件，请检查网络后重试。", 503) from exc
    if hashlib.sha512(raw).digest() != INTEGRITY:
        raise W.Problem("language_integrity", "TypeScript 安装包未通过完整性核对。", 422)
    return raw

def managed_directory(language, item):
    name = item.get("directory", "")
    if not re.fullmatch(r"editor-" + PREFIXES[language] + r"-[a-f0-9]{32}", name):
        raise W.Problem("language_missing", "语言插件文件不存在，请重新安装。", 409)
    path = plugins.directory() / name
    if path.resolve() != path.absolute():
        raise W.Problem("language_path", "语言插件目录已移动。", 409)
    return path

def extract(dest):
    with tarfile.open(fileobj=io.BytesIO(archive()), mode="r:gz") as package:
        for entry in package:
            if entry.isfile() and PAYLOAD.fullmatch(entry.name):
                target = dest / entry.name.removeprefix("package/")
                target.parent.mkdir(exist_ok=True)
                with package.extractfile(entry) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    if not all((dest / f).is_file() for f in ("lib/typescript.js", "lib/lib.d.ts", "LICENSE.txt")):
        raise W.Problem("language_missing", "语言插件缺少运行文件。", 422)

def cleanup():
    """A retired image may still be held by another Electron window on Windows."""
    pending = plugins.state().get("editorLanguageRemovals", {})
    removed = []
    for name, language in pending.items():
        if language not in PREFIXES:
            continue
        path = managed_directory(language, {"directory": name})
        try:
            if path.exists():
                shutil.rmtree(native_language_tools.disk_path(path))
            removed.append(name)
        except OSError:
            # A later catalog read or window shutdown can collect this exact retired image.
            continue
    if removed:
        with plugins.store_write():
            data = plugins.state()
            entries = data.get("editorLanguageRemovals", {})
            if not any(name in entries for name in removed):
                return
            for name in removed:
                entries.pop(name, None)
            if not entries:
                data.pop("editorLanguageRemovals", None)
            plugins.save(data)


def change(language, body):
    action, revision = body.get("action"), body.get("expectedRevision")
    if (set(body) - {"action", "expectedRevision", "_request_id"} or action not in ("install", "enable", "disable", "uninstall")
            or type(revision) is not int):
        raise W.Problem("language_arguments", "请重新读取语言插件后操作。")
    current = record(plugins.state(), language)
    if current["revision"] != revision:
        raise W.Problem("language_changed", "语言插件状态已变化，请刷新。", 409)
    temp = None
    unpublished = None
    prepared = {}
    try:
        if action == "install" and not current["installed"]:
            temp = tempfile.TemporaryDirectory(prefix="editor-install-", dir=native_language_tools.disk_path(plugins.directory()))
            if language == "typescript":
                extract(Path(temp.name))
                prepared = {"version": VERSION, "source": "managed"}
            else:
                prepared = native_language_tools.prepare(language, Path(temp.name))
        with plugins.store_write():
            data = plugins.state()
            current = record(data, language)
            if current["revision"] != revision:
                raise W.Problem("language_changed", "语言插件状态已变化，请刷新。", 409)
            target = dict(current)
            if action == "install" and not current["installed"]:
                target.update(prepared, installed=True, enabled=True)
                if target["source"] != "system":
                    target["directory"] = "editor-" + PREFIXES[language] + "-" + uuid.uuid4().hex
                    destination = managed_directory(language, target)
                    Path(temp.name).rename(destination)
                    unpublished = destination
            elif action == "uninstall" and current["installed"]:
                # A borrowed executable, Cargo home or external sysroot is never a removal target.
                if name := current.get("directory"):
                    managed_directory(language, current)
                    data.setdefault("editorLanguageRemovals", {})[name] = language
                target = dict(DEFAULT)
            elif action in ("enable", "disable"):
                if not current["installed"]:
                    raise W.Problem("language_missing", "请先安装语言插件。", 409)
                target["enabled"] = action == "enable"
            if target != current:
                target["revision"] = revision + 1
                data.setdefault("editorLanguages", {})[language] = target
                plugins.save(data)
                unpublished = None
        if action == "uninstall":
            cleanup()
        return public(language, target)
    finally:
        if temp is not None:
            temp.cleanup()
        if unpublished is not None:
            shutil.rmtree(native_language_tools.disk_path(unpublished))


def runtime(language, item):
    directory = managed_directory(language, item) if item.get("directory") else None
    if language == "typescript":
        return {"library": str(directory / "lib")}
    result = {"pythonPath": sys.executable} if language == "python" else {}
    for key in (("server", "cargo", "rustc", "sources", "sysroot") if language == "rust" else ("server",)):
        path = Path(item[key])
        if not path.is_absolute():
            if directory is None or ".." in path.parts:
                raise W.Problem("language_path", "语言服务位置无效。", 409)
            path = directory / path
            if not path.resolve().is_relative_to(directory):
                raise W.Problem("language_path", "自管组件位置已变化。", 409)
        if not (path.is_dir() if key in ("sysroot", "sources") else path.is_file()):
            raise W.Problem("language_missing", "已登记的语言服务组件已移走，请重新安装语言插件。", 409)
        result[key] = str(path)
    return result


def route(tail, method, query, body):
    if not tail and method == "GET" and not query:
        cleanup()
        data = plugins.state()
        return 200, {"languages": [public(lang, record(data, lang)) for lang in PREFIXES]}
    if not tail or tail[0] not in PREFIXES:
        raise W.Problem("language_arguments", "未知的语言插件。", 404)
    language, *rest = tail
    if not rest and not query:
        if method == "GET":
            return 200, public(language, record(plugins.state(), language))
        if method == "POST":
            return 200, change(language, body)
    if rest == ["authority"] and method == "GET":
        fields = {"workspaceId", "resourceId", "scopeRevision", "revision"}
        if (set(query) not in (fields | {"conversationId"}, fields | {"projectId"})
                or any(len(values) != 1 or not values[0] for values in query.values())):
            raise W.Problem("language_scope", "语言服务需要当前工作间范围。", 403)
        args = {k: v[0] for k, v in query.items()}
        item = record(plugins.state(), language)
        if not item["enabled"] or str(item["revision"]) != args["revision"]:
            raise W.Problem("language_disabled", "语言插件已停用或发生变化。", 403)
        view = project_workroom.context(args.get("conversationId"), args.get("projectId"))
        root = next((r for r in view["roots"] if r["resourceId"] == args["resourceId"]), None)
        workspace = W.workspace(args["workspaceId"])
        if (view["status"] != "ready" or view["scopeRevision"] != args["scopeRevision"] or not root
                or Path(root["path"]) != Path(workspace["path"]) or Path(root["path"]).resolve() != Path(root["path"])):
            raise W.Problem("language_scope", "代码目录的可读范围已变化。", 403)
        return 200, {"root": root["path"], "revision": item["revision"], **runtime(language, item)}
    raise W.Problem("language_arguments", "语言插件请求无效。")
