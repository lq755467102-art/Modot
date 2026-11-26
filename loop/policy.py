#!/usr/bin/env python3
"""policy.py —— 动手前的门禁（不是沙箱）

★ 这一个文件可以单独拷走
    零依赖：只 import pathlib 和 __future__，不认这个仓库里的任何东西。
    接进来只需要三步：
      ① 把本文件放进你的项目；
      ② 开任务时建一个 `Policy(mode="workspace", root=<工作目录>)`；
      ③ 每次动手前 `ok, why = pol.check(工具名, 参数)`，ok 为假就**不要执行**，
         why 是要展示给人的拒绝理由（它写的是人话，不是错误码）。
    想确认拷过去之后行为还对：`python policy.py --self-test`（不用任何依赖，
    跑一张 4 档 × 8 动作的表，逐条核对预期，全对不对就退出码 1）。

为什么要有这个
    `Tool(zone, readonly)` 只是**分类**：它回答「这个动作危险吗」，
    不回答「这一次该不该做」。分类在注册时定死，而"该不该"取决于这一轮的
    任务和工作目录。没有门禁的话，模型让它删什么它就删什么。

★ 它是门禁，不是沙箱 —— 这个区别必须写在界面和发布说明里
    门禁 = 拦住它别去做；沙箱 = 它做了也做不到。
    `run_check` 能跑任意命令，绕过门禁的办法很多
    （`python -c "import os; os.remove(...)"` 就绕过去了）。
    这里能做的只有两件事：
      ① 路径类工具按工作目录判，越界就拒；
      ② 命令类工具用一张保守词表挡掉最危险的几种。
    剩下的靠人看事件。**别把它当安全边界用。**

四档权限（对齐 Claude Code 的模式，去掉需要分类器的 auto）
    readonly    只读工作目录内的东西
    verify      读 + 跑检查命令；**不写**（给"复核者"用：能核实，不能改）
    workspace   工作目录内读+写；出界拒（**默认**）
    trusted     什么都放（命令行和验收用；要有理由才用）

    ★ 默认必须是 fail-closed 的那一档
      门禁的默认值只有两种：要么拦住，要么形同虚设。
      原来默认 trusted、且**非法档位也退回 trusted** ——
      于是把 `workspace` 拼错成 `workspcae`，得到的不是报错，是**门禁全开**。
      而这种事不会有人发现：它看起来一切正常，只是没拦。
      ⇒ 现在：默认 workspace；档位不认识就当场 ValueError。

不可绕过（任何级别都不放）
    ① `.git` 里的写 —— 毁了仓库状态，而且改动在 diff 里看不见
    ② 指向用户主目录、盘符根、仓库根的删除/覆写

★★ 命令规则（2026-10-26 加，`.harness/policy.json`）
    档位只回答"这个级别的 agent 能做什么"，回答不了"**这个项目**里不许做什么"。
    比如：只读档也可以 `git status`，但这个项目可能连 `git push` 都不许碰。
    ⇒ 加一份项目级规则，三个键：

        commands.deny   一律拦（**连 trusted 也拦**：它表达的是项目约束，不是权限档）
        commands.only   非空 = 只允许这些，其余全拒（发给别人时最有用）
        commands.allow  命中就**跳过不可逆词表**（治误报：脚本名里带"删除"不该被拦）

    判定顺序照 Claude Code：**deny → only → allow → 词表**，第一个匹配决定。
    ★ 为什么 deny 优先于 allow：宽泛的拒绝是安全的，宽泛的放行不是。
      两边都写的时候，唯一能接受的默认是"拦"。

    ★ 这一份规则也是**将来接 OS 沙箱的那张表**：
      沙箱要执行"哪些目录可写、能不能联网、能跑什么"，
      而这里已经声明了"能跑什么"。先有表，再加执行点。
"""
# 主要引用方包括 `code_service.py` 和 `subtasks.py`。
from __future__ import annotations

from pathlib import Path

# ★ 加了 verify：**能核实，不能改**。
#   本我（review.py）要看账对不对，就得能自己跑验收 ——
#   只读档不让跑命令，于是它只能"没看到你跑过"，说不出"我跑了，结果不一致"。
#   verify = 读 + 跑检查命令，写操作照样拒。
MODES = ("readonly", "verify", "workspace", "trusted")

# ── 不可逆动作的保守词表 ──
# ★ 为什么用词表：它要挡的是**漏判**，不是精确识别意图。
#   误报的代价 = 多问一次；漏报的代价 = 事后收不回来。这个不对称决定了宁可误报。
#   而有申诉通道（把 policy 调成 trusted 并写下理由），所以保守是对的。
# ★ 这张表原来在 bin/contract.py 里，有两处用（契约检查和这里）。
#   挪到这一处，两边 import —— 各存一份就是第二个真源。
DANGER = (
    "rm -rf", "rm -r ", "rmtree", "unlink", "del /s", "del /q", "shutil.rmtree",
    "git push", "git reset --hard", "force-with-lease",
    "drop table", "truncate", "format ",
    "删除", "清空", "覆盖掉", "对外发", "发布", "上线", "推送", "退款", "转账", "扣款",
)

# 删除/覆写这些地方，任何级别都不放
HOME = Path.home().resolve()

# 项目级规则文件（相对工作目录）
RULES_FILE = ".harness/policy.json"


# ══════════════════════════════════════════════════════════════════
# 工具分档：**每个工具必须自己声明它是哪一类，没声明的一律拒**
#
# ★ 为什么要有这个概念（2026-10-27 深审）
#   原来 `check()` 只对 read_file / write_file / edit_file / run_* 各写一条 if，
#   其余落到最后 `return True, ""` —— **想都没想过的工具，在 workspace / trusted 下默认放行**。
#   后果不是"少拦一次"，是"以后每加一个工具都自动多一个洞"。
#
# ★ 为什么分类放在**注册处**（`Tool(kind=...)`）而不是这里的一张名单
#   第一版我在这里加了 READ/WRITE/EXEC/META 四张表 —— 跑验收立刻发现两件事：
#     ① 表和注册表分处两地 ⇒ 加工具的人只要不改这里，就是"忘了登记"；
#     ② 测试里现造的临时工具（`boom` / `slow1`）全部被误杀 ——
#        而它们本来就该像真工具一样说清"我是哪一类"。
#   ⇒ 单一真源移到 `loop/tools.py` 的注册处，这里只留**判定**和一份兜底映射，
#     供"不经过注册表"的调用（自检、外部拷贝）使用。
#
# ★ 四类语义
#   read  只读文件/信息，要过"读工作目录内"这条边界
#   write 改文件，要过 .git / 根目录 / 工作目录边界
#   exec  跑命令或脚本，要过项目级规则 + 不可逆词表
#   meta  不碰磁盘也不跑命令（问人、看技能、记忆提案…）
#         —— 它们的权限由**注册与否**决定；门禁只管"只读档不许用"
KINDS = ("read", "write", "exec", "meta", "output", "lifecycle")

# 兜底映射：只给"没有 Tool 对象"的调用方（`policy.py --self-test`、外部拷贝）。
# ★ 它**不是**主路径 —— 主路径是 `Tool.kind`，两边冲突时以 Tool 为准。
FALLBACK_KIND = {
    "read_file": "read", "read_image": "read", "list_dir": "read",
    "code_search": "read", "code_outline": "read", "code_reindex": "read",
    "recent_runs": "read", "list_skills": "read", "load_skill": "read",
    "ask_human": "meta", "add_layer": "meta", "task": "meta",
    "remember": "meta", "propose_rule": "meta", "propose_habit": "meta",
    "write_file": "write", "edit_file": "write",
    "run_check": "exec", "run_script": "exec", "run_acceptance": "exec",
}


def resolve_kind(tool: str, kind: str = "") -> str:
    """这一次调用该按哪一类判。**判不出来返回空串** —— 调用方必须当成"拒"。

    优先级：调用方显式给的（`Tool.kind`）> 兜底映射。
    """
    k = str(kind or "").strip()
    if k in KINDS:
        return k
    return FALLBACK_KIND.get(str(tool or ""), "")


def _argv_of(cmd) -> list[str]:
    """把 cmd 统一成 argv。★ 一定要按**词**比较，不能拼成字符串再比 ——
    拼起来之后 `git push` 会匹配到 `git pushy`，而那是两件事。"""
    if isinstance(cmd, (list, tuple)):
        return [str(x) for x in cmd]
    return str(cmd or "").split()


def _hit(pattern, argv: list[str]) -> bool:
    """一条规则命中吗。无通配按 **argv 前缀逐词相等**；有通配才用 fnmatch。"""
    p = str(pattern or "").strip()
    if not p or not argv:
        return False
    if any(ch in p for ch in "*?["):
        import fnmatch
        return (fnmatch.fnmatch(" ".join(argv), p)
                or fnmatch.fnmatch(argv[0], p))
    words = p.split()
    return argv[:len(words)] == words


def _load_json(p: Path) -> tuple[dict, str]:
    """返回 (内容, 错误)。★ 错误要**带出去**，不能吞掉：
    吞掉之后"文件读坏了"和"文件不存在"看起来一模一样，
    而前者要人去修文件、后者要人去建文件 —— 两种处置完全不同。"""
    import json
    if not p.is_file():
        return {}, ""
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(d, dict):
            return {}, "顶层不是对象"
        return d, ""
    except Exception as e:
        # 读坏了就当没有 —— 一个配置手滑不该让整轮任务开不了工。
        return {}, "%s: %s" % (type(e).__name__, e)


def load_rules(root=None) -> dict:
    """读两级规则：用户级 + 项目级（deny/allow 取并集，only 项目级优先）。

    ★ 为什么不合并 only：only 的语义是"只允许这些"。把两边的 only 并起来，
      等于**放宽**了允许范围 —— 而两层配置里，项目那份才是针对这个项目的。
    """
    user, e1 = _load_json(HOME / RULES_FILE)
    proj, e2 = (_load_json(Path(root) / RULES_FILE) if root else ({}, ""))
    out = {"deny": [], "allow": [], "only": [], "source": [], "errors": []}
    for pth, err in ((HOME / RULES_FILE, e1),
                     ((Path(root) / RULES_FILE) if root else None, e2)):
        if err:
            out["errors"].append("%s：%s" % (pth, err))

    def take(d, label, p):
        c = (d.get("commands") or {}) if isinstance(d, dict) else {}
        if not c:
            return
        # ★ 只要文件里**有 commands 段**就记来源，不管规则是不是空 ——
        #   否则"读到了但规则为空"和"根本没读到"显示成一样，
        #   而人配完发现没反应时会去翻文件，翻到的是对的，然后不知道问题在哪。
        out["source"].append(str(p))
        for k in ("deny", "allow"):
            for v in (c.get(k) or []):
                if isinstance(v, str) and v.strip() and v not in out[k]:
                    out[k].append(v.strip())
        if c.get("only"):
            out["only"] = [str(v).strip() for v in c["only"] if str(v).strip()]

    if user:
        take(user, "用户级", HOME / RULES_FILE)
    if proj:
        take(proj, "项目级", Path(root) / RULES_FILE)
    return out


def hits_danger(text: str) -> list[str]:
    t = text or ""
    return sorted({w for w in DANGER if w in t})


class Policy:
    """一次任务的权限边界。**不可变**：一轮之内不中途放宽 ——
    放宽要么开新一轮，要么人显式改配置。中途悄悄放宽，等于没有边界。"""

    def __init__(self, mode: str = "workspace", root=None, rules=None,
                 scope=None):
        # ★ 档位不认识就报错，不退回放行。
        #   这里原来写的是 `mode if mode in MODES else "trusted"` ——
        #   一个拼错的档位得到的是「全放」，而且**没有任何声音**。
        #   fail-closed 的意思是：我不确定该拦多少，那就当它最该被拦。
        if mode not in MODES:
            raise ValueError("不认识的权限档位 %r（可选：%s）"
                             % (mode, " / ".join(MODES)))
        self.mode = mode
        self.root = Path(root or Path.cwd()).resolve()
        # 规则默认从工作目录读（`<root>/.harness/policy.json` + 用户级那份）。
        # ★ 允许外部传进来：测试要能造出确定的规则，不去读真实配置。
        self.rules = rules if rules is not None else load_rules(self.root)
        # ★ 这一轮**允许改哪些路径**（2026-10-28）。
        #   `mode` 管的是"这一档许不许写"，`scope` 管的是"这一轮该不该写这个文件"。
        #   两件事：前者防越权，后者防"做完事自己给自己找活干"——顺手重构、
        #   顺手加功能，每件单看都合理，合起来就是偏离任务。
        #   ★ 空 = 不限（现状语义）。**这条不能反过来**：默认拒绝会让所有既有
        #     调用当场失效，而它们本来就不带范围。
        self.scope = [str(x) for x in (scope or []) if str(x).strip()]

    # ── 判据 ──

    def _abs(self, p) -> Path:
        """相对路径按**本轮的工作目录**解析，不按进程 cwd。

        ★ 2026-10-26 修：这里原来直接 `Path(p).resolve()`，等于拿进程 cwd 当基准。
          主循环里两者恰好相等，所以一直没露；但子 agent 的 root 是隔离副本，
          它说"写 a.txt"，policy 却把这句解析到主工作区，于是判定"写工作目录外"。
          现象是：**每个写操作都被拒，理由还指向一个它根本没打算动的地方。**
        """
        q = Path(p)
        if not q.is_absolute():
            q = Path(self.root) / q
        return q

    def inside(self, p) -> bool:
        try:
            self._abs(p).resolve().relative_to(self.root)
            return True
        except Exception:
            return False

    def _critical(self, p) -> str:
        """指向关键位置的写。返回理由，空字符串表示没问题。

        ★ 算不出真实路径时返回**理由**，不返回空串（2026-10-27 改）。
          原来这里是 `except: return ""` —— 空串的语义是"没问题"，于是
          "我不知道它指向哪"被当成了"它没问题"。安全判定里这个方向必须是反的：
          **判不了就不放**。
          （今天这条路被后面的 `inside()` 挡着：它同样调 `resolve()`，同样失败就拒，
            所以不是现成的洞。但两处判断方向相反，改一次顺序就是洞 —— 这种
            "靠下游兜着"的正确性，不能留。）
        """
        try:
            rp = self._abs(p).resolve()
        except (OSError, ValueError, RuntimeError) as e:
            return ("这个路径解析不出来（%s: %s），没法确认它指向哪 —— 判不了就不写。"
                    % (type(e).__name__, e))
        if any(part == ".git" for part in rp.parts):
            return "写 .git 里 —— 仓库状态会被改坏，而且 diff 里看不见"
        if rp in (self.root, HOME, Path(rp.anchor or "/")):
            return "目标是根目录本身（%s）" % rp
        return ""

    def _out_of_scope(self, p) -> str:
        """这个写操作超出这一轮的范围了吗。返回理由，空串 = 没问题。

        ★ 为什么单独一层而不是塞进 `inside()`：`inside()` 回答"在不在工作目录里"
          （边界，安全）；这里回答"在不在这一轮说好要改的地方"（任务，纪律）。
          两者的拒绝理由对人完全不是一回事：前者是"你没权限"，后者是"你跑偏了"。
        """
        if not self.scope:
            return ""
        try:
            rp = self._abs(p).resolve()
        except (OSError, ValueError, RuntimeError) as e:
            return ("这个路径解析不出来（%s: %s），没法确认它在不在范围内 —— "
                    "判不了就不写" % (type(e).__name__, e))
        for s in self.scope:
            try:
                sp = self._abs(s).resolve()
            except (OSError, ValueError, RuntimeError):
                continue        # 范围里有一条算不出来，跳过它，不影响其它条
            if rp == sp or sp in rp.parents:
                return ""
        try:
            rel = rp.relative_to(self.root)
        except (ValueError, OSError):
            rel = rp
        shown = "、".join(self.scope[:8]) + ("…" if len(self.scope) > 8 else "")
        return ("这一轮的范围是「%s」，而「%s」不在里面。\n"
                "  如果不是必须改它，就别改；确实必须，请把理由写进结论、让人来放宽范围"
                % (shown, rel))

    def _argv_for(self, tool: str, a: dict) -> list[str]:
        """命令类工具 → argv（用来对规则）。其它工具返回空。"""
        if tool == "run_script":
            sc = str(a.get("script") or "").strip()
            return ([sc] + [str(x) for x in (a.get("args") or [])]) if sc else []
        if tool in ("run_check", "run_acceptance"):
            return _argv_of(a.get("cmd"))
        return []

    def check(self, tool: str, args, kind: str = "") -> tuple[bool, str]:
        """(放行吗, 拒绝理由)。理由要能让人看懂在拒绝什么。

        `kind` 是注册时由工具自己声明的档别（`Tool(kind=...)`）。
        不传就查兜底映射 —— **查不到 = 拒**，不存在"默认放行"这个分支。
        """
        a = args if isinstance(args, dict) else {}
        p = a.get("path")
        if self.rules.get("errors"):
            return False, "权限规则无法完整读取；请修复配置后重试，未按空规则执行"

        # ── 项目级禁令：**trusted 也拦** ──
        # ★ 它不是"权限档"，是"这个项目不许做的事"。trusted 的语义是
        #   "这一轮可以放开手脚"，不是"可以违反项目约定"。两者不是一回事。
        argv = self._argv_for(tool, a)
        if argv:
            for pat in (self.rules.get("deny") or []):
                if _hit(pat, argv):
                    src = self.rules.get("source") or [RULES_FILE]
                    return False, ("这个项目禁了「%s」（%s 的 commands.deny）。"
                                   "它不属于权限档，trusted 也不放 —— 要改请改那个文件"
                                   % (pat, Path(src[0]).name))

        # ── only：非空 = 只允许这些，**trusted 也生效** ──
        # ★ 它和 deny 是同一性质（项目约束，不是权限档）。trusted 的意思是
        #   "这一轮放开手脚"，不是"可以跑项目里没批准的命令"。
        #   发给别人时装上 only，是最直接的一层保险。
        if argv:
            _only0 = self.rules.get("only") or []
            if _only0 and not any(_hit(x, argv) for x in _only0):
                return False, ("这个项目只允许跑这几类命令（commands.only）：%s。"
                               "「%s」不在里面" % ("、".join(_only0[:6]), " ".join(argv[:3])))

        # ── 底线清单：**任何档都不放**（2026-10-26）──
        # ★ 位置就是它的全部意义：放在 `trusted` 之前。
        #   用户最后都会选 trusted，这不该被对抗 —— 该被承认，然后给它一条底线。
        #   实测过：加这一条之前，`trusted` 档的 `read_file` 能读 `~/.ssh/id_ed25519`、
        #   能写 `~/.ssh/authorized_keys`（命令层有禁读名单，文件工具层没有）。
        try:
            from . import baseline as BL
            _act = {"read_file": "read", "write_file": "write",
                    "edit_file": "write"}.get(tool, "")
            if p and _act:
                _h = BL.hit(_act, p, self.root)
                if _h:
                    # ★ 记一笔（2026-10-26）：拒了就走的话，"被挡了多少次、都是哪一类"
                    #   只有当场那个人知道 —— 于是"任务一直在做危险的事"和
                    #   "规则误伤了正常动作"这两种情况都看不见。计数不是判据，只是提示。
                    # Audit availability must never change the deny decision.
                    try:
                        BL.record(_h, src="policy:" + str(self.mode))
                    except Exception:
                        return False, "命中安全底线；审计记录失败，动作仍被拒绝"
                    return False, BL.text(_h)
        except Exception:
            # ★ 基线自己出错时**不能**因此放行 —— 落到下面各档的判断，
            #   它们至少还有"工作目录边界"。这里不静默 true。
            return False, "安全底线检查失败，未执行；请检查安全配置"

        # ── 工具分档：**判不出它是什么，任何档都拒** ──
        # ★ 位置在 trusted 之前，理由和底线清单一样：它回答的是
        #   "这套系统知不知道这个动作能干什么"，而 trusted 回答的是
        #   "这一轮允许放到多松"。不知道它是什么，就谈不上放行。
        # ★ 实测过的旧行为（2026-10-27）：`future_write_tool` 在 workspace 下
        #   返回 `(True, "")` —— 一个凭空造的工具名直接过门。
        kind = resolve_kind(tool, kind)
        if not kind:
            return False, ("「%s」没说清自己是哪一类工具（读 / 写 / 跑 / 元），"
                           "所以不放。修法是在注册它的时候给 `Tool(kind=...)` "
                           "指定一类" % tool)

        if self.mode == "trusted":
            return True, ""

        if kind == "read":
            if p and not self.inside(p):
                return False, "读工作目录外的文件（%s）—— 这一档只允许读工作目录内" % p
            return True, ""

        if kind in {"output", "lifecycle"}:
            # Registered output tools accept content/resource IDs and confine
            # writes to app-owned immutable outputs, including in readonly mode.
            if p:return False,"成果工具使用内容或资源 ID，不接受文件路径"
            return True,""

        if kind == "write":
            if self.mode in ("readonly", "verify"):
                return False, ("这一档只能看和核，不许写文件"
                               if self.mode == "verify" else "这一档是只读，不许写文件")
            if not p:
                # ★ 没有路径参数的写类工具（按批处理的写工具、验收里那些模拟写类的假工具）：
                #   门禁到这里只能判"这一档许不许写"，判不了边界 —— 边界得由工具自己守。
                #   这不是漏洞的入口：真正要防的是"新工具被错判成 read 混过去"，
                #   而那种错判现在会让它连门都进不来（kind 由注册处声明，写错要当场改）。
                return True, ""
            bad = self._critical(p)
            if bad:
                return False, bad
            if not self.inside(p):
                return False, ("写工作目录外的东西（%s）—— 这一档只允许改工作目录内。"
                               "确实要改请换 policy" % p)
            # ★ 边界过了才谈纪律（2026-10-28）：在工作目录内，但**不在这一轮
            #   说好要改的地方**，同样是跑偏。层次不能混 —— 先安全后纪律。
            _oob = self._out_of_scope(p)
            if _oob:
                return False, _oob
            return True, ""

        if kind == "exec":
            if self.mode == "readonly":
                return False, "这一档是只读，不许跑命令"
            # ★ 原来这里只有 run_check / run_acceptance —— `run_script` 落到最后的
            #   "认不出的工具"分支，在 workspace 下**默认放行**，连词表都不查。
            #   它能跑仓库内任意脚本，等于开了一条绕过词表的路（2026-10-26 补上）。
            argv2 = self._argv_for(tool, a)
            # ★ only 在上面**统一判过了**（要在 trusted 之前生效），这里不再判 ——
            #   同一件事判两次，改一处忘一处就是两个结论。
            # allow = **豁免词表**（不是"放宽路径权限"）。
            # 治的是误报：脚本名里带"删除"、命令里带"发布"这种，人确认过就写进 allow。
            if argv2 and any(_hit(x, argv2) for x in (self.rules.get("allow") or [])):
                return True, ""
            # verify：允许跑（本我要能核实），但危险词表照拦 —— 它读的轨迹
            # 是模型写的，可能被诱导；"能核"不等于"能改世界"。
            blob = " ".join(argv2) if argv2 else ""
            hit = hits_danger(blob)
            if hit:
                return False, ("命令里有不可逆动作（%s）。这一档不放 —— "
                               "确认它安全就写进 .harness/policy.json 的 commands.allow"
                               % "、".join(hit))
            return True, ""

        # 剩下的只有 meta（不碰磁盘、不跑命令）。
        # ★ 只读档照旧拒：这一档的定位是"什么都不发生"，而 meta 里就有
        #   `propose_rule` / `remember` 这种会落盘的。要放宽得逐个论证，
        #   不能靠一张名单一起放开。
        if self.mode in ("readonly", "verify"):
            return False, "这一档只能看和核，不许用 %s" % tool
        return True, ""

    def label(self) -> str:
        return {"readonly": "只读", "verify": "能核实不能改",
                "workspace": "工作目录内",
                # ★ 不写「全放」：trusted 只是日常不再请求授权，凭据和不可逆动作仍有底线。
                "trusted": "信任·底线仍在"}.get(self.mode, self.mode)

# ── 自检 ──
# ★ 为什么放一个 CLI 进来：这个文件对外宣称「可以单独拷走」，
#   而「拷过去之后还对不对」不该靠读代码确认。
#   拷进新项目后 `python policy.py --self-test`：跑一张 档位 × 动作 的表，
#   逐条核对预期，全对不对就退出码 1。不依赖被测项目里的任何东西。
def _self_test() -> int:
    root = Path.cwd()
    inside = str(root / "a.txt")
    outside = str(root.parent / "somewhere" / "b.txt")
    gitfile = str(root / ".git" / "config")
    danger = {"cmd": ["rm", "-rf", str(root)]}
    safe = {"cmd": ["python", "-c", "print(1)"]}

    # (动作名, 工具, 参数, 各档预期)
    CASES = [
        ("读·目录内", "read_file", {"path": inside},
         {"readonly": True, "verify": True, "workspace": True, "trusted": True}),
        ("读·目录外", "read_file", {"path": outside},
         {"readonly": False, "verify": False, "workspace": False, "trusted": True}),
        ("写·目录内", "write_file", {"path": inside},
         {"readonly": False, "verify": False, "workspace": True, "trusted": True}),
        ("写·目录外", "write_file", {"path": outside},
         {"readonly": False, "verify": False, "workspace": False, "trusted": True}),
        ("写·.git 内", "write_file", {"path": gitfile},
         # 连 trusted 都不该拦（那是"有理由才用"的档），但另外三档必须拦
         {"readonly": False, "verify": False, "workspace": False, "trusted": True}),
        ("写·根目录本身", "write_file", {"path": str(root)},
         {"readonly": False, "verify": False, "workspace": False, "trusted": True}),
        ("跑·普通命令", "run_check", safe,
         {"readonly": False, "verify": True, "workspace": True, "trusted": True}),
        ("跑·不可逆命令", "run_check", danger,
         {"readonly": False, "verify": False, "workspace": False, "trusted": True}),
        # ★ 2026-10-27 改口径：这里原来是 {workspace: True, trusted: True}，
        #   也就是**把 bug 写进了自检** —— 一个没登记的工具在 workspace 下放行，
        #   自检还说"对"。现在反过来：没登记就是拒，四档一致。
        ("用·没登记的工具", "some_new_tool", {},
         {"readonly": False, "verify": False, "workspace": False, "trusted": False}),
        # meta 档：只读/verify 拒，workspace/trusted 放（这是原来的行为，不改）
        ("用·meta 工具", "ask_human", {"question": "选哪个"},
         {"readonly": False, "verify": False, "workspace": True, "trusted": True}),
    ]

    bad = 0
    print("%-16s" % "动作" + "".join("%-11s" % m for m in MODES))
    for name, tool, args, want in CASES:
        row = ""
        for mode in MODES:
            got = Policy(mode, root=root).check(tool, args)[0]
            exp = want[mode]
            ok = (got == exp)
            bad += 0 if ok else 1
            row += "%-11s" % (("准" if got else "拦") + ("" if ok else "≠应" + ("准" if exp else "拦")))
        print("%-16s%s" % (name, row))

    # ── 规则用例（规则从参数传，不读真实配置）──
    R = {"deny": ["git push"], "allow": ["python bin/accept.py"],
         "only": [], "source": ["<test>/.harness/policy.json"]}
    O = {"deny": [], "allow": [], "only": ["pytest", "python bin/accept.py"],
         "source": ["<test>/.harness/policy.json"]}
    _no, _yes = False, True
    # ★ 四档**写全**。原来漏的档默认取 workspace 的值，于是 readonly 那列
    #   全被当成"该放行"—— 而那几行是"只读档不许跑命令"，本来就该拦。
    RCASES = [
        ("规则·deny 命中", "run_check", {"cmd": ["git", "push", "-u", "origin"]},
         R, {"readonly": _no, "verify": _no, "workspace": _no, "trusted": _no}),
        ("规则·deny 不误伤", "run_check", {"cmd": ["git", "status"]},
         R, {"readonly": _no, "verify": _yes, "workspace": _yes, "trusted": _yes}),
        # allow = 豁免词表：命令里带 "unlink"（词表里有），但人在 allow 里确认过
        ("规则·allow 豁免词表", "run_check",
         {"cmd": ["python", "bin/accept.py", "unlink"]},
         R, {"readonly": _no, "verify": _yes, "workspace": _yes, "trusted": _yes}),
        ("规则·only 栏外", "run_check", {"cmd": ["npm", "install"]},
         O, {"readonly": _no, "verify": _no, "workspace": _no, "trusted": _no}),
        ("规则·only 栏内", "run_check", {"cmd": ["pytest", "-q"]},
         O, {"readonly": _no, "verify": _yes, "workspace": _yes, "trusted": _yes}),
        # run_script 以前根本不过词表（落在"认不出的工具"分支默认放行）
        ("规则·run_script 也要过词表", "run_script",
         {"script": "bin/x.py", "args": ["rmtree"]},
         {"deny": [], "allow": [], "only": [], "source": []},
         {"readonly": _no, "verify": _no, "workspace": _no, "trusted": _yes}),
    ]
    for name, tool, args2, rules2, want in RCASES:
        row = ""
        for mode in MODES:
            got = Policy(mode, root=root, rules=rules2).check(tool, args2)[0]
            exp = want[mode]
            ok = (got == exp)
            bad += 0 if ok else 1
            row += "%-11s" % (("准" if got else "拦") + ("" if ok else "≠应" + ("准" if exp else "拦")))
        print("%-16s%s" % (name, row))

    # 档位写错必须报错（这是 fail-closed 的根）
    try:
        Policy("workspcae", root=root)
        print("档位写错          没有报错  ← 这条必须修")
        bad += 1
    except ValueError:
        print("档位写错          报错（对）")

    # 默认档必须是围住的那一档
    if Policy(root=root).mode == "workspace":
        print("默认档            workspace（对）")
    else:
        print("默认档            %s  ← 应该是 workspace" % Policy(root=root).mode)
        bad += 1

    print()
    print("全对" if not bad else "有 %d 条不符预期" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if "--rules" in sys.argv:
        r = load_rules(Path.cwd())
        print("工作目录：%s" % Path.cwd())
        if not r.get("source"):
            print("没有任何命令规则（两个位置都没读到 .harness/policy.json）")
            print("  项目级：%s" % (Path.cwd() / RULES_FILE))
            print("  用户级：%s" % (HOME / RULES_FILE))
        else:
            print("读到的文件：")
            for x in r["source"]:
                print("  %s" % x)
            for k, label in (("deny", "一律拦"), ("only", "只允许"), ("allow", "免词表")):
                v = r.get(k) or []
                print("  %-6s %-5s %s" % (k, label, ("、".join(v) if v else "（空）")))
        for e in (r.get("errors") or []):
            print("  ⚠ 读坏了：%s" % e)
        raise SystemExit(0)
    print("policy.py —— 动手前的门禁（单文件，零依赖，可单独拷走）")
    print("  python policy.py --self-test   跑 档位 × 动作 的自检表")
    print("  在代码里：Policy(mode=%r, root=<工作目录>).check(工具名, 参数)"
          % ("workspace",))
    print("  档位：%s" % " / ".join(MODES))
