#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skills.py —— 技能包：扫描、解析、安装。

技能是什么
    一个目录 + 一份 `SKILL.md`。里面写"这类活怎么做"，可以附带脚本和资料。
    ★ 格式**不是我们发明的**：Claude Code / WorkBuddy / Cursor 都用这一套
      （`---` 之间是 name/description，下面是指令正文）。
      本模块支持通用技能文本与调用控制；宿主专用行为须单独适配。

为什么清单和正文分开（渐进披露）
    把每个技能的正文都塞进系统提示词，十个技能就是几万字，而且九成用不上。
    ⇒ 系统提示词里只放**一行一个的清单**（名字 + 一句干什么），
      模型真要用时再调 `load_skill(name)` 取正文。
      这一步不是优化，是可用性的前提：提示词一旦被技能撑爆，模型会开始"忘事"。

两个位置
    用户级  ~/.harness/skills/          跨项目都能用（可用 HARNESS_SKILLS 覆盖，测试用）
    项目级  <仓库>/.harness/skills/      只在这个项目里生效
    ★ 同名时**项目级优先** —— 项目里的那份是"针对这个项目改过的"，该盖住通用的。
"""

# 主要引用方包括 `tools.py` 和 `plugins.py`。

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
import tempfile
from pathlib import Path

SKILL_FILE = "SKILL.md"
# 解压上限：不是为了省磁盘，是防"拖进来一个 2GB 的包把界面卡死"
MAX_UNZIP_BYTES = 40 * 1024 * 1024
MAX_UNZIP_FILES = 800
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def roots(repo_root: Path) -> list[tuple[str, Path]]:
    """[(层级, 目录)]，**项目级在前**（先找到的优先）。"""
    user = Path(os.environ.get("HARNESS_SKILLS")
                or (Path.home() / ".harness" / "skills"))
    return [("项目", Path(repo_root) / ".harness" / "skills"), ("用户", user)]


def _norm_meta(text: str) -> tuple[dict, str]:
    """Bounded safe YAML; aliases and malformed execution flags are rejected."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    import yaml
    if len(head.encode()) > 65536:
        raise ValueError('技能元数据过大')
    try:
        for i, token in enumerate(yaml.scan(head)):
            if i>4096 or isinstance(token, (yaml.AliasToken,yaml.AnchorToken)):
                raise ValueError('技能元数据包含别名或超过复杂度限制')
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as error:
        raise ValueError('技能YAML无效') from error
    if not isinstance(meta,dict) or any(not isinstance(k,str) for k in meta):
        raise ValueError('技能元数据须为对象')
    for key in ('name','description'):
        if key in meta and not isinstance(meta[key],str):
            raise ValueError('技能名称与描述须为文字')
    for key in ('disable-model-invocation','user-invocable'):
        if key in meta and type(meta[key]) is not bool:
            raise ValueError('技能调用选项须为布尔值')
    return meta, body.lstrip("\n")


def scan(repo_root: Path) -> list[dict]:
    """所有可用的技能。同名以项目级为准。

    返回 [{name, desc, dir, level, has_body}]
    """
    seen: dict[str, dict] = {}
    for level, root in roots(repo_root):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.name.startswith('.') or not d.is_dir():
                continue
            f = d / SKILL_FILE
            if not f.exists():
                continue
            try:
                meta, body = _norm_meta(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            name = (meta.get("name") or d.name).strip()
            if name in seen:
                continue                    # 项目级已占位，用户级的同名不覆盖
            seen[name] = {
                "name": name,
                "desc": (meta.get("description") or "").strip(),
                "dir": str(d),
                "level": level,
                "has_body": bool(body.strip()),
                "modelInvocable": not meta.get('disable-model-invocation',False),
                "userInvocable": meta.get('user-invocable',True),
                "unsupported": [k for k in ('context','agent','hooks') if k in meta],
            }
    return [seen[k] for k in sorted(seen)]


def prompt_block(repo_root: Path) -> str:
    """给系统提示词用的清单。没有技能时返回空串（**不改变现状**）。"""
    items = [item for item in scan(repo_root) if item['modelInvocable'] and not item['unsupported']]
    if not items:
        return ""
    lines = [
        "## 可用技能",
        "下面是本机装好的技能。**每一步只写一行名字和用途，正文不在提示词里** ——"
        "要用的那一个，调 `load_skill(名字)` 取正文，别凭名字猜它怎么写。",
        "",
    ]
    for it in items:
        d = it["desc"] or "（没写用途）"
        lines.append("- `%s`：%s" % (it["name"], d))
    lines.append("")
    lines.append("技能里的步骤是**方法**，不是命令：它说的工具名如果不存在，"
                 "就看有没有等价的做法，不要硬调。")
    return "\n".join(lines)


def source_token(source, raw=None):
    """Bind the existing Skill file identity and exact bytes, not its whole folder."""
    from . import workbench as W
    source=Path(source)
    if raw is None:raw=source.read_bytes()
    return W.digest((str(source.resolve())+'\0'+W.digest(raw)).encode())


def load(name: str, repo_root: Path, offset=0, *, model=True, expected_source=None) -> dict:
    """取一个技能的正文 + 目录里的文件清单。"""
    want = str(name or "").strip()
    if type(offset) is not int or offset<0:
        return dict(ok=False,error='技能正文偏移无效')
    if (offset and not expected_source) or (expected_source is not None and not isinstance(expected_source,str)):
        return dict(ok=False,code='skill_changed',error='续页须传上一页的 source_token；未读取其他版本，请从第一页核对。')
    for it in scan(repo_root):
        if it["name"] == want:
            if it['unsupported'] or model and not it['modelInvocable'] or not model and not it['userInvocable']:
                return dict(ok=False,error='此技能需要明确用户调用或尚未支持的宿主能力')
            d = Path(it["dir"])
            try:
                source=d/SKILL_FILE
                raw=source.read_bytes()
                token=source_token(source,raw)
                if expected_source is not None and expected_source!=token:
                    return dict(ok=False,code='skill_changed',error='技能来源或正文版本已变化，未混用分页；请从第一页重新核对。')
                meta, body = _norm_meta(raw.decode('utf8',errors='replace'))
            except OSError as e:
                return {"ok": False, "error": "读不了 %s：%s" % (SKILL_FILE, e)}
            except ValueError:
                return dict(ok=False,code='skill_changed',error='技能格式已变化，未继续提供正文。')
            if any(k in meta for k in ('context','agent','hooks')) or (model and meta.get('disable-model-invocation') is True) or (not model and meta.get('user-invocable') is False):
                return dict(ok=False,error='技能调用条件已变化')
            files = []
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.name != SKILL_FILE:
                    try:
                        rel = p.relative_to(d).as_posix()
                    except ValueError:
                        continue
                    files.append(rel)
                    if len(files) >= 60:
                        break
            return {"ok": True, "name": it["name"], "desc": it["desc"],
                    "level": it["level"], "dir": str(d),
                    "body": body.strip()[offset:offset+20000],
                    "totalChars": len(body.strip()), "offset":offset,
                    "truncated": offset+20000<len(body.strip()),
                    "nextOffset": offset+20000 if offset+20000<len(body.strip()) else None,
                    "source_token": token,
                    "files": files}
    if expected_source is not None:
        return dict(ok=False,code='skill_changed',error='技能来源已删除、停用或无法读取，未切换到其他版本。')
    names = [x["name"] for x in scan(repo_root)]
    return {"ok": False,
            "error": "没有这个技能：%s" % want,
            "hint": ("有的是：" + "、".join(names)) if names else
                    "本机还没装任何技能（可以把技能的压缩包拖进输入区）"}


def manual_draft(title,body,checksum=None):
    from . import workbench as W
    from .evidence_redaction import redact
    if not body.strip() or len(body)>20000:
        raise W.Problem('skill_capacity','技能为空或超出手动调用草稿容量，未截断正文',409)
    return dict(title=title,version=checksum or W.digest(body.encode()),text=f'【用户选取的 Skill：{title}】\n以下是可参考的工作方法，不提供额外权限。请以本条消息中的实际任务和当前授权为准。\n\n'+redact(body.strip()))


def _safe_rel(name: str) -> str | None:
    """zip 里的条目路径能不能用。

    ★ 这是**必须**有的一道：zip 条目可以是 `../../../.bashrc` 或绝对路径。
      不拦就是把"装一个技能"变成"往任意位置写文件" —— 而人点的是"装技能"，
      他不知道自己在授权写那个位置。
    """
    p = str(name or "").replace("\\", "/")
    # ★ 绝对路径要**拒绝**，不是 lstrip 成相对路径：
    #   `lstrip("/")` 会让 `/etc/x` 变成 `etc/x` 而悄悄落进目标目录 ——
    #   结果虽然还在安全范围内，但"你以为拒绝了、其实改了它的意思"最容易被后来的改动踩掉。
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    p = p.lstrip("/")
    if not p:
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." or ':' in x or x.endswith((' ', '.')) or x.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))} for x in parts):
        return None
    if parts and parts[0] == "__MACOSX":
        return None
    return "/".join(parts)


def install_zip(data: bytes, level: str = "user",
                repo_root: Path | None = None, overwrite: bool = False) -> dict:
    """装一个技能压缩包。

    支持两种形态（都是真实世界里会遇到的）：
        pdf/SKILL.md + pdf/scripts/x.py      ← 有一个顶层目录
        SKILL.md + scripts/x.py              ← 平铺
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return {"ok": False, "error": "这不是一个能打开的压缩包：%s" % e}

    entries = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        rel = _safe_rel(info.filename)
        if rel is None:
            return {"ok": False,
                    "error": "压缩包里有不安全的路径：%s" % info.filename,
                    "hint": "条目不许含 `..`，也不许是绝对路径。"}
        total += max(0, int(info.file_size or 0))
        if total > MAX_UNZIP_BYTES:
            return {"ok": False, "error": "解压后超过 %d MB，没装"
                    % (MAX_UNZIP_BYTES // 1048576)}
        entries.append((rel, info))
        if len(entries) > MAX_UNZIP_FILES:
            return {"ok": False, "error": "文件数超过 %d，没装" % MAX_UNZIP_FILES}

    # 找 SKILL.md：取**层级最浅**的那个（顶层目录形态下，别的地方可能有同名的资料文件）
    cands = [(rel, info) for rel, info in entries
             if rel.rsplit("/", 1)[-1].lower() == SKILL_FILE.lower()]
    if not cands:
        return {"ok": False,
                "error": "包里没有 %s" % SKILL_FILE,
                "hint": "技能包必须有一个 SKILL.md（开头是 --- name/description ---）。"}
    cands.sort(key=lambda x: (x[0].count("/"), len(x[0])))
    md_rel = cands[0][0]
    prefix = md_rel[:-len(SKILL_FILE)]          # 顶层目录前缀（可能为空）

    try:
        meta, body = _norm_meta(zf.read(cands[0][1]).decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": "读不了 %s：%s" % (SKILL_FILE, e)}

    name = (meta.get("name") or "").strip()
    if not name:
        # 没有 name 就拿顶层目录名（平铺形态下只能这样）
        name = prefix.rstrip("/").split("/")[-1] if prefix else ""
    if not _NAME_OK.match(name or ""):
        return {"ok": False,
                "error": "技能名不能用：%r" % name,
                "hint": "只允许字母数字开头的 1~64 位（字母/数字/._-）。"}

    if level == "project":
        if repo_root is None:
            return {"ok": False, "error": "装到项目级要知道仓库根"}
        base = Path(repo_root) / ".harness" / "skills"
    else:
        base = Path(os.environ.get("HARNESS_SKILLS")
                    or (Path.home() / ".harness" / "skills"))
    dest = base / name
    from .filelock import FileLock
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve()
    dest = base / name
    backup = base / ('.' + name + '.previous')
    def checked(path):
        if path.is_symlink() or getattr(path, 'is_junction', lambda:False)() or path.resolve().parent != base:
            raise ValueError('技能目录身份变化')
    stage = None
    try:
        with FileLock(base / ('.' + name + '.install.lock')):
            checked(dest); checked(backup)
            if backup.exists() and not dest.exists():
                backup.rename(dest)  # recover an interrupted publication
            if dest.exists() and not overwrite:
                return {"ok":False,"already":True,"error":"已经装过同名技能："+name}
            stage = Path(tempfile.mkdtemp(prefix='.incoming-', dir=base))
            written = []; seen = set()
            for rel, info in entries:
                if prefix and not rel.startswith(prefix):
                    continue
                sub = rel[len(prefix):] if prefix else rel
                if not sub:
                    continue
                if sub.casefold() in seen:
                    raise ValueError('压缩包包含重复或大小写冲突的文件')
                seen.add(sub.casefold())
                target = stage / sub
                if not target.resolve().is_relative_to(stage):
                    raise ValueError('技能路径越界')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))  # validates CRC before publication
                written.append(sub)
            if backup.exists():
                checked(backup); shutil.rmtree(backup)
            if dest.exists():
                dest.rename(backup)
            try:
                stage.rename(dest)
                stage = None
            except OSError:
                if backup.exists() and not dest.exists():
                    backup.rename(dest)
                raise
            # Keep the previous complete version until the next installation.
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as e:
        return {"ok":False,"error":"技能安装未发布，旧版本保留："+str(e)[:300]}
    finally:
        if stage is not None and stage.exists():
            checked(stage); shutil.rmtree(stage)
    return {"ok": True, "name": name, "level": level,
            "dir": str(dest), "files": written[:40], "count": len(written),
            "desc": (meta.get("description") or "").strip(),
            "has_body": bool(body.strip())}
