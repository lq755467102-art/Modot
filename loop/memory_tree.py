"""Versioned two-tree storage. No provider, arbitrary path or implicit migration."""

# 主要引用方包括 `habits.py` 和 `memory_providers.py`。

from copy import deepcopy
import hashlib, json, time, uuid
from .filelock import FileLock, atomic_json, read_text
from . import workbench as W

MAX_BYTES = 4 * 1024 * 1024


# 用稳定序列化计算记忆状态或归档内容的哈希。
def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def error(message, code="invalid_memory", status=400):
    raise W.Problem(code, message, status)


def text(value, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        error("记忆字段为空或过长")
    return value.strip()


class TreeStore:
    # 库名是宿主范围键，文件位于数据目录的记忆区。
    def __init__(self, key):
        if (
            not isinstance(key, str)
            or not key
            or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in key
            )
        ):
            error("记忆范围无效")
        self.path = W.home() / "memory-trees" / (key + ".json")

    # 合并近期变更和校验通过的归档片段，供原修订分页回查。
    def history_page(self, checksum, offset=0):
        row = self.load()
        if type(offset) is not int or offset<0 or not any(a['hash']==checksum for a in row.get('historyArchives', [])):
            error('记忆归档不存在', 'not_found', 404)
        path = self.path.parent/'history'/self.path.stem/(checksum+'.json')
        if path.is_symlink() or not path.resolve().is_relative_to(self.path.parent.resolve()) or path.stat().st_size>8*1024*1024:
            error('记忆归档不可核对', 'memory_unavailable', 503)
        entries = json.loads(read_text(path))
        if digest(entries)!=checksum:
            error('记忆归档版本变化', 'memory_unavailable', 503)
        return dict(history=entries[offset:offset+50],total=len(entries),nextOffset=offset+50 if offset+50<len(entries) else None)

    # 新库从空快照开始；已存在但格式不可确认的库保留原数据并报告不可用。
    def load(self):
        if not self.path.resolve().is_relative_to(W.home().resolve()):
            error("记忆路径越界", "memory_denied", 403)
        if not self.path.exists():
            return dict(
                format=1, revision=0, nodes={}, choices={}, history=[], conflicts={}
            )
        if (
            self.path.is_symlink()
            or not self.path.resolve().is_relative_to(W.home().resolve())
            or self.path.stat().st_size > MAX_BYTES
        ):
            error("记忆库不可读取", "memory_unavailable", 503)
        try:
            row = json.loads(read_text(self.path))
            assert (
                row["format"] == 1
                and isinstance(row["nodes"], dict)
                and isinstance(row["revision"], int)
            )
            return row
        except (ValueError, KeyError, AssertionError):
            error("记忆库格式异常，原数据保留", "memory_unavailable", 503)

    # 在同一写锁内校验原修订、应用变更、记审计和做容量检查，全部通过后原子替换快照。
    def change(self, expected, actor, fn):
        with FileLock(self.path.with_suffix(".lock")):
            row = self.load()
            if type(expected) is not int or expected != row["revision"]:
                error("记忆已更新，请刷新后重试", "memory_stale", 409)
            before = digest(row)
            result = fn(row)
            row["revision"] += 1
            if len(row['history']) >= 4500:
                # Publish a content-addressed complete history chunk before
                # replacing the live snapshot; a crash leaves only an orphan.
                chunk = row['history'][:-100]
                checksum = digest(chunk)
                archive = self.path.parent/'history'/self.path.stem/(checksum+'.json')
                if archive.exists():
                    if digest(json.loads(read_text(archive))) != checksum:
                        error('记忆归档校验失败', 'memory_unavailable', 503)
                else:
                    atomic_json(archive, chunk)
                row.setdefault('historyArchives', []).append(dict(hash=checksum,
                    count=len(chunk), firstRevision=chunk[0]['revision'],lastRevision=chunk[-1]['revision']))
                row['history'] = row['history'][-100:]
            row["history"].append(
                dict(
                    revision=row["revision"],
                    actor=actor,
                    at=time.time(),
                    before=before,
                    after=digest(row["nodes"]),
                )
            )
            if (
                len(row["nodes"]) > 1000
                or len(row["history"]) > 5000
                or len(json.dumps(row).encode()) > MAX_BYTES
            ):
                error("记忆容量已满，原数据未改", "memory_capacity", 409)
            atomic_json(self.path, row)
            return dict(result or {}, revision=row["revision"])

    # 新增或修改带来源的记忆节点，写入使用调用方读取的库修订。
    def upsert(self, expected, actor, body, source, *, adoption=None):
        # 核对节点原身份、范围和版本关系后生成新版本，将旧版本留给历史回查。
        def apply(row):
            tree = body.get("tree", "facts")
            if tree not in {"facts", "habits"}:
                error("只有事实树和习惯树")
            ident = body.get("id")
            old = row["nodes"].get(ident)
            title = text(body.get("title"), 120)
            content = text(body.get("body"), 8000)
            layer = body.get("layer", (old or {}).get("layer", ["项目知识"]))
            if not isinstance(layer, list) or not 1 <= len(layer) <= 6:
                error("归层路径无效")
            layer = [text(v, 80) for v in layer]
            priority = body.get("priority", (old or {}).get("priority", 50))
            weight = body.get("weight", (old or {}).get("weight", 60))
            if (
                type(priority) is not int
                or not 1 <= priority <= 999
                or type(weight) is not int
                or not 0 <= weight <= 100
            ):
                error("优先级或权重无效")
            ident = body.get("id")
            if ident and ident not in row["nodes"]:
                error("节点不存在", "not_found", 404)
            old = row["nodes"].get(ident)
            expected_version = body.get("expectedVersion", body.get("version"))
            if (
                old
                and expected_version is not None
                and old["version"] != expected_version
            ):
                error("节点原版本已变化", "memory_stale", 409)
            if old and old["tree"] != tree:
                error("跨树变更需要升格流程")
            key = text(body.get("key", (old or {}).get("key", title)), 160)
            for node in row["nodes"].values():
                if node["id"] != ident and node["tree"] == tree and node["key"] == key:
                    if node["body"] == content:
                        return dict(id=node["id"], duplicate=True)
                    error("已有同主题节点，请更新原节点", "memory_duplicate", 409)
            ident = ident or uuid.uuid4().hex
            node = dict(
                id=ident,
                tree=tree,
                key=key,
                title=title,
                body=content,
                layer=layer,
                priority=priority,
                weight=weight,
                state=(old or {}).get("state", "active"),
                version=(old or {}).get("version", 0) + 1,
                created=(old or {}).get("created", time.time()),
                updated=time.time(),
                sources=(old or {}).get("sources", []),
                evidence=(old or {}).get("evidence", []),
                versions=(old or {}).get("versions", []),
                scope=body.get('scope', (old or {}).get('scope', 'session')),
                ownerId=body.get('ownerId', (old or {}).get('ownerId')),
                entryType=body.get('entryType', (old or {}).get('entryType', 'fact')),
            )
            if node['entryType'] not in {'fact', 'rule'}:
                error('记忆条目类型无效')
            for field in ('promotion', 'habitProposal'):
                if old and field in old:
                    node[field] = deepcopy(old[field])
            if adoption is not None:
                if actor != 'human':
                    error('采用需要用户确认', 'memory_denied', 403)
                node['promotion'] = deepcopy(adoption['lineage'])
                node['evidence'] = [dict(deepcopy(e), factVersion=node['version'], origin=adoption['lineage'])
                    for e in adoption['evidence'] if e.get('factVersion') == adoption['sourceVersion'] and e['kind'] != 'injected']
            if old:
                node["versions"].append(
                    {
                        k: old[k]
                        for k in (
                            "version",
                            "body",
                            "layer",
                            "priority",
                            "weight",
                            "state",
                        )
                    }
                )
                if len(node["versions"]) > 64:
                    error("节点版本达到上限，未覆盖历史", "memory_capacity", 409)
            if source not in node["sources"]:
                node["sources"].append(source)
            node["branches"] = body.get("branches", (old or {}).get("branches", []))
            if (
                not isinstance(node["branches"], list)
                or any(not isinstance(v, str) or len(v) > 150 for v in node["branches"])
                or len(node["branches"]) > 20
            ):
                error("分支条件无效")
            node["kind"] = body.get("kind", (old or {}).get("kind", "observation"))
            if node["kind"] not in {"observation", "decision", "inference"}:
                error("验证状态由证据决定，不能自行声明")
            row["nodes"][ident] = node
            return dict(id=ident, version=node["version"])

        return self.change(expected, actor, apply)

    # 按管理动作调整已有节点的生效条件和组织，复用存储版本检查。
    def configure(self, expected, actor, ident, body, conversation_id=None):
        # 配置变更在存储锁内执行，读取当前节点后再校验权限与状态。
        def apply(row):
            node = row["nodes"].get(ident)
            if not node:
                error("节点不存在", "not_found", 404)
            if "restoreVersion" in body:
                old = next(
                    (
                        v
                        for v in node["versions"]
                        if v["version"] == body["restoreVersion"]
                    ),
                    None,
                )
                if not old:
                    error("原版本不可用", "not_found", 404)
                current = {
                    k: node[k]
                    for k in ("version", "body", "layer", "priority", "weight", "state")
                }
                node.update({k: v for k, v in old.items() if k != "version"})
                node["versions"].append(current)
                node["version"] += 1
                node["updated"] = time.time()
            if "state" in body:
                node["versions"].append(
                    {
                        k: node[k]
                        for k in (
                            "version",
                            "body",
                            "layer",
                            "priority",
                            "weight",
                            "state",
                        )
                    }
                )
                if body["state"] not in {"active", "reference", "retired"}:
                    error("节点状态无效")
                node["state"] = body["state"]
                node["version"] += 1
                node["updated"] = time.time()
            if "on" in body:
                if type(body["on"]) is not bool:
                    error("节点选择无效")
                mode = body.get("mode", "full")
                text(mode, 40)
                scope = row["choices"]
                if conversation_id:
                    scope = row.setdefault("conversationChoices", {}).setdefault(
                        conversation_id, {}
                    )
                choices = scope.setdefault(mode, {"on": [], "off": []})
                for name in ("on", "off"):
                    choices[name] = [v for v in choices[name] if v != ident]
                choices["on" if body["on"] else "off"].append(ident)
            return dict(id=ident)

        return self.change(expected, actor, apply)


def configured_ids(row, mode, conversation_id=None):
    """Explicit on/off choices, before branch, permissions and layer budget."""
    from .rulenodes import select_ids
    ids = list(row['nodes'])
    keep = select_ids(ids, ids if mode != 'off' else [], row['choices'].get(mode, {}))
    local = row.get('conversationChoices', {}).get(conversation_id, {}).get(mode, {})
    return set(select_ids(ids, keep, local))


# 按模式、允许的资料范围与字符预算选取记忆，保留未选中的原因作为审查记录。
def select(
    row,
    mode="full",
    max_chars=16000,
    branch="",
    allowed=lambda node: True,
    conversation_id=None,
    represented=(),
):
    from .charter import select_layer_rows

    nodes = list(row["nodes"].values())
    eligible = {
        n["id"]
        for n in nodes
        if n["state"] == "active"
        and n['id'] not in represented
        and (not n["branches"] or branch in n["branches"])
        and allowed(n)
    }
    layers = {}
    for node in nodes:
        if node["id"] not in eligible:
            continue
        key = node["tree"] + "/" + "/".join(node["layer"])
        group = layers.setdefault(key, dict(priority=node["priority"], chars=0))
        group["priority"] = min(group["priority"], node["priority"])
        group["chars"] += len(node["body"]) + len(node["title"]) + 8
    # Budget is charged for complete layers before leaf exceptions, matching charter.
    chosen = set(
        select_layer_rows(
            [(g["priority"], k, g["chars"]) for k, g in layers.items()], max_chars
        )
    )
    keep = configured_ids(row, mode, conversation_id)
    selected_ids = eligible & keep
    out = []
    for node in nodes:
        reason = (
            "selected"
            if node["id"] in selected_ids
            and node["tree"] + "/" + "/".join(node["layer"]) in chosen
            else "state"
            if node["state"] != "active"
            else "branch"
            if node["branches"] and branch not in node["branches"]
            else 'represented_by_habit'
            if node['id'] in represented
            else "permission"
            if node["id"] not in eligible
            else "not_selected"
            if node["id"] not in keep
            else "budget"
        )
        out.append(dict(deepcopy(node), selected=reason == "selected", reason=reason))
    return out
