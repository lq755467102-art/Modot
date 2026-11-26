#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runcheck.py —— `run_check` 的**白名单**：只放行「只读核验」那类命令。

为什么从黑名单换成白名单（2026-10-26 用户拍板）
    原来是一张动词黑名单（`rm|mv|cp|chmod|>`）。它挡的是"我想到的坏命令"——
    `python -c "os.remove(...)"`、`sed -i`、`find -delete`、`git push`
    一个都挡不住，而"没想到的"永远比想到的多。
    白名单反过来：**不认识的默认不放**。多一条命令只是多一行配置，
    而黑名单漏一条就是一个洞。

★★ 最重要的设计约束：**不能把新 agent 锁死**
    权限收窄最典型的翻车方式，是把"它平时就要用的东西"一起挡掉 ——
    现象是「派了活，子 agent 回来说我做不到」，而日志里只有一条 rc=126。
    所以这份名单是照着"一个正常 agent 一天会跑什么"列的，而且：
      · 拒的时候必须说清**换哪条命令**（不是只说"不许"）；
      · 探针里有一组"常用命令必须全过"的判据 —— 少一条就是锁死，会红；
      · 配置可以**加**（`run_check.extra_bins`），不是"整张表换掉"：
        换成替换语义的话，谁写一份不完整的表，就等于把自己锁在门外。

★ 三层判定（按顺序，命中即停）
    ① 这个可执行文件在不在名单里；
    ② 它的参数是不是踩了"写了世界"的那几条（`find -delete` / `sed -i` / `git push` …）；
    ③ 参数里的路径在不在工作区内（和 `read_file` 同一条边界）。
    第 ③ 层是这次顺带补的一个**真洞**：`cat ~/.xxx/credentials.env` 以前是通的 ——
    `cat` 在白名单里，而路径没人看。读文件的门在 `read_file` 那边，`run_check` 绕过了它。

★ trusted 档不弹授权（2026-10-30 用户拍板）
    白名单原本**自己**发 `needs_human`，不看权限档 —— 实测最近 40 个会话里
    7 次停点全部来自这里，`trusted` 一次都没生效。现在 `verdict(relaxed=True)`：
      · 未知命令、工作区外路径、`python -c` 直接放行；
      · 凭据路径、危险参数、改世界的内联代码仍然 `hard_deny`，**不弹授权**。
    放行口径集中在这一个函数里，工具层不再各写一套 if。

★ 仍然不是沙箱
    命令是在本机跑的，能读什么最终取决于 OS 权限。这里挡的是"顺手就把事做坏"，
    挡不住"铁了心要绕过"。所以叫白名单，不叫沙箱 —— 这个区别要一直说清。
"""
# 主要引用方包括 `tools.py` 和 `diagnose.py`。
from __future__ import annotations

import json

# ★ 「这一档允许一句话验算吗」（2026-10-29）。默认 **不许**。
#   job 模式会把它打开（见 bin/run-task.py）：那一档有落地前快照 + changes 哈希 +
#   scope 门禁，验算被拦的代价（每个想算个数的人都得停下来问人）远大于收益。
#   交互式保持关闭 —— 那种没有快照，`-c` 就是一道绕过所有门禁的任意代码。
# ★ 为什么是模块级而不是 contextvar（2026-10-29 实测撞到）：
#   换成 contextvar 之后**开关打不开** —— `run_check` 是跑在工具执行线程里的，
#   而那个线程的 context 是新的，读到的永远是 default(False)。
#   现象是"代码改了、开关也调了，命令还是被拒"，理由还是那句旧的。
#   run-task 一个进程只跑一轮，没有并发多任务，模块级够用而且不会错。
_ALLOW_INLINE = [False]


def set_allow_inline(on: bool) -> None:
    """由调用方（job 模式）打开/关闭。"""
    _ALLOW_INLINE[0] = bool(on)


def allow_inline() -> bool:
    return bool(_ALLOW_INLINE[0])


# 一句话验算里出现这些就拒。★ 这是**启发式，不是保证** ——
#   它挡的是"顺手写文件/删东西/联网/起进程"这类明显动作，
#   挡不住刻意绕（`getattr(__builtins__, ...)` 那种）。真正的边界还是 scope + 快照。
#   方向选保守：误拦的代价是"换个写法"，错放的代价是绕过所有门禁。
_INLINE_DANGER = (
    "open(", "write", "unlink", "rmtree", "rename", "mkdir", "makedirs",
    "chmod", "chown", "remove(", "system(", "popen", "subprocess",
    "socket", "urllib", "requests", "httpx", "shutil", "eval(", "exec(",
    "__import__", "input(",
)


def _inline_bad(code: str) -> str:
    """这段一句话代码里，第一处看着像"改世界"的东西。空串 = 没发现。"""
    low = (code or "").lower()
    for w in _INLINE_DANGER:
        if w in low:
            return w
    return ""

import re
from pathlib import Path

# ───────────────────── 名单：一个正常 agent 一天会跑什么 ─────────────────────
# ★ 刻意宽：这份名单的失败模式是"锁死"，而不是"放进来一个坏命令"——
#   坏命令有第 ② ③ 层兜着，而锁死没有任何一层兜。
ALLOW_BINS = (
    # 看目录 / 看文件
    "ls", "dir", "cat", "type", "head", "tail", "wc", "tree", "file", "stat",
    "du", "df", "pwd", "echo", "basename", "dirname", "realpath", "readlink",
    # 找
    "grep", "egrep", "fgrep", "rg", "find", "findstr", "where", "which", "locate",
    # 比
    "diff", "cmp", "comm", "sort", "uniq", "cut", "tr", "nl", "md5sum", "sha256sum",
    # 解释器（**受限**：见 `_interp()`，只认 `脚本文件` / `-m 白名单模块` / `-V`）
    "python", "python3", "py", "node", "bash", "sh",
    # 版本控制（**子命令另有一张表**：`git status` 和 `git push` 是同一个可执行文件）
    "git",
    # 检查和测试（都是只读的默认行为）
    "pytest", "ruff", "mypy", "flake8", "eslint", "tsc",
)
# git 只放只读子命令。`git status` 和 `git push` 是同一个可执行文件 ——
# 所以"命令级"白名单必须细到子命令，不然 `git` 这一条就等于全开。
ALLOW_GIT_SUB = (
    "status", "log", "diff", "show", "blame", "ls-files", "ls-tree", "rev-parse",
    "describe", "shortlog", "cat-file", "grep", "branch", "tag", "remote",
    "stash", "worktree", "config", "whatchanged", "count-objects", "check-ignore",
)
# ★ 这几个 git 子命令本身是只读的，但**带上这些参数就变成写**。
DENY_GIT_ARG = {
    "branch": ("-d", "-D", "-m", "-M", "--delete", "--move"),
    "tag": ("-d", "--delete", "-a", "-s", "-m"),
    "remote": ("add", "remove", "rm", "set-url", "prune"),
    "stash": ("pop", "apply", "drop", "clear", "push"),
    "worktree": ("add", "remove", "prune"),
    "config": ("--add", "--unset", "--replace-all", "--global", "--system"),
}
# 第 ② 层：按可执行文件列的"参数一出现就是写"
DENY_ARG = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fwrite",
             "-fls"),
    "sed": ("-i", "--in-place"),
    "sort": ("-o", "--output"),
    "tee": (),
    "ruff": ("--fix", "--unsafe-fixes"),
    "eslint": ("--fix",),
    "tsc": ("--build",),
}
# 第 ③ 层：这些**路径**就算在工作区里也不给读 —— 它们的存在就是为了不给 agent 看
DENY_PATH = (
    r"(^|[\\/])api-token$",          # 接口钥匙（2026-10-26）
    r"(^|[\\/])credentials(\.env)?$",
    r"\.env$",
    r"(^|[\\/])id_(rsa|ed25519|ecdsa)$",
    r"\.ssh[\\/]",
    # ★ 许可表（2026-10-26）：它要是能被 `cat` 出来，人批过什么就全泄露了；
    #   能被 `echo >>` 写进去，那"停下来问"等于形同虚设。
    r"(^|[\\/])grants\.jsonl$",
    r"(^|[\\/])grants\.lock$",
    # ★ 沙箱规则文件（2026-10-26）：读它就知道"哪条禁止、名单里有谁"，
    #   写它等于自己开免死金牌。两个方向都挡。
    r"(^|[\\/])sandbox_rules\.json$",
)

# 解释器允许 `-m` 的模块（只读的检查器）。别的一律拒 —— `-m pip install` 就是装包。
ALLOW_MODULES = ("pytest", "unittest", "mypy", "ruff", "py_compile", "compileall",
                 "json.tool", "timeit")
# 解释器允许的纯信息开关
INFO_FLAGS = ("-V", "--version", "-h", "--help")

_WHY = {
    "rm": "删东西", "rmdir": "删目录", "del": "删文件", "mv": "移动/改名",
    "cp": "复制（会覆盖目标）", "dd": "写裸设备", "mkfs": "格式化",
    "chmod": "改权限", "chown": "改属主", "kill": "杀进程", "taskkill": "杀进程",
    "shutdown": "关机", "reboot": "重启", "reg": "改注册表", "sc": "改服务",
    "curl": "联网发请求", "wget": "联网下载", "nc": "联网", "ssh": "连远程",
    "scp": "传文件", "rsync": "同步/覆盖", "pip": "装包", "pip3": "装包",
    "npm": "装包/跑脚本", "npx": "装包/跑脚本", "yarn": "装包", "pnpm": "装包",
    "apt": "装包", "choco": "装包", "winget": "装包", "docker": "起容器",
    "powershell": "能跑任意东西（而且是一整段脚本）",
    "cmd": "能跑任意东西", "pwsh": "能跑任意东西",
    "env": "会把环境变量（含 API key）打出来",
    "printenv": "会把环境变量（含 API key）打出来",
    "set": "会把环境变量打出来", "export": "改环境", "source": "执行文件内容",
    "xargs": "把上一条的输出变成命令", "shred": "彻底删", "sudo": "提权",
    "chcp": "改代码页（会影响输出解读）",
}


def _baseline_patterns() -> tuple:
    """底线清单里的**名字模式**（2026-10-26）。

    ★ 命令层只能看到参数里的字符串（`cat` + 一个路径），没法做"绝对路径相等"，
      所以只能按名字挡。它比文件工具那一层**宽** —— 这是刻意的：
      宁可多挡一次 `cat charter.json`，也不要让 `cat ~/.ssh/id_rsa` 过去。
    """
    try:
        from . import baseline as BL
        return tuple(BL.regexes())
    except Exception:
        return ()


def _re():
    pats = tuple(DENY_PATH) + _baseline_patterns()
    return re.compile("|".join("(?:%s)" % p for p in pats), re.I)


_DENY_PATH_RE = _re()


def _leaf(a: str) -> str:
    """取可执行名：去掉目录和后缀（`C:/x/python.exe` → `python`）。"""
    b = str(a or "").replace("\\", "/").split("/")[-1].lower()
    for suf in (".exe", ".cmd", ".bat", ".com"):
        if b.endswith(suf):
            b = b[:-len(suf)]
    return b


def _looks_path(a: str) -> bool:
    s = str(a or "")
    if not s or s.startswith("-"):
        return False
    if s in (".", ".."):
        return True
    return ("/" in s) or ("\\" in s) or s.startswith("~")


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _deny_path_ok(a: str) -> bool:
    """这个路径是不是"就算在仓库里也不给看"的那一类。"""
    return not _DENY_PATH_RE.search(str(a or "").replace("\\", "/"))


def _hint_for(bin_name: str, why: str = "") -> str:
    """拒了之后告诉它**怎么办**。这一句是"不锁死"的关键 —— 只说"不许"等于锁死。"""
    tail = ("要改文件请走 write_file / edit_file（它们有镜像和四道门）；"
            "要跑一整段脚本请走 run_script；只是看一眼就用 read_file / list_dir（更省）。")
    if bin_name in ("bash", "sh", "powershell", "cmd", "pwsh"):
        return ("`run_check` 不收 `-c` 这种「把一整段脚本递进来」的用法（它没法核）。"
                "要看东西用 cat/grep/ls；要跑脚本用 run_script；"
                "要管道就把命令拆成两条。")
    if bin_name in ("python", "python3", "py", "node"):
        return ("解释器只认三样：`脚本文件`（在工作区内）、`-m pytest|unittest|mypy|ruff`、"
                "`-V`。`-c` 是任意代码，不收。" + tail)
    if why:
        return why + "。" + tail
    return tail


def verdict(cmd, root=None, cfg: dict | None = None,
            relaxed: bool = False) -> dict:
    """判一条命令能不能跑。返回 {ok, why, hint, bin}；`ok=True` 时其余字段为空。

    `relaxed=True` 是 trusted 档的口径：**不再用只读白名单拦未知命令、工作区外路径、
    解释器任意代码**；但凭据路径、危险参数和不可逆动作仍然是硬拒绝。
    这里只表达「能不能安全放行」，不负责弹授权卡 —— 那是权限层的事。
    """
    # ★ 「数组被写成字符串」要单独认（2026-10-29 实测撞到）：模型有时把
    #   `["python","-c","..."]` 整个塞进一个字符串再传进来。那一刻 `parts[0]` 是
    #   `'["python"'`，白名单回的是"`["python"` 不在只读白名单里" ——
    #   指的方向完全是错的，它会以为这个命令不让跑，去换命令，而实际只是格式错。
    if isinstance(cmd, str) and cmd.strip().startswith("[") and cmd.strip().endswith("]"):
        try:
            _arr = json.loads(cmd)
        except Exception:
            _arr = None
        if isinstance(_arr, list):
            return {"ok": False,
                    # 参数格式错误与「这条命令要人授权」是两类结果。
                    # 下游据此把错误还给模型改，不能消耗许可、更不能整轮停下。
                    "invalid_args": True,
                    "why": "cmd 传成了一个**字符串化的数组**",
                    "hint": "cmd 本身就要是数组：`[\"python\", \"-c\", \"...\"]`，"
                            "不要把整个数组塞进一个字符串里。这不是权限问题。"}
    parts = ([str(x) for x in cmd] if isinstance(cmd, (list, tuple))
             else [str(cmd or "")])
    parts = [p for p in parts]
    if not parts or not parts[0].strip():
        return {"ok": False, "invalid_args": True,
                "why": "命令是空的", "hint": "cmd 要写成数组，例如 [\"ls\",\"-la\"]"}

    root_p = Path(root or ".")
    bins = set(ALLOW_BINS)
    extra = []
    if cfg:
        extra = [str(x).strip().lower() for x in (cfg.get("extra_bins") or []) if str(x).strip()]
    bins |= set(extra)

    b = _leaf(parts[0])
    # ③ 禁读名单：**每个参数都查**，不管它像不像路径。
    #    ★ 这一条是探针逮出来的（2026-10-26）：原来只在"看起来像路径"的参数上查，
    #      而 `api-token` 这种**裸文件名**不含斜杠 ⇒ 跳过 ⇒ `cat api-token`
    #      直接把接口钥匙打出来了。名字越短的东西越容易被这个漏洞放过。
    #    ★ 放在白名单之前（2026-10-30）：trusted 会跳过未知命令检查，
    #      如果还放在后面，`未知命令 ~/.ssh/...` 会绕过它。
    for a in parts[1:]:
        if not _deny_path_ok(a):
            return {"ok": False, "bin": b, "hard_deny": True, "blocked_by": "baseline",
                    "why": "路径 `%s` 在禁读名单里（钥匙/凭据这类）" % a,
                    "hint": "这些文件的存在就是为了不给 agent 看；要确认配置对不对，请人来看。"}
    # ① 可执行名
    if b not in bins and not relaxed:
        why = _WHY.get(b)
        return {"ok": False, "bin": b,
                "why": ("`%s` 不在只读白名单里" % b) + ("（它%s）" % why if why else ""),
                "hint": _hint_for(b, why or ""),
                "cfg": "确实需要它，就把它加进 harness/charter.json 的 "
                       "`run_check.extra_bins`（是**追加**，不是整表替换）"}
    # ② 参数
    if b == "git":
        sub = next((x for x in parts[1:] if not x.startswith("-")), "")
        if sub and sub not in ALLOW_GIT_SUB:
            return {"ok": False, "bin": "git", "why": "`git %s` 不是只读子命令" % sub,
                    "hint": _hint_for("git", "git 里只放 status/log/diff/show/blame 这类；"
                                             "push/commit/reset/checkout 属于改世界")}
        for bad in DENY_GIT_ARG.get(sub, ()):
            if bad in parts:
                return {"ok": False, "bin": "git", "hard_deny": True, "blocked_by": "baseline",
                        "why": "`git %s %s` 会改东西" % (sub, bad),
                        "hint": _hint_for("git", "这条子命令只有带 `%s` 时才写" % bad)}
    for bad in DENY_ARG.get(b, ()):
        if bad in parts:
            return {"ok": False, "bin": b, "hard_deny": True, "blocked_by": "baseline",
                    "why": "`%s %s` 会改东西" % (b, bad),
                    "hint": _hint_for(b, "`%s` 这个参数本身就是写操作" % bad)}
    if b in ("python", "python3", "py", "node", "bash", "sh"):
        if relaxed:
            # trusted 下 `python -c` 这类任意代码放行；但裸解释器会开交互卡住，
            # 仍作为**参数错误**退回，不弹授权。
            if len(parts) == 1:
                return {"ok": False, "bin": b, "invalid_args": True,
                        "why": "没给参数（裸解释器会开交互，卡住）",
                        "hint": _hint_for(b)}
            # ★ 放行任意代码，不等于不看内容：一句话验算先扫明显改世界的动作。
            #   这是启发式，挡不住刻意绕；但 trusted 仍承诺「不可逆动作硬拒」。
            if "-c" in parts or "-e" in parts or parts[1] == "-":
                _i = (parts.index("-c") if "-c" in parts else
                      parts.index("-e") if "-e" in parts else 1)
                _code = parts[_i + 1] if _i + 1 < len(parts) else ""
                _bad = _inline_bad(_code)
                if _bad:
                    return {"ok": False, "bin": b, "hard_deny": True,
                            "blocked_by": "baseline",
                            "why": "这一句里出现了 `%s`，属于改世界/联网动作" % _bad,
                            "hint": "trusted 也不执行不可逆动作；要改文件走 write_file / edit_file。"}
        else:
            r = _interp(b, parts, root_p)
            if not r.get("ok"):
                return r
    # ④ 路径边界：像路径的参数必须在工作区内。
    #    trusted 不设这条（和 read_file 在 trusted 下允许读工作区外保持一致）；
    #    凭据路径已经在上面硬拦。
    for a in (() if relaxed else parts[1:]):
        if not _looks_path(a):
            continue
        p = Path(a)
        if not p.is_absolute():
            p = root_p / a
        if not _inside(p, root_p):
            return {"ok": False, "bin": b,
                    "why": "路径 `%s` 在工作区外（%s）" % (a, root_p),
                    "hint": "`run_check` 和 `read_file` 是同一条边界：只看工作区里面。"
                            "真要读外面，请人来做。"}
    return {"ok": True}


def _interp(b: str, parts: list[str], root: Path) -> dict:
    """解释器单独一套：只认 `脚本文件` / `-m 白名单模块` / `-V`。

    ★ 为什么不能像别的命令那样"参数过一遍就行"：`python -c "..."` 的参数里
      **不含路径、也不含危险动词**，它是任意代码。白名单对它唯一有效的形态，
      是"只允许跑一个文件"——文件路径还能被第 ③ 层管住，字符串管不住。
    """
    args = parts[1:]
    if not args:
        return {"ok": False, "bin": b, "why": "没给参数（裸解释器会开交互，卡住）",
                "hint": _hint_for(b)}
    for a in args:
        if a in INFO_FLAGS:
            return {"ok": True}
    if "-c" in args or "-e" in args or args[0] == "-":
        # ★ 默认拒。只有调用方显式打开（job 模式）才放，而且**放之前先过一遍词表**
        #   （2026-10-29）：放行不等于不看着 —— 这是"降一档"，不是"取消"。
        if allow_inline():
            i = args.index("-c") if "-c" in args else (
                args.index("-e") if "-e" in args else args.index("-"))
            code = args[i + 1] if i + 1 < len(args) else ""
            bad = _inline_bad(code)
            if bad:
                # ★ 这里必须是**硬拒**，不是「要人点头」（2026-10-30）：
                #   测试者也打开了 inline。若返回普通 false，它会走 needs_human，
                #   测试者在只读副本里停住等一个根本不该问的问题。
                return {"ok": False, "bin": b, "hard_deny": True,
                        "blocked_by": "baseline",
                        "why": "这一句里出现了 `%s` —— 那是改世界的动作，"
                               "一句话验算只允许算、不允许改" % bad,
                        "hint": "验算请只写表达式 / print / assert。要改文件走 edit_file"
                                "（那一层有范围门禁和落地前快照）。"}
            return {"ok": True}
        return {"ok": False, "bin": b,
                "why": "`%s %s` 是「把一段代码递进来」，没法核它做什么" % (b, args[0]),
                "hint": _hint_for(b)}
    if "-m" in args:
        i = args.index("-m")
        mod = args[i + 1] if i + 1 < len(args) else ""
        if mod.split(".")[0] not in ALLOW_MODULES:
            return {"ok": False, "bin": b, "why": "`%s -m %s` 不在允许的模块里" % (b, mod),
                    "hint": "只放 pytest / unittest / mypy / ruff 这类只读检查器。"
                            + _hint_for(b)}
        return {"ok": True}
    return {"ok": True}      # 剩下的交给第 ③ 层判路径


def explain(cfg: dict | None = None) -> str:
    """给界面/文档用的一段人话（"现在放行哪些"）。"""
    extra = [str(x) for x in ((cfg or {}).get("extra_bins") or []) if str(x).strip()]
    return ("只读白名单：%d 个命令 + git 的 %d 个只读子命令%s"
            % (len(ALLOW_BINS), len(ALLOW_GIT_SUB),
               ("；另加了 %s" % "、".join(extra)) if extra else ""))
