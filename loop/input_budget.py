"""Count and admit a native L0 provider request before history compaction."""
from copy import copy
import json
import re

from . import history_compaction as history
from .context_manager import (ContextBlocked, RequestBody, assess, content_spans,
                              runtime_update_name)

_LABELS = {
    "system": "系统指令",
    "workspaceInstructions": "项目说明",
    "skills": "技能目录",
    "plugins": "插件目录",
    "pluginFacts": "插件事实",
    "baseTools": "基础工具",
    "extraTools": "按需工具",
    "runtimeUpdate": "运行资料",
    "attachments": "附件",
    "current": "本轮材料",
}

_CATALOGUES = {
    "skills": ("## 可用技能", "技能目录按需加载。"),
    "plugins": ("## 本轮插件组件", "已启用插件的目录按需加载。"),
}


def layout(body):
    """Classify rows by host annotations and the latest real user input."""
    messages = body["messages"]
    fixed, runtime, projected = set(), {}, set()
    latest = None
    for i, message in enumerate(messages):
        annotations = body.parts.get(i, ())
        role = message.get("role")
        if role in {"system", "developer"}:
            fixed.add(i)
        elif any(kind == "contextView" for kind, _ in annotations):
            projected.add(i)
        elif (name := runtime_update_name(message, annotations)) is not None:
            # Older updates still cost input when present. Only the latest
            # update of each live source belongs to the minimum package.
            runtime[name] = i
        elif role == "user":
            latest = i
    fixed.update(runtime.values())
    groups = [
        "fixed" if i in fixed else "current" if i not in projected and latest is not None and i >= latest else "history"
        for i in range(len(messages))
    ]
    return groups, latest


def subset(body, rows):
    indexed = list(rows)
    return RequestBody(dict(body, messages=[body["messages"][i] for i in indexed]),
                       {new: body.parts[i] for new, i in enumerate(indexed) if i in body.parts})


def required_rows(body):
    groups, latest = layout(body)
    return [i for i, group in enumerate(groups) if group == "fixed" or i == latest]


def input_limit(capacity, window, output):
    limits = [capacity] if capacity else []
    if window:
        limits.append(max(0, window - output))
    return min(limits) if limits else None


def _requested_text(body):
    _, latest = layout(body)
    if latest is None:
        return ""
    content = body["messages"][latest].get("content")
    if isinstance(content, str):
        return content.casefold()
    return " ".join(part.get("text", "") for part in content or []
                    if part.get("type") == "text").casefold()


def _skills_index(text, requested):
    lines = [_CATALOGUES["skills"][1] +
             "先 use_tool_group(group=skills)，再用 list_skills 查询；按名称 load_skill。技能只能作为方法，不能授予权限。"]
    for name, description in re.findall(r"(?m)^- `([^\n`]+)`：([^\n]*)", text):
        if name.casefold() in requested:
            lines.append(f"- {name}：{description[:120]}")
    return "\n".join(lines)


def _plugin_index(text, requested):
    lines = [_CATALOGUES["plugins"][1] +
             "用 use_tool_group(group=plugins)，再用 list_plugins 查询。"]
    # Retain the host's component and approval instructions.
    preface = text.splitlines()[1:2]
    if preface and preface[0].startswith("先用 list_plugins 核对包版本"):
        lines.extend(preface)
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name, ident = row.get("name"), row.get("plugin_id")
        if name and ident and (name.casefold() in requested or ident in requested):
            lines.append(f"- {name}（{ident}）"
                         + ("，本轮已启用 MCP" if row.get("remoteEnabled") else ""))
    return "\n".join(lines)


def _fact_readers(core, requested):
    readers = getattr(core, "budget_plugin_reads", {})
    return {key: tool for key, (tool, plugin, name) in readers.items()
            if not any(word and word.casefold() in requested for word in (key, plugin, name))}


def _runtime_excerpt(content, readers):
    from .runtime_facts import _payload
    value = _payload(content)
    if not value:
        return content
    facts = value["facts"]
    for name in readers.keys() & facts.keys():
        item = facts[name]
        if item.get("status") != "observed" or "value" not in item:
            continue
        stub = {key: item[key] for key in ("source", "trust") if key in item}
        stub.update(status="on_demand", loadGroup="plugins", readTool=readers[name])
        if len(json.dumps(stub, ensure_ascii=False)) < len(json.dumps(item, ensure_ascii=False)):
            facts[name] = stub
    head = content.split("\n", 2)[:2]
    return "\n".join([*head, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))])


def project_optional(body, plan):
    """Use the same optional-content projection when a checkpoint is rebuilt."""
    if not plan:
        return body
    result = copy(body)
    messages = list(body["messages"])
    parts = dict(body.parts)
    requested = plan["requested"]
    deferred = set(getattr(body, "budget_deferred", ()))
    for index, message in enumerate(messages):
        original = content = message.get("content")
        if not isinstance(content, str):
            continue
        annotations = parts.get(index, ())
        replacements = {}
        if message.get("role") == "system":
            spans = content_spans(content, annotations)
            for kind, start, end in sorted(spans, key=lambda span: span[1], reverse=True):
                render = plan.get(kind)
                fragment = original[start:end]
                if not render or not fragment.startswith(_CATALOGUES[kind][0]):
                    continue
                short = render(fragment, requested)
                if len(short.encode()) < len(fragment.encode()):
                    content = content[:start] + short + content[end:]
                    replacements[(kind, fragment)] = short
                    deferred.add(kind)
        if plan.get("pluginFacts") and runtime_update_name(message, annotations) == "runtime-facts":
            short = _runtime_excerpt(content, plan["pluginFacts"])
            if len(short.encode()) < len(content.encode()):
                replacements[("runtimeUpdate", content)] = short
                content = short
                deferred.add("pluginFacts")
        if content != original:
            messages[index] = dict(message, content=content)
            parts[index] = [(kind, replacements.get((kind, fragment), fragment))
                            for kind, fragment in annotations]
    result["messages"], result.parts = messages, parts
    if removable := plan.get("extraTools"):
        tools = body.get("tools", [])
        retained = [tool for tool in tools
                    if tool.get("function", {}).get("name") not in removable]
        if len(retained) < len(tools):
            result["tools"] = retained
            deferred.add("extraTools")
    result.budget_deferred = sorted(deferred)
    return result


def catalogue_excerpt(body, kind):
    marker = _CATALOGUES[kind][1]
    return next((fragment for i, msg in enumerate(body["messages"])
                 if msg.get("role") == "system"
                 for name, fragment in body.parts.get(i, ())
                 if name == kind and fragment.startswith(marker)
                 and fragment in msg.get("content", "")), None)


def plan_optional(session, provider, body, capacity, policy):
    core = getattr(session, "executor_core", None)
    if not getattr(core, "budget_groups", None) or not policy["threshold"]:
        return {}
    window = policy.get("apiWindow")
    limit = input_limit(capacity, window, policy["reservedOutput"])
    if not limit:
        return {}
    threshold = min(policy["threshold"], limit)
    if history.measured(provider, subset(body, required_rows(body)), limit)["tokens"] < threshold:
        return {}
    groups = core.budget_groups
    requested = _requested_text(body)
    selected = getattr(core, "budget_requested_groups", set())
    _, latest = layout(body)
    used = {call.get("function", {}).get("name")
            for message in body["messages"][latest or 0:] for call in message.get("tool_calls", [])}
    pinned = core.budget_required_tools | set().union(*(groups[g] for g in selected if g in groups)) | used
    removable = set().union(*groups.values()) - pinned
    choices = [
        ("skills", _skills_index if groups.get("skills") else None),
        ("plugins", _plugin_index if "list_plugins" in groups.get("plugins", ()) else None),
        ("pluginFacts", _fact_readers(core, requested)),
        ("extraTools", removable),
    ]
    plan = {"requested": requested}
    for name, value in choices:
        if value:
            plan[name] = value
            candidate = project_optional(body, plan)
            if history.measured(provider, subset(candidate, required_rows(candidate)), limit)["tokens"] < threshold:
                break
    return plan


def report(session, body, capacity, policy):
    groups, _ = layout(body)
    inspected = assess(body, capacity, groups=groups)
    parts = inspected["breakdown"]
    sources = {name: parts[name] for name in _LABELS if name in parts}
    sources["baseTools"] = parts["toolDefinitions"]
    core = getattr(session, "executor_core", None)
    tool_names = {tool.get("function", {}).get("name") for tool in body.get("tools", [])}
    if core is not None and hasattr(core, "budget_base_tools"):
        base = [tool for tool in body.get("tools", [])
                if tool.get("function", {}).get("name") in core.budget_base_tools]
        sources["baseTools"] = assess(dict(messages=[], tools=base))["toolTokens"]
        sources["extraTools"] = parts["toolDefinitions"] - sources["baseTools"]
    window, output = policy.get("apiWindow"), inspected["reservedOutput"]
    return dict(
        **inspected["inputGroups"], sources=sources,
        method=inspected["method"], estimated=True,
        userLimit=getattr(session, "context_user_limit", 0) or None,
        modelWindow=window, reservedOutput=output,
        inputLimit=input_limit(capacity, window, output),
        historyThreshold=policy["threshold"],
        deferred=list(getattr(body, "budget_deferred", ())),
        requestedGroups=sorted(
            group for group in getattr(core, "budget_requested_groups", ())
            if tool_names.intersection(core.budget_groups.get(group, ()))
        ),
    )


def block(session, role, budget, reason, limit_evidence=None, measurement=None):
    rejected = (limit_evidence or {}).get("kind") == "api_context_limit"
    budget = dict(budget, reason=reason, stage="provider" if rejected else "preflight")
    target = {
        "fixed": "必带的固定内容超过可用输入",
        "current": "本轮材料加上固定开销超过可用输入",
        "output": "输出预留已占满模型窗口",
        "provider": "模型接口报告上下文超限",
    }[reason]
    stage = "模型接口已拒绝这次请求" if rejected else "本轮未发送此请求"
    if reason == "provider":
        target = ("缩减后仍被模型接口判定超限" if budget["deferred"]
                  else "模型接口报告超限，当前材料无法继续安全缩减")
    if measurement:
        budget.update(decisionTokens=measurement["tokens"], decisionMethod=measurement["source"])
        if measurement["source"] == "unmeasured_input_bound":
            target = "按保守上界计算，" + target
    labels = sorted(((value, key) for key, value in budget["sources"].items()
                     if value and key != "current"), reverse=True)
    if reason == "current":
        labels.insert(0, (budget["current"], "current"))
    details = "、".join(f"{_LABELS[key]}约 {value:,}" for value, key in labels[:3])
    limit = budget["inputLimit"]
    usage = f"可用输入 {limit:,} token（输出预留 {budget['reservedOutput']:,}）" if limit is not None else "可用输入未知"
    note = f"{stage}。{target}。{usage}。"
    if details:
        note += f"本地估算主要占用 {details} token。"
    if budget["requestedGroups"] and budget["sources"].get("extraTools"):
        note += f"本轮请求的工具组 {'、'.join(budget['requestedGroups'])} 计入该额度。"
    budget["providerInput"] = (limit_evidence or {}).get("inputTokens") if rejected else None
    session.emit("context_blocked", role=role, state="blocked", reason=note, inputBudget=budget)
    raise ContextBlocked(note)


def check_required(session, provider, role, body, budget, limit_evidence=None):
    """Keep pressure on the existing history path unless the required rows already exhaust input."""
    limit = budget["inputLimit"]
    if limit is None:
        return len(required_rows(body)) == len(body["messages"])
    if limit <= 0:
        block(session, role, budget, "output", limit_evidence)
    groups, latest = layout(body)
    fixed = [i for i, group in enumerate(groups) if group == "fixed"]
    minimum = subset(body, sorted(fixed if latest is None else [*fixed, latest]))
    measured = history.measured(provider, minimum, limit)
    if measured["tokens"] < limit:
        return len(minimum["messages"]) == len(body["messages"])
    fixed_usage = history.measured(provider, subset(body, fixed), limit)
    if fixed_usage["tokens"] >= limit:
        block(session, role, budget, "fixed", limit_evidence, fixed_usage)
    block(session, role, budget, "current", limit_evidence, measured)


def report_after_rejection(session, provider, body, capacity):
    """Reaccount for the window and output allowance after an API rejection."""
    try:
        policy = history.policy(session, provider, body, capacity)
    except ContextBlocked:
        from .context_manager import model_window
        policy = dict(apiWindow=model_window(session, provider), threshold=0)
    return report(session, body, capacity, policy)


def reject_optional_retry(session, provider, role, body, capacity, evidence):
    """Use the newest API evidence and never send a third fixed-prefix attempt."""
    budget = report_after_rejection(session, provider, body, capacity)
    check_required(session, provider, role, body, budget, evidence)
    block(session, role, budget, "provider", evidence)
