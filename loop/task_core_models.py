"""Bind a task core to the specialist connection selected by the user."""

import json

from . import models, workbench as W
from .model_catalog import model_source


ROLES = frozenset({"vision"})


def model_identity(base_url, model):
    if not base_url or not model:
        return None
    source = model_source(base_url)
    service = source["id"] if source and source["id"] != "azure" else base_url.rstrip("/")
    return W.digest(json.dumps([service, model]).encode("utf-8"))


def _connection_id(route):
    target = [route["base_url"].rstrip("/"), route["model"]]
    return W.digest(json.dumps(target).encode("utf-8"))


def _resolve(role):
    if role not in ROLES:
        raise W.Problem("worker_model_role", "任务核心声明的模型角色无效", 409)
    route = models.resolve(role)
    if not all(route.get(key) for key in ("base_url", "api_key", "model")):
        raise W.Problem("worker_model_missing", "请先在模型设置为任务核心选择模型连接", 409)
    return route


def bind(role, parent_identity=None):
    route = _resolve(role)
    if parent_identity is None:
        primary = models.resolve("main")
        parent_identity = model_identity(primary["base_url"], primary["model"])
    if model_identity(route["base_url"], route["model"]) == parent_identity:
        raise W.Problem("worker_model_same", "请为任务核心选择与父核心不同的模型", 409)
    source = model_source(route["base_url"])
    return dict(role=role, model=route["model"], provider=source["name"] if source else "自定义兼容端点",
                connection=_connection_id(route))


def public(binding):
    if binding is None:
        return None
    return {key: binding[key] for key in ("role", "model", "provider")}


def check(binding):
    route = _resolve(binding["role"])
    if _connection_id(route) != binding["connection"]:
        raise W.Problem("worker_model_changed", "任务核心的模型连接已变化，请用现有配置重新委派", 409)
    return route


def provider_for_child(ident, *, role=None):
    from . import subtasks
    from .provider import OpenAICompatProvider

    row = subtasks.validate_child(ident)
    worker = (row or {}).get("worker") or {}
    binding = worker.get("modelBinding")
    declared = (worker.get("contract") or {}).get("modelRole")
    if declared and (not binding or binding["role"] != declared):
        raise W.Problem("worker_model_missing", "任务核心缺少可核对的模型绑定", 409)
    if not binding or role is not None and binding["role"] != role:
        return None

    route = check(binding)

    def authorize():
        live = subtasks.validate_child(ident)
        if not live or live.get("worker", {}).get("modelBinding") != binding:
            raise W.Problem("worker_model_changed", "当前任务核心的模型授权已变化", 409)
        if check(binding)["api_key"] != route["api_key"]:
            raise W.Problem("worker_model_changed", "任务核心的模型凭据已变化，请重新委派", 409)

    return OpenAICompatProvider(profile=models.ROLES[binding["role"]], base_url=route["base_url"],
                                api_key=route["api_key"], model=route["model"],
                                request_guard=authorize)
