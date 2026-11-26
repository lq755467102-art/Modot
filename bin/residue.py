#!/usr/bin/env python3
"""residue.py —— 查「已经犯过的错」的同类残留

为什么要有这个
    开发这套东西的过程里，我犯了 12 次判据错。12 次全是**我自己**发现的。
    发现者 = 加害者，这等于没有独立审查。
    更麻烦的是：改掉那几行 ≠ 改掉那个思路。同一个思路会换个形状再犯一次
    （比如「拿词当结构」先是查字符串，后来变成查 dict 的 key 名）。

    所以这里不是查「那几行改没改」，是查**那个错法还在不在别处**。

它查八类（每条对应一次真实踩过的错）
    R0 源码/夹具不可解析   —— printf 把 \\n 写成 /n，T1 白跑 14 轮
    R1 拿词当结构          —— PASS/FAIL、dict key 猜类型
    R2 无锚定替换          —— 数字/短串 replace 误伤「300 行/300 秒」
    R3 验收段重复          —— 重复插入 4 次，数字虚高到 389
    R4 替换后没验就开跑    —— 变异写坏代码，红的 6 条被误当证据
    R5 夹具缺失或空        —— 整轮跑完才知道没测到东西
    R6 只认一种返回形状    —— 工具调用只认一家格式，名字读成 None
    R7 chdir 之后用相对路径 —— 静默指到别处
    R8 序号不从已有事件续  —— 续跑重号，按序排全废
    R9 文件根本 parse 不了 —— 中文里混了 ASCII 引号那类，跑到才知道

用法
    python bin/residue.py            # 只报，不改
    python bin/residue.py --strict   # 有发现就 rc=1
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = ("bin", "loop", "tools")
SRC = [p for d in DIRS if (ROOT / d).is_dir() for p in sorted((ROOT / d).glob("*.py"))]

# ★ 能被指向别处，是为了让它自己被验。
#   一个只能扫自己仓库的检查器，没法证明它抓得住 —— 因为仓库是干净的。
#   验收里会造一个**已知有毛病**的临时目录，看它报不报。
_OVERRIDE_ROOT: Path | None = None

# ★ 必须锚定：`os.replace(tmp, p)` 是原子写，不是字符串替换。
#   第一版没锚定，把 swap.py 的原子写报了 4 处假警 ——
#   检查器自己犯了它要查的错（正则没锚定），这类噪音会让人干脆不看报告。
_REPL = re.compile(r'(?<!os)\.replace\(')

FINDINGS: list[tuple[str, str, int, str]] = []


def add(rule: str, path, line: int, msg: str) -> None:
    p = path if isinstance(path, str) else path.relative_to(ROOT).as_posix()
    FINDINGS.append((rule, p, line, msg))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ───────────────────────── R0 能不能解析 ─────────────────────────
def r0() -> None:
    for p in SRC:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ast.parse(read(p))
        except SyntaxError as e:
            add("R0", p, e.lineno or 0, "语法错误：%s" % e.msg)
            continue
        for w in caught:
            if issubclass(w.category, SyntaxWarning):
                add("R0", p, getattr(w, "lineno", 0) or 0,
                    "编译告警：%s —— 现在是告警，将来是错误" % w.message)
    bdir = ROOT / "bench"
    if bdir.is_dir():
        for d in sorted(bdir.glob("t*")):
            f = d / "task.md"
            if not f.exists():
                add("R0", f, 0, "夹具目录里没有 task.md")
            elif not f.read_text(encoding="utf-8", errors="replace").strip():
                add("R0", f, 0, "夹具任务文件是空的")
            elif "/n" in f.read_text(encoding="utf-8", errors="replace") and "\\n" not in f.read_text(encoding="utf-8", errors="replace"):
                add("R0", f, 0, "疑似 \\\\n 被写成了 /n")


# ───────────────────────── R1 拿词当结构 ─────────────────────────
def r1() -> None:
    # ★ 已知盲区：只有变量名像「外部返回值」时才报。
    #   为什么不一刀切全报：provider.py 里 `if "tool" in step` 读的是测试剧本
    #   （我们自己造的 dict，形状固定），不是外部返回值，报它纯属噪音。
    #   但代价是：换个名字的外部返回值就漏了。这条规则是启发式，不是证明。
    EXT = {"out", "output", "result", "res", "resp", "reply", "ret", "data"}
    for p in SRC:
        t = read(p)
        has_pass = re.search(r'["\']PASS["\']\s+(not\s+)?in', t)
        has_fail = re.search(r'["\']FAIL["\']\s+(not\s+)?in', t)
        if has_pass and not has_fail:
            ln = t[: has_pass.start()].count("\n") + 1
            add("R1", p, ln, "只查 PASS 没查 FAIL：换个说法就漏，得两个方向都查")
        for i, ln in enumerate(t.splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            m = re.search(r'if\s+["\']([a-z_]{2,})["\']\s+in\s+([a-z_]+)\s*:', ln)
            if m and p.parent.name == "loop" and m.group(2) in EXT:
                add("R1", p, i,
                    "按 dict 的 key 猜类型（%r in %s）：形状相近的返回值会互相误判，"
                    "应按工具名分派" % (m.group(1), m.group(2)))


# ───────────────────────── R2 无锚定替换 ─────────────────────────
def r2() -> None:
    pat = re.compile(r'(?<!os)\.replace\(\s*(["\'])(.+?)\1\s*,')
    for p in SRC:
        for i, ln in enumerate(read(p).splitlines(), 1):
            if ln.lstrip().startswith("#"):
                continue
            m = pat.search(ln)
            if not m:
                continue
            a = m.group(2)
            if a.isalnum() and len(a) <= 3:
                add("R2", p, i, "replace 目标是 %r：太短，会误伤长串里的同段" % a)


# ───────────────────────── R3 验收段重复 ─────────────────────────
def r3() -> None:
    p = ROOT / "bin" / "accept.py"
    if not p.exists():
        return
    t = read(p)
    names = re.findall(r'print\(\s*"===\s*(.+?)\s*==="', t)
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    for n, c in seen.items():
        if c > 1:
            add("R3", p, 0, "组名 %r 出现 %d 次：整段可能被重复贴过" % (n, c))
    titles = re.findall(r'chk\(\s*"([^"]*)"', t)
    seen3: set[tuple[str, ...]] = set()
    for i in range(len(titles) - 2):
        seg = tuple(titles[i:i + 3])
        if seg in seen3:
            add("R3", p, 0, "连续 3 条断言在别处也出现过：%s" % (seg,))
            break
        seen3.add(seg)


# ───────────────────────── R4 替换后没验就开跑 ─────────────────────────
def r4() -> None:
    # 精准判据：**同一个函数里，读了某个文件的文本、又写回同一个路径**。
    # 第一版只查「文件里有 replace 也有 write_text」，报出 11 处假警：
    #   review.py 是 markdown 表格转义、tools.py 是行尾归一化、swap.py 是 os.replace 原子写。
    # 噪音一多，真问题（mutation-probe）就淹掉了 —— 和「验收数字虚高」同一个病。
    for p in SRC:
        t = read(p)
        if not _REPL.search(t):
            continue
        try:
            tree = ast.parse(t)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(t, fn) or ""
            reads = set(re.findall(r'(\w+)\.read_text\(', seg))
            writes = set(re.findall(r'(\w+)\.write_text\(', seg))
            if not (reads & writes) or not _REPL.search(seg):
                continue
            first = fn.lineno + seg[: _REPL.search(seg).start()].count("\n")
            # ★ 窄口豁免（2026-10-26）：只有函数体里显式写 `noqa: R4` 才跳过。
            #   为什么要留这个口：R4 防的是"改了 Python 源码没验"，而有些函数
            #   改的是 **markdown**（记忆树、习惯区）—— 对它做 ast.parse 没有意义。
            #   不开口的后果是人去改检查器，而不是去看那条真正的风险。
            if "noqa: R4" in seg:
                continue
            if "ast.parse" not in t:
                add("R4", p, first,
                    "读源码→替换→写回，没验能不能 ast.parse："
                    "写坏了，验收全红会被读成「证据」")
            if not re.search(r'\.count\(', seg):
                add("R4", p, first,
                    "没验替换命中数：目标没命中也照跑，等于测了一份「改了一半」的代码")


# ───────────────────────── R6 只认一种返回形状 ─────────────────────────
def r6() -> None:
    # 只认「真的在取 name」的写法。第一版查 '["function"]' 这个字符串出现过没有，
    # 结果扫到了检测器自己的源码 —— 规则文本里必然含被查的模式。
    # 检查器扫自己永远是噪音，直接排除，并写明理由。
    pat = re.compile(
        r'(\[["\']function["\']\]|\.function)\s*'
        r'(\.get\(\s*["\']name|\[["\']name["\']\]|\.name)')
    for p in SRC:
        if p.name == "residue.py":
            continue
        t = read(p)
        if not pat.search(t):
            continue
        fb = re.search(r'(get\(\s*["\']name["\']|\[["\']name["\']\])\s*,?\s*'
                       r'(or\s+|\})', t)
        if not fb:
            add("R6", p, 0, "读 tool_calls 只认 function 形状：另一种形状的 name 会读成 None")


# ───────────────────────── R7 chdir 之后用相对路径 ─────────────────────────
def r7() -> None:
    for p in SRC:
        lines = read(p).splitlines()
        marks = [i for i, ln in enumerate(lines) if "chdir(" in ln]
        if not marks:
            continue
        for i, ln in enumerate(lines):
            if i <= marks[0]:
                continue
            if ln.lstrip().startswith("#"):
                continue
            # ★ 窄口豁免：只有显式写 `noqa: R7` 才跳过。
            #   为什么需要：判据命令是**字符串**，里面本来就该用相对路径
            #   （它要在当前目录跑）。改成绝对路径反而会让"空世界"对照失效 ——
            #   绝对路径在任何 cwd 都指向真文件，那测什么都是绿的。
            if "noqa: R7" in ln:
                continue
            # ★ 必须锚定词边界。第二版漏了，把 `urllib.urlopen("http://...")`
            #   当成 `open("相对路径")` 报了假警 —— 而这是同一条规则第二次犯
            #   「正则没锚定」的错（第一次是 os.replace 被当成字符串替换）。
            #   教训：写正则先问「它会不会匹配到更长的词」。
            if re.search(r'\b(open|Path)\(\s*["\'](?![\\/]|[A-Za-z]:)', ln):
                add("R7", p, i + 1, "chdir 之后用了字面相对路径：会静默指到别处")


# ───────────────────────── R8 序号没从已有事件续 ─────────────────────────
def r8() -> None:
    p = ROOT / "loop" / "session.py"
    if not p.exists():
        return
    t = read(p)
    if "def _last_seq" not in t and "max(" not in t:
        add("R8", p, 0, "没有从已有事件续号的逻辑：续跑会从 1 重来")
    for i, ln in enumerate(t.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        if re.search(r'\bseq\s*=\s*1\b', ln) and "self.seq + 1" not in ln:
            add("R8", p, i, "seq 硬起为 1：续跑会重号")


# ───────────────────────── R9 文件 parse 不了 ─────────────────────────
def r9() -> None:
    """每个源文件先过一遍 `ast.parse`。

    ★ 为什么要有这一条（2026-10-26）：中文提示词里混进一个 ASCII 双引号，
      `"...被要求"别做"，是做不了"` 这种 —— 肉眼看不出来，报错只报"第 N 行非 ASCII"，
      而 N 往往是**整个文件**里最不显眼的那一处。今天 4 次，全都发生在写长中文串的时候。
    ★ 为什么不靠"跑一遍"兜底：`bin/accept.py` 一万行，语法错要到它跑完才炸，
      而它跑一次 95 秒 —— 中间那 95 秒会让人以为"改对了"。
    """
    for p in SRC:
        try:
            ast.parse(read(p))
        except SyntaxError as e:
            FINDINGS.append(("R9", str(p.relative_to(ROOT)), e.lineno or 0,
                             "这个文件 parse 不了：%s —— 先修语法，别谈行为" % e.msg))
        except Exception:
            pass


def main(argv=None) -> int:
    global ROOT, SRC
    ap = argparse.ArgumentParser(description="查已犯过的错的同类残留")
    ap.add_argument("--strict", action="store_true", help="有发现就 rc=1")
    ap.add_argument("--root", default="",
                    help="扫哪个目录（默认本仓库）。验收用它指向一个已知有毛病的样本")
    a = ap.parse_args(argv)

    if a.root:
        ROOT = Path(a.root).resolve()
        SRC = [p for d in DIRS if (ROOT / d).is_dir()
               for p in sorted((ROOT / d).glob("*.py"))]
        FINDINGS.clear()

    for fn in (r9, r0, r1, r2, r3, r4, r6, r7, r8):
        fn()

    print("查了 %d 个文件（%s）" % (len(SRC), " / ".join(DIRS)))
    if not FINDINGS:
        print("\n没有发现同类残留。")
        print("★ 注意：这只能说明「我预设的九种错法不在」，")
        print("   说明不了「没有别的错法」—— 规则是我想出来的，这就是天花板。")
        return 0

    by_rule: dict[str, list] = {}
    for f in FINDINGS:
        by_rule.setdefault(f[0], []).append(f)
    print("\n发现 %d 处，按规则分组：" % len(FINDINGS))
    for rule in sorted(by_rule):
        print("\n【%s】%d 处" % (rule, len(by_rule[rule])))
        for _, p, line, msg in by_rule[rule]:
            loc = "%s:%d" % (p, line) if line else p
            print("  %-32s %s" % (loc, msg))
    return 1 if a.strict else 0


if __name__ == "__main__":
    sys.exit(main())
