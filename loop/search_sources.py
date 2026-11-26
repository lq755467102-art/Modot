"""Projection of structured search citations returned by an authorized plugin."""

# 主要引用方包括 `workbench.py` 和 `evidence.py`。

import json
from urllib.parse import urlsplit, urlunsplit

from . import evidence, plugins
from .evidence_redaction import redact


def citations(result):
    if result.get("ok") is not True or not isinstance(result.get("pluginId"), str):
        return []
    reply = result.get("result")
    structured = reply.get("structuredContent") if isinstance(reply, dict) else None
    rows = structured.get("sources") if isinstance(structured, dict) else None
    if not isinstance(rows, list):
        return []
    sources = {}
    for row in rows[:30]:
        if not isinstance(row, dict):
            continue
        url, title = row.get("url"), row.get("title")
        if (not isinstance(url, str) or not isinstance(title, str) or len(url) > 1800 or "\\" in url
                or any(c.isspace() or ord(c) < 32 for c in url)):
            continue
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                continue
        except ValueError:
            continue
        normalized = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))
        if redact(normalized) != normalized:
            continue
        sources.setdefault(normalized, dict(url=normalized, title=redact(" ".join(title.split())[:140]) or parts.hostname))
    return list(sources.values())[:12]


def listing(ident, sv):
    if not sv._resolve_sid(ident):
        return {"items": []}
    grants = {(p["id"], p["fingerprint"], p["revision"]) for p in plugins.grants(ident) if p["remoteEnabled"]}
    if not grants:
        return {"items": []}
    root = evidence.directory(ident, sv)
    items = {}
    with evidence.projection(root) as (db, _):
        rows = db.execute("SELECT data FROM actions WHERE tool='call_plugin_tool' AND state='completed' ORDER BY position DESC LIMIT 200")
        for (raw,) in rows:
            action = json.loads(raw)
            impact = action.get("impact") or {}
            search = impact.get("search") or {}
            key = (search.get("pluginId"), search.get("fingerprint"), search.get("revision"))
            if (action.get("sourceUnverified") or not action.get("sourceRefs") or not action.get("runId")
                    or key not in grants or not search.get("items")):
                continue
            evidence._verify_source(root, action, db)
            for source in search.get("items", []):
                items.setdefault((action["runId"], source["url"]), dict(source, runId=action["runId"], tool=search["tool"]))
            if len(items) >= 100:
                break
    return {"items": list(items.values())[:100]}
