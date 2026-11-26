#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memperms.py —— 记忆的**权限分层**：谁能对哪一层记忆做哪一档事。

为什么要有它（2026-10-26 用户：「直接把 agent 记忆权限分层补齐吧」）
    三层记忆（习惯 / 规则 / 事实）之前是**三种零散的处理**：
      习惯区人写、规则区人写 + agent 提、事实区 agent 写（还没开）。
    零散的直接后果是"到底谁有权改"没人说得清，而且**没地方写**：
    你想让一个子 agent 能提案，唯一办法是往它的 `tools:` 里点名一个工具名 ——
    那是在写工具清单，不是在写权限；两者混在一起，权限就没有一张自己的表。

★ 三个区 × 四个档（档是递增的，不是并列的）

    none     连看都不看（不注入，也拿不到任何记忆工具）
    read     注入 + 可读（默认值）
    propose  能提提案 —— **不生效**，等人采纳
    edit     能直接改 —— 带备份、带审计、界面上一眼看得到

  · 规则区跟习惯区四档都开（`edit` 默认谁都不给）；
  · 事实区**没有 propose 档**：那是 agent 自己的笔记本，提给自己批没有意义。
    所以事实区只有 none / read / edit 三档。

★ 为什么在**工具注册**处生效，而不是在提示词里写"你不许改"
    用提示词说"你不能改"是最贵的一种假约束：它占预算，而模型一旦决定要改，
    它照样能改 —— 因为工具还在手上。写不了就是拿不到那个工具
    （这条和子 agent 的 `tools:` 白名单是同一个理由）。

★ 声明只有一个地方：类型定义的 frontmatter `memory_tiers:`
    memory_tiers: rules=propose, habits=read
  写错区名/档名**加载就报错**，不悄悄按默认跑 ——
  「我想给它提案权，但拼错了一个字母」这种错，静默的后果是它一直没提案权，
  而人以为给了。

★ 一个刻意的分工（不要合并）
    `memory:`       → 事实区的**作用域**（user / project / local），管"哪一份"
    `memory_tiers:` → 三个区各自的**档位**，管"能做几档事"
  两个键名长得很像，所以各管一件事，不互相推断。唯一的一条联动：
  写了 `memory:` 就等于事实区给了 `read`（不然给了位置却不给它看，没道理）。
"""
# 主要引用方包括 `tools.py` 和 `facts.py`。
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REGIONS = ("habits", "rules", "facts")
TIERS = ("none", "read", "propose", "edit")
RANK = {t: i for i, t in enumerate(TIERS)}

# 每个区**允许**的档位。事实区没有 propose —— 见模块注释。
GRANTS = {
    "habits": ("none", "read", "propose", "edit"),
    "rules": ("none", "read", "propose", "edit"),
    "facts": ("none", "read", "edit"),
}

# 没声明时用哪一套。事实区默认 none：不写 `memory:` 就不注入（不做"全局默认开"）。
DEFAULT_SUB = {"habits": "read", "rules": "read", "facts": "none"}
# 主 agent 是人自己那一半：能提两个区的提案，事实区能直接写（它是人自己的笔记）。
DEFAULT_MAIN = {"habits": "propose", "rules": "propose", "facts": "edit"}

# 某个区到了某个档，多出来的是哪个工具。**这张表就是"权限 → 工具"的全部映射**。
TOOL_AT = {
    ("rules", "propose"): "propose_rule",
    ("habits", "propose"): "propose_habit",
    ("facts", "edit"): "remember",
    ("rules", "edit"): "edit_memory",
    ("habits", "edit"): "edit_memory",
}

# 这些工具**只能**由档位授予，不许写进 `tools:`（写进去=两处授权，必打架）
MEM_TOOLS = tuple(sorted({v for v in TOOL_AT.values()}))

# 中文名：界面上给人看的（不给人看内部名）
REGION_CN = {"habits": "习惯", "rules": "规则", "facts": "事实"}
TIER_CN = {"none": "不看", "read": "只读", "propose": "可提案", "edit": "可直改"}


class MemError(ValueError):
    """档位声明写错了。加载期抛，不静默回退。"""


def rank(tier: str) -> int:
    return RANK.get(str(tier or "").strip().lower(), -1)


def at_least(mem: dict, region: str, tier: str) -> bool:
    """这一套权限在 `region` 上够不够 `tier`。"""
    cur = (mem or {}).get(region) or "none"
    return rank(cur) >= rank(tier)


def tools_for(mem: dict) -> list[str]:
    """这套权限**额外**解锁哪些记忆工具（按区、按档，去重保序）。"""
    out: list[str] = []
    for (region, tier), name in TOOL_AT.items():
        if at_least(mem, region, tier) and name not in out:
            out.append(name)
    return out


def parse(raw, where: str = "") -> dict:
    """解析 `memory_tiers:` 那一行。空 ⇒ 空 dict（由调用方叠默认值）。

    支持 `rules=propose, habits=read` / `rules:propose habits:read` 两种写法。
    ★ 区名、档名、区的档位上限，**任何一项不对都抛** —— 悄悄按默认跑是最坏的。
    """
    s = str(raw or "").strip()
    if not s:
        return {}
    out: dict = {}
    for piece in s.replace(",", " ").replace(";", " ").split():
        if "=" in piece:
            k, v = piece.split("=", 1)
        elif ":" in piece:
            k, v = piece.split(":", 1)
        else:
            raise MemError("%s的 memory_tiers 里 `%s` 不是一个 `区=档` 的写法"
                           "（例：rules=propose, habits=read）"
                           % (where, piece))
        region = k.strip().lower()
        tier = v.strip().lower()
        if region not in REGIONS:
            raise MemError("%s的 memory_tiers 写了没有这个区：`%s`（可选：%s）"
                           % (where, region, "、".join(REGIONS)))
        if tier not in TIERS:
            raise MemError("%s的 memory_tiers 里 %s 的档位是 `%s`，没有这一档"
                           "（可选：%s）" % (where, region, tier, "、".join(TIERS)))
        if tier not in GRANTS[region]:
            raise MemError(
                "%s的 memory_tiers 给 %s 写了 `%s`，这个区没有这一档（可选：%s）。"
                "事实区是 agent 自己的笔记，没有「提案」这一档 —— 要写就直接给 edit。"
                % (where, region, tier, "、".join(GRANTS[region])))
        out[region] = tier
    return out


def main_tiers(cfg: dict | None = None) -> dict:
    """主 agent 的记忆权限表：默认 `DEFAULT_MAIN`，可被 `charter.json` 的 `memory_tiers` 覆盖。

    ★ 为什么从代码搬进配置（2026-10-26）：原来三处（工具注册 / 启动 / 界面展示）
      各自 `dict(DEFAULT_MAIN)`，想给某个项目放宽到 `edit` 只能改代码 ——
      而"这个项目要放宽到什么程度"本来就是配置该管的事。
    ★ 为什么抽成一个函数：三处各写一遍 `dict(DEFAULT_MAIN)`，
      改的时候必然漏一处，而漏的那处表现是"界面上写着 edit、实际拿不到工具"。
    ★ 写错**仍然抛**（沿用 `parse` 的规矩）：配错了按默认跑，等于"配了没生效"。
    """
    out = dict(DEFAULT_MAIN)
    try:
        if cfg is None:
            from . import charter as CH
            cfg = CH.load_config()
        raw = (cfg or {}).get("memory_tiers")
        if raw:
            out.update(parse(raw, "charter.json 的 memory_tiers"))
    except MemError:
        raise
    except ValueError as _e:
        # ★ charter.json 整体坏掉（JSON 不合法）时**不能**按默认跑（2026-10-26 自查）：
        #   那就变成"我配了但要没生效"，而且没有任何提示 —— 和这个函数上面
        #   「写错仍然抛」的说法直接矛盾。当初只 catch 了 MemError，漏了这一层。
        raise MemError("charter.json 的 memory_tiers 读不出来：%s" % _e)
    except OSError:
        pass          # 配置文件不存在 = 用默认，这是正常情况
    return out


def resolve(spec_meta: dict, where: str = "", main: bool = False) -> dict:
    """从类型定义（或主 agent）算出最终权限表。

    叠法：档位默认 → `memory:` 给了作用域就把事实区抬到 read → `memory_tiers` 覆盖。
    """
    mem = main_tiers() if main else dict(DEFAULT_SUB)
    scope = str(spec_meta.get("memory") or "").strip().lower()
    if scope and not main:
        mem["facts"] = "read"          # 给了位置就得给它看，不然"memory: project"是个空承诺
    mem.update(parse(spec_meta.get("memory_tiers"), where))
    return mem


def describe(mem: dict) -> str:
    """一行中文，给界面和系统提示用。"""
    return " · ".join("%s=%s" % (REGION_CN.get(k, k), TIER_CN.get(v, v))
                      for k, v in mem.items() if v and v != "none")


# ───────────────────── 直改档：备份 + 审计 ─────────────────────
# ★ 为什么 edit 档必须有这两样（2026-10-26）
#   `edit` 是唯一能让机器在自己不在场时改变"以后怎么做"的档 ——
#   也就是「机器不写教训」那条铁律唯一被迫松口的地方。
#   松口可以，但必须**留得下证据**：改前留一份原样，改后记一行"谁改的、为什么"。
#   否则那条铁律就只剩下一个说法，而说法是拦不住任何事的。

def audit_path(region: str, repo_root=None) -> Path:
    """审计落在**它改的那个东西旁边**（落点跟着身份走，不跟着调用者走）。

    习惯区是跨项目的 ⇒ `~/.harness/`；规则区和事实区是这个项目的 ⇒ `<repo>/.harness/`。
    `HARNESS_MEM_AUDIT` 可以整体指到别处 —— 探针要用，不然会往真实 home 里写记录。
    """
    import os
    env = os.environ.get("HARNESS_MEM_AUDIT")
    if env:
        return Path(env)
    if region == "habits":
        return Path.home() / ".harness" / "mem-audit.jsonl"
    return Path(repo_root or ".") / ".harness" / "mem-audit.jsonl"


def backup(region: str, src: Path, repo_root=None) -> str:
    """改之前把原件留一份。返回备份路径（空串=没留成，调用方要当错误处理）。

    ★ 文件名必须**唯一到毫秒**（2026-10-26 实测撞出来的）：
      原来叫 `<秒级时间>-<原文件名>`，于是**同一秒内的两次备份会互相覆盖**。
      而那正好发生在最要紧的时刻 —— `revert` 先给当前这份留底、再盖回旧的，
      两次都在同一秒 ⇒ **留底把"要盖回去的那份"冲掉了**，
      回退等于把脏内容又写了一遍，而且报的是"回退成功"。
      ⇒ 加了毫秒 + 4 位随机后缀；仍撞上（极端）就加序号，绝不覆盖已有备份。
    """
    try:
        src = Path(src)
        if not src.exists():
            return ""
        base = audit_path(region, repo_root).parent / "mem-backups"
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % int(time.time() * 1000 % 1000)
        uniq = "%04x" % (int.from_bytes(os.urandom(2), "big") & 0xFFFF)
        dst = base / ("%s-%s-%s" % (stamp, uniq, src.name))
        n = 1
        while dst.exists():                       # 绝不覆盖：备份是最后一道底
            n += 1
            dst = base / ("%s-%s-%d-%s" % (stamp, uniq, n, src.name))
        dst.write_bytes(src.read_bytes())
        return str(dst)
    except OSError:
        return ""


def log(region: str, action: str, title: str, why: str,
        repo_root=None, src: str = "", bak: str = "", actor: str = "") -> None:
    """记一行审计。★ 写失败**不抛** —— 记录不能反过来把活搞崩。

    ★ 但"记不上"这件事要说出来：`ok` 为假时调用方要把这句话带进返回里，
      否则审计表坏了而没人知道，比没有审计更坏（人以为有人在看着）。
    """
    p = audit_path(region, repo_root)
    row = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "region": region,
           "act": action, "title": str(title or "")[:80],
           "why": str(why or "")[:300], "src": actor or _actor(),
           "file": src, "backup": bak}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _actor() -> str:
    import os
    s = os.environ.get("HARNESS_SESSION") or ""
    return Path(s.rstrip("/\\")).name if s else ""


def read_audit(region: str, repo_root=None, limit: int = 50) -> list[dict]:
    """读审计表（尾部 limit 条，新的在前）。读不到就是空表，不抛。

    ★ 规则区和事实区**共用同一个文件**（都在 `<repo>/.harness/`），所以必须按
      `region` 过滤 —— 不过滤的话 `all_audit` 会把同一条记录列出两遍，
      而"数量"是这页最直观的东西，重一遍就没人信了。
    """
    p = audit_path(region, repo_root)
    rows: list[dict] = []
    try:
        if not p.exists():
            return rows
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if str(r.get("region") or "") == region:
                rows.append(r)
    except OSError:
        return rows
    return list(reversed(rows))[:max(1, int(limit or 50))]


def all_audit(repo_root=None, limit: int = 50) -> list[dict]:
    """三个区合起来，按时间倒序。界面上一屏看"agent 最近动过哪些记忆"。"""
    rows = []
    for r in REGIONS:
        rows.extend(read_audit(r, repo_root, limit))
    rows.sort(key=lambda x: str(x.get("t") or ""), reverse=True)
    return rows[:max(1, int(limit or 50))]


# ───────────────────── 回退：把一条留痕退回去 ─────────────────────
# ★ 为什么回退比"备份"更要紧（2026-10-26）
#   只留备份、不留回退入口，等于把"能捡回来"变成"你得知道备份存在哪、再手动 cp"——
#   那件事在出事的那天没人做得对。所以备份和回退必须是同一件事的两半。
#
# ★ 回退本身也要能回退
#   把备份盖回去之前，先把**当前**这份再留一份。否则"回退错了"就变成终局 ——
#   而人按回退按钮的时候，恰恰是最不确定的时候。

def _bak_dir(region: str, repo_root=None) -> Path:
    return audit_path(region, repo_root).parent / "mem-backups"


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def target_ok(region: str, src: str, repo_root=None) -> bool:
    """这个 `src` 是**这个区允许改的那种文件**吗。

    ★ 为什么必须校验：`revert` 会拿一个路径去覆盖另一个路径。
      不校验的话，"回退"就是一个**任意文件覆盖**的原语 ——
      而接口还没有鉴权（见欠账），那等于把写任意文件的能力开给本机所有程序。
      所以这里只认三件事：习惯区就是 `HABITS.md`；规则区是某棵树里的 `tree/*.md`；
      事实区是 `.harness/agent-memory*/` 下的 `MEMORY.md`。
    """
    from . import charter as CH
    p = Path(str(src or ""))
    if not p.name:
        return False
    if region == "habits":
        import os
        want = Path(os.environ.get("HARNESS_HABITS")
                    or (Path.home() / ".harness" / "HABITS.md"))
        return p.resolve() == want.resolve()
    base = Path(repo_root or ".").resolve()
    if region == "rules":
        try:
            root, _ = CH.resolve_root(CH.load_config().get("roots") or [])
        except Exception:
            root = None
        if root is None or not _inside(p, Path(root) / "tree"):
            return False
        return p.suffix == ".md" and p.parent.name == "tree"
    if region == "facts":
        if not _inside(p, base):
            return False
        parts = [x.lower() for x in p.parts]
        return (p.name == "MEMORY.md"
                and any(x.startswith("agent-memory") for x in parts))
    return False


def revert(region: str, src: str, backup_path: str, why: str = "",
           repo_root=None) -> dict:
    """把 `backup_path` 盖回 `src`。盖之前先给当前这份再留一份备份。

    ★ 参数名不叫 `backup`（2026-10-26 实测踩到）：那个名字是模块里的**函数**，
      同名参数一进来就把函数遮住，`backup(...)` 直接 `TypeError: 'str' object is not callable`。
      这是"同名不同义"的第 N 次 —— 名字得像它是什么（`backup_path` 是个路径）。
    """
    region = str(region or "").strip().lower()
    if region not in REGIONS:
        return {"ok": False, "error": "没有这个区：%s" % region}
    if not target_ok(region, src, repo_root):
        return {"ok": False,
                "error": "这个路径不是 %s 区允许改的文件（%s）—— 回退只认记忆文件"
                         % (REGION_CN.get(region, region), src)}
    b = Path(str(backup_path or ""))
    if not b.exists() or not _inside(b, _bak_dir(region, repo_root)):
        return {"ok": False, "error": "找不到这份备份（它应该在 mem-backups/ 里）"}
    tgt = Path(str(src))
    keep = backup(region, tgt, repo_root)          # 回退本身也要能回退
    if not keep and tgt.exists():
        return {"ok": False, "error": "没法先给当前这份留底，所以没回退"}
    try:
        tgt.write_bytes(b.read_bytes())
    except OSError as e:
        return {"ok": False, "error": "写不回去：%s" % e}
    log(region, "revert", tgt.name, why or "把这条改回去了",
        repo_root=repo_root, src=str(tgt), bak=keep or str(b))
    return {"ok": True, "file": str(tgt), "restored_from": str(b),
            "saved_current": keep,
            "note": "已回退。当前那份也留了底（%s），后悔还能再退回来" % (keep or "无")}
