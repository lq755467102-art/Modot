#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agents.py —— 子 agent 的「类型」：内置三种 + 可写的定义文件。

★ 现在还**没有任何用户定义**（2026-10-26 盘点，`.harness/agents/` 里 0 个文件）
    也就是说：这 400 多行现在只服务于内置那三种，加「自定义 agent」这条路
    **建好了但没人走过**。这不是错 —— 但下一个人看到这个文件时别以为
    「已经有别人在定义了」。真要验它，最省事的是写一个最小的定义文件跑一次派活。
    判断「该不该留」的时机：等真有第二个使用场景；在那之前它属于**提前量**。

它解决什么
    在 2026-10-26 之前，子 agent 只有一种：一份写死的 `SUB_SYS` + 一个写死的工具表。
    可现实里"派活"至少有三种意图 —— 去**读**（查清楚在哪、有没有）、
    去**想**（给方案不落地）、去**验**（跑一遍看红绿）。
    三种意图需要的工具本来就不一样：纯想的类型不该有跑命令的能力，
    而"只能看不能跑"这种约束，**用提示词写是没用的**，得从工具表里拿掉。

★ 格式不自创
    和技能包、和 Claude Code / Codex 一样：`---` 之间 `key: value`，下面是指令正文。
    Claude 是 `.claude/agents/*.md`、Codex 是 `.codex/agents/*.toml`，我们是
    `<仓库>/.harness/agents/*.md`。**抄的是结构，不是抄它的文案。**

★ 两个位置，项目级优先
    项目级 `<仓库>/.harness/agents/`  只在这个项目生效（针对这个项目改过的，该盖住通用的）
    用户级 `~/.harness/agents/`       跨项目都用（可用 HARNESS_AGENTS 覆盖，测试用）

★ 一个刻意的拒绝
    定义里写了子 agent 不该有的工具（`write_file` / `edit_file` / `run_script` / `task`）
    ⇒ **加载失败**，而不是悄悄剔掉。
    悄悄剔掉的后果是：文件里明明白白写着 `tools: read_file, write_file`，
    跑起来却写不了 —— 人会先怀疑"是不是权限没配"，而真正的原因被藏起来了。
    写不了就是写不了，要在**加载**的时候说清楚。
"""
# 主要引用方包括 `tools.py` 和 `subagent.py`。
from __future__ import annotations

import os
import re
from pathlib import Path

from .skills import _norm_meta          # ★ 同一套 frontmatter 解析，不写第二份
from . import memperms as MP             # 记忆权限表（三区 × 四档）

# 子 agent **能**用的工具全集。定义文件只能从这里挑，挑不出新的。
# ★ 分两组，是因为"能不能写"不是偏好，是**要不要开隔离副本**：
#   只读类型改的都是自己的上下文；写类型必须换个 root，否则它改的就是人的工作区。
ALLOWED_RO = ("read_file", "list_dir", "code_search", "code_outline",
              "run_check", "recent_runs", "list_skills", "load_skill")
ALLOWED_RW = ALLOWED_RO + ("write_file", "edit_file", "run_script")
# ★ 提案权（2026-10-26）：子 agent 可以点名要（类型定义里写 `tools: ..., propose_rule`），
#   但**内置类型默认一个都不给** —— 一个探查型的子 agent 最该做的是把结论带回来，
#   而不是攒一堆待办给人。它也不在 ALLOWED_RO / ALLOWED_RW 里，所以不加 `write:` 就拿到。
ALLOWED = ALLOWED_RO + ("write_file", "edit_file", "run_script", "propose_rule")

# 不要求 `write: true` 的工具（它们不碰工作区）
NO_WRITE_NEEDED = ("propose_rule",)

# ★ 不含 `task`：递归的成本是乘法（一次 task 里套三次 = 四倍模型调用）。
#   Codex 用 `max_depth=1` 允许套一层，我们直接不给 —— 等并行跑稳了再谈。

# 所有类型共有的规矩（自定义 agent 的作者不用重复写这几条）
COMMON = """你是**子任务**的执行者。父 agent 把一件具体的事交给你，你只做这一件，
然后把结论交回去。

规则：

1. 你**不能改任何文件**。这不是建议，是权限：写操作在你这儿会被拒。
   需要看的东西用 read_file 读；需要验证用 run_check 跑。
2. 不要问问题。缺什么就用工具查；查不到的，在结论里明说"这一点没查到"，
   不要猜一个看起来合理的答案 —— 那个答案会被父 agent 当成事实用。
3. 结论要短（**500 字以内**），只写两样东西：
   - 结论本身
   - 证据（哪个文件、哪一行 / 哪条命令的哪一行输出）
   **不要复述过程。** 父 agent 看不到你的过程，也不关心你翻了几个文件。
4. 最后一段就是你交给父 agent 的东西。不要写"以上就是我的结论"这种收尾。
5. 交出去之前自问一句：**这个结论有证据吗**？没证据的，写成"没查到"或
   "这一点要跑 X 才能确认"，不要写成结论 —— 你会被当成事实引用。
"""

BUILTIN = (
    {
        "name": "general",
        "desc": "通用的只读子任务：查清楚一件事，把结论带回来",
        # ★ 显式给只读集，不用 `list(ALLOWED)`：ALLOWED 一旦加上写工具，
        #   这个"通用"类型就会**悄悄变成能写的**（而且它没有 write: true，没有沙箱）。
        #   那种事不会报错，只是某个只该看的子 agent 突然能改文件了。
        "tools": list(ALLOWED_RO), "spawn": [], "memory": "",
        "mem": dict(MP.DEFAULT_SUB),
        "body": "没有特别的专长。按上面的规则把这件事查清楚。",
    },
    {
        "name": "explorer",
        "desc": "在代码库里找东西：某个函数被谁调用、某个配置从哪读、某个功能在哪个文件",
        "tools": ["read_file", "list_dir", "code_search", "code_outline",
                  "recent_runs", "list_skills", "load_skill"],
        "spawn": [], "memory": "", "mem": dict(MP.DEFAULT_SUB),
        "body": """你的任务几乎总是这两种之一：**"X 在哪"** 和 **"X 有没有被用到"**。

做法：
- 先用 list_dir 摸清目录，再 read_file 读具体文件。不要一上来就读大文件。
- 说"有/没有"的时候，**必须给路径和行号**。说"没有"比说"有"更需要证据 ——
  你说没有，父 agent 就不会再去找，它信你。
- 你**不能跑命令**（这是权限，不是懒）。所以"跑一下才知道"的问题，
  在结论里明说"这个要靠跑命令验证，我这边跑不了"。""",
    },
    {
        "name": "plan",
        "desc": "只出方案不动手：给出分步骤的做法、每步怎么验、哪些地方有风险",
        "tools": ["read_file", "list_dir", "recent_runs"],
        "spawn": [],
        "body": """你的产出是**一份能照着做的方案**，不是做完的结果。

格式：
1. 先一句话说清"这件事的关键难点是什么"（没有难点就直说没有）。
2. 然后编号列步骤。每一步说清楚：改哪个文件 / 加什么 / **怎么验证这一步对了**。
3. 最后列"这样做的风险"：哪里可能把别的东西弄坏，怎么回退。

你**只能读**（连命令都不能跑），所以别写"先跑一下看看"这种话 ——
如果你不确定某件事，就把它写成"需要先确认：X"。
不要写"首先、其次、最后"这种空话，步骤要能直接执行。""",
    },
    {
        "name": "verifier",
        "desc": "跑一遍验证并判红绿：哪几组红了、红在哪一行、是代码问题还是环境问题",
        "tools": ["run_check", "read_file", "recent_runs"],
        "spawn": [],
        "body": """你的任务通常是"跑 X，告诉我红没红、红在哪"。

做法：
- 跑之前先想清楚**这条命令能不能跑**（会不会很慢、会不会改东西）。
  只跑只读命令；要改东西的命令你不该跑，也没权限跑。
- 输出很长时，**不要整段抄回来**。只抄失败的那几行 + 退出码。
- 判断"红的原因"要分三类，并且明说是哪一类：
  ① 代码真的错了（给出文件行号）
  ② 环境/依赖不对（缺包、路径不对、没启动）
  ③ 判据本身有问题（断言写得不对，或它在测一件跟这次改动无关的事）
  第三类最容易被当成第一类，而修法完全相反 —— 所以不要跳过这个分类。""",
    },
    {
        "name": "worker",
        "desc": "在隔离副本里改代码：修一个 bug、加一小段功能（改完只交 diff，不自动合）",
        "tools": ["read_file", "list_dir", "run_check", "write_file",
                  "edit_file", "run_script", "recent_runs"],
        "spawn": [],
        "write": True,
        "body": """你要**动手改**，但不只是改完就算 —— 改完要被审。

做法：
- 先读再改。改哪一行之前，那一段必须先读过（这是硬规则，不是建议：
  没读过就改会被拒，而且拒的理由是"你还没读"，不是"你没权限"）。
- **改动要小。** 不要顺手重构别的文件、不要统一缩进、不要改注释措辞。
  每一处改动都要能用一句话解释"为什么这里必须改"。
- 改完自己验一遍（能跑就跑，跑不了就在结论里说"这个我没验"）。
- 结论里写清：改了哪几个文件、每个文件改了什么、还有哪些没解决。
  父 agent 会拿着你的 diff 让人审 —— 你说不清的地方，人就得自己猜。""",
    },
)


class AgentError(ValueError):
    """定义文件有问题 / 要的类型不存在。"""


def roots(repo_root: Path) -> list[tuple[str, Path]]:
    """[(层级, 目录)]，**项目级在前**（先找到的优先）。"""
    user = Path(os.environ.get("HARNESS_AGENTS")
                or (Path.home() / ".harness" / "agents"))
    return [("项目", Path(repo_root) / ".harness" / "agents"), ("用户", user)]


def _split_tools(raw: str, where: str, can_write: bool = False) -> list[str]:
    """`read_file, run_check` → ['read_file', 'run_check']，并逐个校验。"""
    out = []
    for t in re.split(r"[,\s]+", str(raw or "")):
        t = t.strip()
        if not t:
            continue
        if t not in ALLOWED:
            raise AgentError(
                "%s 想给子 agent 用 `%s`，但它只能用这几样：%s"
                "（派活那一层不给 `task` —— 递归的成本是乘法）"
                % (where, t, "、".join(ALLOWED)))
        # ★ `propose_rule` 不要求 `write: true`（2026-10-26）：它写的是**提案队列**，
        #   不是工作区、也不是树。要它就得放开写权限的话，等于"留个便签要先开沙箱"——
        #   而权限只降不升，那一步一旦开了就收不回。
        if t not in ALLOWED_RO and t not in NO_WRITE_NEEDED and not can_write:
            raise AgentError(
                "%s 想要 `%s`，但没写 `write: true`。"
                "能写的子 agent 会在一份**隔离副本**里改（不碰你的工作区），"
                "改完把 diff 交出来等人审 —— 想让它写，就把 write 也写上"
                % (where, t))
        out.append(t)
    return out


def _split_spawn(raw, where: str) -> list[str]:
    """`spawn: explorer, verifier` → ['explorer', 'verifier']。

    ★ 这里**不校验类型是否存在**：自定义类型可以互相引用，而 scan 的时候
      另一份文件可能还没读到（顺序无保证）。存在性在派活那一刻校验
      （`subagent.run` 里 ship 之前），报错时会把"有哪些类型"一起列出来。
    """
    out = []
    for t in re.split(r"[,\s]+", str(raw or "")):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out


def _split_skills(raw: str, where: str, repo_root: Path) -> list[str]:
    """`skills: grill, review` → 名字列表，**当场校验存在**。

    ★ 为什么校验存在（2026-10-26）：不校验的话，写错一个字母的后果是
      "这个类型开场什么方法也没带"，而它跑起来一切正常 —— 只是变笨了。
      那种失败没有任何提示，人只会觉得"这个类型的活干得不如上次"。
      ⇒ 和 `tools:` 一个规矩：**配错了要当场报，别静默降级**。
    """
    names = [t.strip() for t in re.split(r"[,\s]+", str(raw or "")) if t.strip()]
    if not names:
        return []
    try:
        from . import skills as SK
        have = {x["name"] for x in SK.scan(Path(repo_root))}
    except Exception:
        return names          # 技能目录读不出来不该让定义加载失败（会连带整张表挂掉）
    bad = [n for n in names if n not in have]
    if bad:
        raise AgentError("%s 的 skills 里有不存在的技能：%s（现在有的是：%s）"
                         % (where, "、".join(bad),
                            "、".join(sorted(have)[:12]) or "一个都没有"))
    return names


def scan(repo_root: Path) -> list[dict]:
    """所有可用的子 agent 类型。同名以项目级 > 用户级 > 内置。

    返回 [{name, desc, tools, model, body, level}]
    """
    seen: dict[str, dict] = {}
    for a in BUILTIN:
        seen[a["name"]] = {"name": a["name"], "desc": a["desc"],
                           "tools": list(a["tools"]), "model": "",
                           "write": bool(a.get("write")),
                           "spawn": list(a.get("spawn") or []),
                           "memory": str(a.get("memory") or ""),
                           "mem": dict(a.get("mem") or MP.DEFAULT_SUB),
                           "skills": list(a.get("skills") or []),
                           "remember": bool(a.get("remember")),
                           "body": a["body"], "level": "内置"}
    # ★ 顺序：内置 → 用户 → 项目。后写的覆盖前面的，所以项目级天然最优先。
    for level, root in reversed(roots(repo_root)):
        if not root.is_dir():
            continue
        for f in sorted(root.glob("*.md")):
            if f.name.startswith("_"):
                continue                     # 下划线开头＝草稿，不列出来
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _norm_meta(text)
            name = str(meta.get("name") or f.stem).strip()
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
                continue                     # 名字不合法就当没这个文件，不报错打断整张表
            wr = str(meta.get("write") or "").strip().lower() in ("1", "true", "yes", "on")
            # ★ `spawn:` —— 这个类型**能派哪几个类型**（2026-10-26，照 Claude Code 的
            #   `tools: ..., Task(monitor, rollback)` 做的）。
            #   为什么用白名单不用数字：数字只能说"能派几层"，说不出"能派谁"；
            #   而"能派谁"才是真正要管的事 —— 一个只会查代码的类型，
            #   不该有权限去派一个能改文件的类型。
            sp = _split_spawn(meta.get("spawn"), f.name)
            # ★ `memory:` —— 事实区的作用域（2026-10-26，照 Claude 的三档）。
            #   不写就是没有记忆：不做全局开关，**只在定义里显式开**。
            memscope = str(meta.get("memory") or "").strip().lower()
            if memscope and memscope not in ("user", "project", "local"):
                raise AgentError(
                    "%s 的 memory 写的是 `%s`，只能填 user / project / local"
                    "（分别落在用户目录、本仓库 .harness/、本仓库 .harness/ 且不进版本控制）"
                    % (f.name, memscope))
            # ★ `memory_tiers:` —— 三个记忆区各自的档位（2026-10-26）。
            #   写错任何一项都在**加载时**报，不按默认跑：拼错一个字母
            #   而静默回退的结果是"它一直没提案权，而人以为给了"。
            try:
                mtiers = MP.resolve(meta, where=" %s " % f.name)
            except MP.MemError as e:
                raise AgentError(str(e))
            # ★ 记忆工具**不许写进 `tools:`**（2026-10-26）：授权只有一个真源。
            #   两处都能授权的话，就会出现"tools 里写了 propose_rule、
            #   但 memory_tiers 是 read"这种自相矛盾的定义，而它不会报错 ——
            #   只会让人反复怀疑"是不是权限没生效"。
            for _t in _split_tools(meta.get("tools"), f.name, can_write=wr):
                if _t in MP.MEM_TOOLS:
                    raise AgentError(
                        "%s 的 tools 里点了 `%s`，但记忆工具**不由 tools 授权** ——"
                        " 改用它自己的那一行：`memory_tiers: rules=propose`"
                        "（可选区：%s；可选档：%s）。两处授权一定会打架。"
                        % (f.name, _t, "、".join(MP.REGIONS), "、".join(MP.TIERS)))
            try:
                tools = _split_tools(meta.get("tools"), f.name, can_write=wr)
            except AgentError:
                raise                        # ★ 工具写错**必须**报出来，不能吞
            # ★ `skills:` —— 开场就带着的方法（2026-10-26）。
            #   和 `tools: load_skill` 的区别：那个要它先想到去取，
            #   而这个**开局就在上下文里**。区别在"知道有这个能力"和"已经有这个做法"。
            skl = _split_skills(meta.get("skills"), f.name, repo_root)
            # ★ `remember: on` —— 允许它往**自己那本**事实里记东西（2026-10-26）。
            #   为什么锁成"自己那本"：子 agent 之间不共享判断，让它写公共事实
            #   等于让一个只看到局部的角色改全项目的常识。
            rmb = str(meta.get("remember") or "").strip().lower() in ("1", "true", "yes", "on")
            if rmb and not memscope:
                raise AgentError(
                    "%s 写了 `remember: on`，但没写 `memory:` ——"
                    " 没有作用域就没有地方可写（memory 填 user / project / local）" % f.name)
            if rmb:
                # 把事实区抬到 edit；其余区不动（它不该顺手改规则）
                mtiers["facts"] = "edit"
            seen[name] = {
                "name": name,
                "desc": str(meta.get("description") or "").strip(),
                "tools": tools or list(BUILTIN[0]["tools"]),
                "model": str(meta.get("model") or "").strip(),
                "write": wr,
                "spawn": sp,
                "memory": memscope,
                "mem": mtiers,
                "skills": skl,
                "remember": rmb,
                "body": body.strip(),
                "level": level,
                "from": str(f),
            }
    return list(seen.values())


def load(name: str, repo_root: Path) -> dict:
    """按名字取一个类型。取不到就把**有的是哪些**列出来（只说不存在，人没法选）。"""
    want = str(name or "").strip() or "general"
    table = scan(repo_root)
    for a in table:
        if a["name"] == want:
            return a
    raise AgentError("没有叫 `%s` 的子 agent。现在是这几种：%s"
                     % (want, "、".join(a["name"] for a in table)))


def system_prompt(spec: dict, repo_root=None) -> str:
    """类型、隔离边界、技能及有明确作用域的事实；不加载旧预设准则。"""
    head = "你这一趟的类型是「%s」：%s\n\n" % (
        spec["name"], spec.get("desc") or "没有额外说明")
    body = (spec.get("body") or "").strip()
    risk = ""
    if spec.get("write"):
        risk = """
★ 你在一间**隔离副本**里，不是主工作区。你改的东西不会立刻影响任何人 ——
  改完只把 diff 交出去，由人决定要不要合。所以：
- 路径一律用**相对路径**（相对工作区根）。绝对路径会指到副本外面去，被拒。
- 改动要小、要能一句话解释"为什么这里必须改"；不要顺手重构别的文件。
- 你做不到的事（要装包、要动仓库外面的东西、要重启服务）就明说，别绕。
"""
    mem = ""
    if spec.get("memory"):
        try:
            from . import facts as FACTS
            mem = FACTS.block(spec["memory"], spec["name"], repo_root)
        except Exception as _e:
            # 读不出来要说一句：静默的话，"没有记忆"和"读失败"长得一样。
            mem = "## 你的记忆\n（读不出来：%s）" % _e
    perm = _perm_block(spec)
    skl = _skills_block(spec, repo_root)
    return (COMMON + "\n" + head + (body + "\n" if body else "") + risk
            + (("\n" + perm + "\n") if perm else "")
            + (("\n" + skl + "\n") if skl else "")
            + (("\n" + mem + "\n") if mem else ""))


# 预加载的正文上限。★ 为什么要上限：技能正文可以很长，全塞进系统提示会把
# 真正重要的东西（规矩、边界）挤到看不见 —— 而上下文是有限资源，
# 挤掉了不会报错，只会让它在关键处"忘了"。
_SKILL_MAX_EACH = 2400
_SKILL_MAX_ALL = 6000


def _skills_block(spec: dict, repo_root=None) -> str:
    """把定义里 `skills:` 点名的技能**正文**拼进来。

    ★ 为什么不是只给名字：只给名字的话，模型得先"想起去 load_skill"——
      而它不知道那个技能里讲的是什么，就不会想起。**知道有这个能力 ≠ 已经有这个做法。**
    ★ 为什么保留"删减"标记：正文被截断时必须说出来，否则它会以为
      "这个方法就这么点内容"，然后照着半截规则干活。
    """
    names = spec.get("skills") or []
    if not names:
        return ""
    try:
        from . import skills as SK
    except Exception:
        return ""
    parts = []
    total = 0
    for n in names:
        try:
            r = SK.load(n, Path(repo_root or "."))
        except Exception as e:
            parts.append("### %s（读不出来：%s）" % (n, e))
            continue
        if not r.get("ok"):
            parts.append("### %s（读不出来：%s）" % (n, r.get("error")))
            continue
        body = (r.get("body") or "").strip()
        cut = len(body) > _SKILL_MAX_EACH
        body = body[:_SKILL_MAX_EACH]
        if total + len(body) > _SKILL_MAX_ALL:
            parts.append("### %s（放不下了：已经带了 %d 字，剩下的请自己 load_skill）"
                         % (n, total))
            continue
        total += len(body)
        parts.append("### %s\n%s%s" % (n, body, "\n…（后面还长，要看完整版自己 load_skill）"
                                        if cut else ""))
    head = ("## 你开场就带着的做法（不是命令，是方法）\n"
            "下面这些是定义里点名带的技能正文 —— 不用再去 load_skill 取。\n"
            "★ 它们是**方法**：照着做，但具体情况还得自己判断。\n")
    return head + "\n".join(parts)


def _perm_block(spec: dict) -> str:
    """把你对**记忆**能做几档事说清楚。

    ★ 为什么要写（2026-10-26）：档位已经把工具拿掉了，所以模型**碰不到**越权动作。
      但"碰不到"会让它以为"这个功能不存在"，于是它不去说"这条我想记下来但没权限"，
      而是**默默丢掉** —— 那才是真正的损失。说清楚"你能做 A/B，不能做 C，不能做的写进结论"。
    """
    mem = spec.get("mem") or {}
    if not any(v and v != "none" for v in mem.values()):
        return ""
    lines = ["## 你能对记忆做什么（权限表，不是建议）"]
    lines.append("现在：" + (MP.describe(mem) or "全部不看"))
    if spec.get("remember") and spec.get("memory"):
        # ★ 说出来（2026-10-26）：不说的话它不知道自己能记，
        #   于是每次都把"这个项目该怎么做"重新推一遍 —— 那正是这条要消灭的浪费。
        lines.append("另外你可以用 `remember` 往**你自己那本**事实里记东西"
                     "（kind 固定为 `%s`，改不了；作用域 `%s`）。"
                     "下次同类型开工时它会被带回来。" % (spec.get("name"), spec["memory"]))
    can = [n for (r, t), n in MP.TOOL_AT.items() if MP.at_least(mem, r, t)]
    if can:
        lines.append("你手上因此有这些工具：" + "、".join("`%s`" % c for c in can))
    lines.append(
        "★ 没到档的事**工具不在你手里** —— 不是你被要求「别做」，是做不了。"
        "遇到这种：把「想记什么、为什么要记」写进结论，让人来定，"
        "**不要默默丢掉** —— 丢掉之后连「有过这个念头」都没人知道。")
    lines.append(
        "★ 分不清习惯和规则时的判据：**换一个仓库还成立吗？** 成立是习惯（跨项目），"
        "只有这个项目才成立是规则。")
    return "\n".join(lines)


def provider_for(spec: dict, provider):
    """这个类型指定了模型就换一个 —— 同 profile、同 key，只换 model。

    ★ 造不出来就用父的：**"想省钱"不能变成"跑不了"**。
      这和降级链里"默认允许切"是同一条取舍：多试一次最坏是多花一点，
      不试就是整件事没做成。
    """
    m = str(spec.get("model") or "").strip()
    if not m or provider is None:
        return provider
    if str(getattr(provider, "model", "") or "") == m:
        return provider
    prof = getattr(provider, "profile", "")
    if not prof:
        return provider                      # 降级链/假 provider 没有 profile，换不了
    try:
        from .provider import FallbackProvider, OpenAICompatProvider
        cheap = OpenAICompatProvider(profile=prof, model=m)
        # ★ 指定模型不能变成**单点故障**（2026-10-26）：这个便宜模型挂了，
        #   而父那边明明配了备用链。所以把父链里其它模型接在它后面 ——
        #   "想省钱"不能变成"更容易失败"。
        chain = getattr(provider, "chain", None)
        if chain:
            rest = [x for x in chain if str(getattr(x, "model", "")) != m]
            if rest:
                return FallbackProvider([cheap] + rest, quiet=True)
        return cheap
    except Exception:
        return provider
