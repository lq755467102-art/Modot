"""Non-destructive history pointers with preview, exact backups and reconciliation."""

# 主要引用方包括 `workbench.py` 和 `human_actions.py`。

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from . import workbench as W, runtime as RT, event_index as EI
from .filelock import FileLock, atomic_json, read_text
from .evidence_redaction import redact

VERSION = 2
INVENTORY = [
    dict(
        id="models",
        name="模型档案与角色选择",
        state="new_entry_verified",
        entry="新版设置",
        legacyWrites=False,
        paths=["/api/models"],
    ),
    dict(
        id="policy",
        name="默认权限",
        state="new_entry_verified",
        entry="新版设置",
        legacyWrites=False,
        paths=["/api/policy"],
    ),
    dict(
        id="context",
        name="本轮上下文与后续注入偏好",
        state="new_entry_verified",
        entry="对话的本轮上下文与设置",
        legacyWrites=False,
        paths=[],
    ),
    dict(
        id="skills",
        name="技能安装、移除与外部工具清单",
        state="compatibility_retained",
        entry="兼容管理：装了什么",
        legacyWrites=True,
        paths=["/api/skills/"],
    ),
    dict(
        id="memory",
        name="记忆、事实与跨项目习惯",
        state="compatibility_retained",
        entry="兼容管理：状态与记忆",
        legacyWrites=True,
        paths=["/api/facts/", "/api/memory/", "/api/habits"],
    ),
    dict(
        id="rules",
        name="规则、候选裁定、树节点、注入模式",
        state="compatibility_retained",
        entry="兼容管理：状态与规则",
        legacyWrites=True,
        paths=[
            "/api/rules/",
            "/api/proposed/",
            "/api/tree/",
            "/api/inject",
            "/api/mode",
        ],
    ),
    dict(
        id="execution",
        name="审批、一次授权、命令、MCP与运行配置",
        state="compatibility_retained",
        entry="兼容管理与按需工程工具",
        legacyWrites=True,
        paths=["/api/grants", "/api/commands/", "/api/gflow/", "/api/config"],
    ),
    dict(
        id="history",
        name="历史会话与旧循环任务",
        state="compatibility_retained",
        entry="只读历史索引；兼容管理仍保留原记录",
        legacyWrites=True,
        paths=["/api/sessions", "/api/jobs", "/api/diagnose"],
    ),
]


# 以旧状态的实测文件信息生成可复核的版本指纹。
def fingerprint(value):
    return W.digest(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def format_path():
    return W.home() / "data-format.json"


# 从当前目录的管理记录判断运行时可读的格式和迁移状态。
def format_state():
    path = format_path()
    if not path.exists():
        return dict(schemaVersion=1, minWriter=1, compatible=True)
    try:
        if path.stat().st_size > 8192:
            raise ValueError("capacity")
        row = json.loads(read_text(path))
        if (
            not isinstance(row, dict)
            or type(row.get("schemaVersion")) is not int
            or type(row.get("minWriter")) is not int
        ):
            raise ValueError("format")
        return dict(
            row,
            compatible=1 <= row["schemaVersion"] <= VERSION
            and 1 <= row["minWriter"] <= VERSION,
        )
    except (OSError, ValueError, TypeError):
        return dict(
            schemaVersion=None,
            minWriter=None,
            compatible=False,
            notice="数据格式记录无法核对，写入已阻止；原数据保留",
        )


# 遇到本版本无法确认的状态格式时拒绝管理动作，保留原数据。
def ensure_compatible():
    if not format_state()["compatible"]:
        raise W.Problem(
            "data_version",
            "数据版本不兼容或不可读取，禁止破坏性写入；请保留并备份原数据",
            409,
        )


# 对未声明新版协议的客户端检查禁止写入的旧路径，避免旧客户端修改新格式状态。
def check_client(path, version):
    ensure_compatible()
    if str(version) == "2":
        return
    if any(
        path == p for row in INVENTORY if not row["legacyWrites"] for p in row["paths"]
    ):
        raise W.Problem(
            "entry_migrated",
            "此写入口已在新版设置验收，请返回新版；旧数据和其他兼容管理入口保留",
            409,
        )
    if format_state()["schemaVersion"] == 2 and path.startswith("/api/v2/"):
        raise W.Problem(
            "client_version", "数据已升级，请使用支持版本2的客户端；未写入", 409
        )


# 从约定的 `runs` 目录读取旧会话元数据，限制记录位置和内容大小，避免纳入无关目录。
def metadata(directory, data=None):
    directory = Path(directory)
    source = W.P.data_root() / "runs"
    if (
        not directory.resolve().is_relative_to(source.resolve())
        or directory.is_symlink()
    ):
        return dict(
            id=directory.name,
            workspaceId=None,
            cwd=None,
            hashes={},
            reason="历史目录为外部链接，未读取或自动关联",
            unsupported=True,
        )
    hashes = {}
    content = {}
    reason = "没有可证明的工作目录关联"
    cwd = None
    title = directory.name
    for name in ("session.json", "cwd.txt"):
        path = directory / name
        if not path.exists():
            continue
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(directory.resolve())
            or path.stat().st_size
            > (2 * 1024 * 1024 if name == "session.json" else 8192)
        ):
            reason = "元数据超出边界或存在链接，关联未知"
            return dict(
                id=directory.name,
                workspaceId=None,
                cwd=None,
                hashes=hashes,
                reason=reason,
                unsupported=True,
            )
        raw = path.read_bytes()
        hashes[name] = W.digest(raw)
        try:
            value = (
                json.loads(raw.decode("utf-8-sig"))
                if name == "session.json"
                else raw.decode("utf-8-sig").strip()
            )
            if name == "session.json" and isinstance(value, dict):
                caption = value.get("title") or value.get("task")
                if isinstance(caption, str) and caption.strip():
                    title = caption.strip().splitlines()[0][:100]
            content[name] = (
                value.get("cwd")
                if isinstance(value, dict)
                else value
                if name == "cwd.txt"
                else None
            )
        except (ValueError, UnicodeError):
            reason = "历史元数据损坏，关联未知"
    paths = []
    for value in content.values():
        if isinstance(value, str) and value and Path(value).is_absolute():
            paths.append(str(Path(value).resolve()))
    if paths and len(set(os.path.normcase(p) for p in paths)) == 1:
        cwd = paths[0]
        reason = "仅以原始cwd元数据为证；索引不授予目录访问或续跑权限"
    elif len(paths) > 1:
        reason = "session.json与cwd.txt不一致，未猜造关联"
    matches = [
        w["id"]
        for w in (data if data is not None else W.registry())["workspaces"].values()
        if w.get("kind") != "conversation"
        and cwd
        and os.path.normcase(w["path"]) == os.path.normcase(cwd)
    ]
    return dict(
        id=directory.name,
        title=title,
        workspaceId=matches[0] if len(matches) == 1 else None,
        cwd=cwd,
        hashes=hashes,
        reason=reason,
        unsupported=False,
        legacyTrace=not (directory / "events.jsonl").exists(),
        runtime=RT.status(directory)["state"],
    )


# 根据现有会话索引和旧目录可读性筛选迁移对象，保留无法迁移的原因。
def candidates():
    root = W.P.data_root() / "runs"
    result = []
    data = W.registry()
    threads = data["threads"]
    scanned = 0
    total_bytes = 0
    more = False
    if root.exists():
        import itertools

        entries = list(itertools.islice(root.iterdir(), 10001))
        if len(entries) > 10000:
            raise W.Problem(
                "migration_capacity", "数据根目录超过1万项，请先保留并分批整理", 409
            )
        for directory in sorted(entries):
            if (
                not directory.is_dir()
                or directory.name == "workbench"
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", directory.name)
                or directory.name in data.get("legacyHistory", {})
                or threads.get(directory.name, {}).get("projectless")
            ):
                continue
            if (
                not (directory / "session.json").exists()
                and not (directory / "trace.jsonl").exists()
            ):
                continue
            scanned += 1
            if scanned > 1000:
                raise W.Problem(
                    "migration_capacity",
                    "旧会话超过1000项，请先导出分批核对；未假装完成全部扫描",
                    409,
                )
            size = sum(
                min((directory / name).stat().st_size, 2 * 1024 * 1024 + 1)
                for name in ("session.json", "cwd.txt")
                if (directory / name).exists()
            )
            if len(result) >= 100 or (result and total_bytes + size > 16 * 1024 * 1024):
                more = True
                break
            result.append(metadata(directory, data))
            total_bytes += size
    return result, more


# 从原计划 ID 解析宿主持有的计划文件。
def plan_path(ident):
    if not re.fullmatch(r"[0-9a-f]{32}", str(ident)):
        raise W.Problem("invalid_plan", "迁移计划标识无效")
    return W.home() / "migration-plans" / (ident + ".json")


# 提取会随用户确认冻结的迁移范围、指纹与格式事件。
def approval_content(plan):
    return {
        key: plan[key]
        for key in (
            "id",
            "created",
            "candidates",
            "hasMore",
            "registryBefore",
            "formatBefore",
            "previewNotice",
        )
    }


# 读取被确认的原计划并核对哈希；原计划丢失时需要创建新预览。
def load_plan(ident):
    path = plan_path(ident)
    if not path.exists() or path.stat().st_size > 4 * 1024 * 1024:
        raise W.Problem(
            "migration_plan_missing", "迁移计划缺失或超限，未重建旧确认", 409
        )
    plan = json.loads(read_text(path))
    try:
        if fingerprint(approval_content(plan)) != plan["planHash"]:
            raise ValueError("approval content changed")
    except (ValueError, KeyError, TypeError):
        raise W.Problem(
            "migration_plan_changed",
            "迁移预览内容与原确认版本不符；未执行，原数据保留",
            409,
        )
    return plan


# 扫描可迁移会话并保存带哈希的预览，为后续明确确认提供原对象。
def preview():
    ensure_compatible()
    directory = W.home() / "migration-plans"
    directory.mkdir(exist_ok=True)
    if len(list(directory.glob("*.json"))) >= 100:
        raise W.Problem(
            "migration_capacity", "迁移预览达到100份，请先保留并核对记录", 409
        )
    before = W.registry()
    rows, more = candidates()
    plan = dict(
        id=uuid.uuid4().hex,
        state="prepared",
        created=time.time(),
        candidates=rows,
        hasMore=more,
        registryBefore=fingerprint(before),
        formatBefore=format_state(),
        notice="只添加只读历史指针，不改写原会话，不建立项目授权，不导入未知来源为成果",
    )
    plan["previewNotice"] = plan["notice"]
    plan["planHash"] = fingerprint(approval_content(plan))
    if len(json.dumps(plan).encode()) > 2 * 1024 * 1024:
        raise W.Problem("migration_capacity", "预览元数据超限，尚未创建迁移", 409)
    atomic_json(plan_path(plan["id"]), plan)
    return plan


# 以既有登记和已核对候选构建迁移后的索引。
def migrated_registry(before, plan):
    after = json.loads(json.dumps(before))
    history = after.setdefault("legacyHistory", {})
    for row in plan["candidates"]:
        history.setdefault(row["id"], dict(row, readonly=True, migratedBy=plan["id"]))
    after["schemaVersion"] = VERSION
    return after


# 用计划 ID、备份清单和内容哈希核对回退材料是否完整。
def verified_backup(plan):
    directory = Path(plan["backup"])
    root = (W.home() / "migration-backups").resolve()
    if (
        directory.name != plan["id"]
        or not directory.resolve().is_relative_to(root)
        or directory.is_symlink()
    ):
        raise W.Problem("migration_backup", "原备份身份无法核对", 409)
    manifest_path = directory / "manifest.json"
    if (
        not manifest_path.exists()
        or manifest_path.stat().st_size > 8192
        or manifest_path.is_symlink()
    ):
        raise W.Problem("migration_backup", "原备份清单缺失或异常，未继续迁移", 409)
    manifest = json.loads(read_text(manifest_path))
    if set(manifest.get("files", {})) != {"registry.json", "data-format.json"}:
        raise W.Problem("migration_backup", "原备份清单不完整，未继续迁移", 409)
    for name, expected in manifest["files"].items():
        file = directory / name
        if (
            file.is_symlink()
            or expected is None
            and file.exists()
            or expected is not None
            and (
                not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not file.exists()
                or file.stat().st_size > 4 * 1024 * 1024
                or W.digest(file.read_bytes()) != expected
            )
        ):
            raise W.Problem("migration_backup", "原备份内容校验失败，未继续迁移", 409)
    original = directory / "registry.json"
    before = (
        json.loads(read_text(original))
        if original.exists()
        else dict(workspaces={}, threads={}, presets={}, reviews={})
    )
    if fingerprint(before) != plan["registryBefore"]:
        raise W.Problem(
            "migration_backup", "备份不是原审批的工作记录版本，未继续迁移", 409
        )
    return before


# 迁移前把状态和会话元数据保存在独立目录，再写可核对清单。
def write_backup(plan):
    directory = W.home() / "migration-backups" / plan["id"]
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for name, path in (
        ("registry.json", W.home() / "registry.json"),
        ("data-format.json", format_path()),
    ):
        if path.exists():
            if path.stat().st_size > 4 * 1024 * 1024:
                raise W.Problem(
                    "migration_capacity", "待备份元数据超过4MiB，原文件未修改", 409
                )
            raw = path.read_bytes()
            with (directory / name).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            manifest[name] = W.digest(raw)
        else:
            manifest[name] = None
    atomic_json(
        directory / "manifest.json",
        dict(
            files=manifest,
            created=time.time(),
            notice="仅备份本迁移将修改的元数据；原始会话和资料完全保留在原位置",
        ),
    )
    return str(directory)


# 核对用户确认和原计划哈希后才备份并写索引；已成功的计划可返回原回执。
def apply(body):
    ensure_compatible()
    plan = load_plan(body.get("planId"))
    if body.get("confirm") is not True or body.get("planHash") != plan["planHash"]:
        raise W.Problem("migration_confirmation", "迁移须确认当前完整预览版本", 409)
    if plan["state"] == "applied":
        return plan
    if plan["state"] != "prepared":
        raise W.Problem(
            "migration_unknown",
            "原迁移不是未执行预览，请先核对中断状态；不自动重放",
            409,
        )
    with FileLock(W.home() / "migration.lock"), FileLock(W.home() / "registry.lock"):
        before = W.registry()
        if (
            fingerprint(before) != plan["registryBefore"]
            or fingerprint(
                [
                    metadata(W.P.data_root() / "runs" / row["id"], before)
                    for row in plan["candidates"]
                ]
            )
            != fingerprint(plan["candidates"])
            or format_state() != plan["formatBefore"]
        ):
            raise W.Problem(
                "migration_stale",
                "历史或工作记录已变化，请重新预览；旧确认未被复用",
                409,
            )
        after = migrated_registry(before, plan)
        plan.update(
            state="backing_up",
            registryAfter=fingerprint(after),
            backup=str(W.home() / "migration-backups" / plan["id"]),
        )
        atomic_json(plan_path(plan["id"]), plan)
        try:
            write_backup(plan)
            plan["state"] = "applying"
            atomic_json(plan_path(plan["id"]), plan)
            atomic_json(W.home() / "registry.json", after)
            atomic_json(format_path(), dict(schemaVersion=VERSION, minWriter=VERSION))
            plan.update(state="applied", finished=time.time())
            atomic_json(plan_path(plan["id"]), plan)
        except Exception:
            plan.update(
                state="unknown",
                notice="迁移写入未确认，原文件和备份保留；请核对中断迁移，未自动重放",
            )
            try:
                atomic_json(plan_path(plan["id"]), plan)
            except OSError:
                pass
    return plan


# 对中断或未知的原计划比较现存状态与备份，先核对已有结果。
def reconcile(body):
    ensure_compatible()
    plan = load_plan(body.get("planId"))
    if body.get("confirm") is not True or body.get("planHash") != plan["planHash"]:
        raise W.Problem("migration_confirmation", "请确认要核对的原迁移计划", 409)
    if plan["state"] not in {"backing_up", "applying", "unknown"}:
        return plan
    with FileLock(W.home() / "migration.lock"), FileLock(W.home() / "registry.lock"):
        current = fingerprint(W.registry())
        if current == plan.get("registryAfter"):
            before = verified_backup(plan)
            if fingerprint(migrated_registry(before, plan)) != plan["registryAfter"]:
                raise W.Problem(
                    "migration_checkpoint",
                    "中断目标记录不是原预览推导结果，未继续迁移",
                    409,
                )
            atomic_json(format_path(), dict(schemaVersion=VERSION, minWriter=VERSION))
            plan.update(
                state="applied",
                finished=time.time(),
                notice="只核对并完成原元数据版本标记，没有重新导入或执行旧任务",
            )
        elif current == plan["registryBefore"]:
            plan.update(
                state="not_applied",
                notice="工作记录仍为原版本；未重放，请按新预览决定是否迁移",
            )
        else:
            plan.update(
                state="unknown",
                notice="当前记录已分叉，未覆盖；请保留备份和当前文件人工核对",
            )
        atomic_json(plan_path(plan["id"]), plan)
    return plan


# 给管理页列出保留计划以及仍需核对的状态。
def plan_summaries():
    result = []
    paths = sorted(
        (W.home() / "migration-plans").glob("*.json"),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )[:20]
    for path in paths:
        try:
            plan = load_plan(path.stem)
            result.append(
                {
                    k: v
                    for k, v in plan.items()
                    if k
                    in {
                        "id",
                        "state",
                        "created",
                        "planHash",
                        "backup",
                        "notice",
                        "hasMore",
                    }
                }
            )
        except (W.Problem, OSError, ValueError, KeyError, TypeError):
            result.append(
                dict(
                    id=path.stem,
                    state="unknown",
                    notice="该计划文件无法核对，原文件保留；其他历史与管理能力仍可查看",
                )
            )
    return result


# 分流到旧会话只读历史、迁移预览、确认和中断核对，持续指向原记录。
def route(parts, method, body, query, sv):
    if len(parts) > 2 and parts[1] == "plans" and method == "GET":
        return 200, load_plan(parts[2])
    if len(parts) > 1 and parts[1] == "history":
        history = W.registry().get("legacyHistory", {})
        if len(parts) == 2:
            return 200, dict(
                items=list(history.values()),
                notice="历史索引只读，旧运行和资料未自动授权",
            )
        if method != "GET" or parts[2] not in history:
            raise W.Problem("legacy_readonly", "旧历史仅可只读核对", 403)
        directory = W.P.data_root() / "runs" / parts[2]
        if not directory.is_dir():
            raise W.Problem("legacy_missing", "原历史目录缺失，未推测完成状态", 404)
        current = metadata(directory)
        if current.get("unsupported"):
            raise W.Problem(
                "legacy_unavailable",
                "原历史元数据不可核对，未读取外部链接或推测授权",
                409,
            )
        before = (query.get("before_offset") or [None])[0]
        rows, total, meta = EI.page(
            directory / "events.jsonl",
            limit=201,
            tail=True,
            before_position=int(before) if before else None,
        )
        expected = (query.get("generation") or [None])[0]
        if expected and expected != meta["generation"]:
            raise W.Problem(
                "history_changed",
                "原日志版本已变化，请回到最新原记录，旧游标不能证明连续",
                409,
            )
        more = len(rows) > 200 or bool(meta.get("pageClipped"))
        rows = rows[-200:]
        if not (directory / "events.jsonl").exists():
            meta.update(
                gap=True,
                historyNotice="原事件日志缺失或仅有旧trace，未推测完成；原文件保留供兼容查看",
            )
        from .conversation_record import timeline_events

        return 200, dict(
            item=history[parts[2]],
            events=redact(timeline_events(directory / "events.jsonl", rows, meta)),
            total=total,
            hasMore=more,
            beforeOffset=min((r["sourceOffset"] for r in rows), default=0),
            **meta,
        )
    if method == "GET":
        all_history = list(W.registry().get("legacyHistory", {}).values())
        history_offset = max(0, min(100000, int((query.get("offset") or ["0"])[0])))
        return 200, dict(
            format=format_state(),
            inventory=INVENTORY,
            history=all_history[history_offset : history_offset + 50],
            totalHistory=len(all_history),
            plans=plan_summaries(),
            notice="退回兼容界面保留所有新增数据和两套UI草稿；兼容入口不是降级迁移",
        )
    operation = body.get("operation")
    result = (
        preview()
        if operation == "preview"
        else apply(body)
        if operation == "apply"
        else reconcile(body)
        if operation == "reconcile"
        else None
    )
    if result is None:
        raise W.Problem("invalid_migration", "未知迁移操作")
    return (202 if result["state"] == "unknown" else 200), result
