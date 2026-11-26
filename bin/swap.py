#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""swap.py —— 在镜像里改，验过再接回去（一次可以是一组文件）

为什么不是"写文件之前先备份"
    业务方 2026-10-23 的判断：改写更像**器官移植**，不像打补丁。
    要改的那部分是器官，上下游是血管。正确做法不是在器官里动刀，而是：
        在外面把镜像做好 → 在镜像里改 → 接进去看输出 → 不满意就把原器官塞回去
    所以"可逆"不该是一个形容词，它得是**一个 .bak 在那儿、加一条能把它塞回去的命令**。

它防的失败模式，是最经典的那个
    **镜像会漂移。**
    从做镜像到切回去，原件可能已经被下游改过 —— 任务环境不是静态的。
    这时候"塞回去"塞回去的是一个**过期的器官**，而你以为回到了原点。
    ⇒ 所以 commit 之前必须核对原件的哈希；对不上就拒绝切。

────────────────────────────────────────────────────────────────────
一组改动怎么处理（2026-10-23 补。查了三家成熟做法，抄两条，否掉一条）
────────────────────────────────────────────────────────────────────
问题：真实改写的单位常常是**一组文件**（改一个模块 = 代码 + 测试 + 文档）。
一组里第一个切成功、第二个失败，"这组是一件事"就没被表达出来，回滚粒度是乱的。

**Capistrano（部署工具）** —— 每次发布到 `releases/<时间戳>/` 新目录，
切换 = 把 `current` 这个软链指过去。软链重指向是**一次 rename**，
所以外面看到的东西要么整个旧版、要么整个新版，**不会混合**。

    ✗ 范式抄不了：它要求"被替换的东西都在一个可替换的目录里"。
      我们的场景是就地改散落的文件，改成那种布局会改变下游看到的东西。
    ✓ 但抄它一句坦白。它的官方文档写的是 "Deploys are **mostly** atomic" ——
      不是完全原子，而且明确说：失败若发生在软链切换之后，会出现不一致状态，
      得靠 `deploy:rollback` 收拾。
      ⇒ 所以我们也只承诺 **mostly**，不吹"原子"。

**safefile / fs-transaction（Python 库）** —— 两个都做同样两件事：

    ① 两阶段：先把整组操作**全部校验**一遍（文件在不在、权限够不够），
       一条不过就整体不动手；全部通过才开始逐个执行。
    ② 逆序 undo：执行到一半失败，**倒着**把已经做过的撤掉。

    safefile 还多做一件更关键的事：**动文件之前先把 journal 落盘**，
    进程被 SIGKILL / 断电，下次启动 `recover_orphaned()` 能救回来。
    ⇒ 这条必须抄。**日志先落盘，动作后发生**（和数据库 ARIES 一个道理）。

────────────────────────────────────────────────────────────────────
所以这里给的保证是「可收敛」，不是「原子」
────────────────────────────────────────────────────────────────────
    rename 不是跨文件的原子操作，做不到"全成或全不成"。
    能做到的是：**任何中断点，都有一条命令能推回某个一致状态。**

        staged      什么都没动      → recover 直接取消（删镜像）
        committing  可能切了一半    → recover **逆序**把 .bak 全塞回去（回到全旧）
        committed   已经全切了      → recover **不动**，留给人看和决定
        rollingback 正在塞回        → 继续塞完

    这就是"mostly atomic"的准确含义：**不保证不出半截，保证半截能被收拾。**

────────────────────────────────────────────────────────────────────
四步
────────────────────────────────────────────────────────────────────
    stage <f1> [f2 ...] --note "..."    给整组做镜像（之后只改 .stage）
    check <f1>                          镜像还在吗？原件在镜像期间漂移了吗？
    commit <f1> [--force]               两阶段：全验 → 逐个 rename，每步落盘
    rollback <f1>                       逆序把 .bak 塞回去（只认 .bak，不看 state）
    recover <dir>                       把目录下所有没结束的切换推回一致状态
    status [dir]                        看有哪些进行中

★ journal 挂在**第一个文件**旁边（`<锚文件>.swap.json`），整组共用一份。
  传参时第一个就是锚 —— 后面几个只是为了让你确认改的是同一组。
★ .bak 不叫 .tmp：它是人确认满意之前唯一的恢复凭据，不许随手清。
★ 能镜像的才叫可逆。已发出的消息、已被人看到的输出、已扣的钱复制不了，
  把它们当"可逆"是这套办法最危险的误用 —— 那是 consumed，只能事先问人。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path


# ───────────────────── 基础 ─────────────────────

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def txid() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def who() -> str:
    u = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    return "%s@%s" % (u, platform.node())


def fslots(f) -> dict:
    """单个文件的三个位置。.stage / .bak 在文件旁边 —— 保证 rename 同盘。"""
    p = Path(f).resolve()
    return {"target": p,
            "stage": p.with_name(p.name + ".stage"),
            "bak": p.with_name(p.name + ".bak")}


def journal_for(anchor) -> Path:
    p = Path(anchor).resolve()
    return p.with_name(p.name + ".swap.json")


def sha(p: Path) -> str:
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def save_journal(p: Path, obj: dict) -> None:
    """★ journal 自己也要原子写：先写 .tmp 再 replace。
    不然"记日志"这一步自己崩在半路，日志就变成半截 JSON，
    恢复时连"做到哪了"都读不出来 —— 比不记还糟。"""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def load_journal(p: Path):
    """(journal, err)。兼容最早的单文件格式（没有 files 数组）。"""
    if not p.exists():
        return None, "这里没有进行中的切换（没有 %s）" % p.name
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return None, "journal 读不出来（%s）—— 别猜，先看看这个文件：%s" % (e, p)
    if "files" not in j:                      # 旧格式：顶层就是那一个文件
        j["files"] = [{"target": j.get("target"), "stage": j.get("stage"),
                       "bak": j.get("bak"), "orig_sha256": j.get("orig_sha256"),
                       "orig_size": j.get("orig_size"), "state": "staged"}]
    return j, ""


def _abs(j: dict) -> list[dict]:
    """把 journal 里的文件条目补成绝对路径。"""
    out = []
    for f in j["files"]:
        t = Path(f["target"]).resolve()
        s = Path(f["stage"]).resolve() if f.get("stage") else fslots(t)["stage"]
        b = Path(f["bak"]).resolve() if f.get("bak") else fslots(t)["bak"]
        out.append(dict(f, target=t, stage=s, bak=b))
    return out


# ───────────────────── stage ─────────────────────

def cmd_stage(a) -> int:
    files = [Path(x) for x in a.targets]
    if not files:
        print("× 至少要给一个文件")
        return 4
    anchor = files[0].resolve()
    jp = journal_for(anchor)
    if jp.exists():
        j, _ = load_journal(jp)
        print("× 这里已经有一次没结束的切换（%s 开始，状态 %s）" % (
            (j or {}).get("staged_at"), (j or {}).get("state")))
        print("  先 commit 或 rollback，别开第二个 —— 同时改两版镜像，等于没镜像。")
        return 2

    # ── 阶段一：整组全部预检，一条不过就什么都不动 ──
    for f in files:
        if not f.exists():
            print("× 原件不存在：%s" % f)
            print("  新建文件不需要镜像（它还没有下游在读），直接写。")
            return 4
        if f.is_dir():
            # ★ 这行原来会直接抛 PermissionError traceback —— 拿异常当报错。
            print("× 这是目录不是文件：%s" % f)
            print("  要改一整个目录，请把里面的文件逐个列出来。")
            print("  ⇒ 目录级的镜像（整棵树快照）还没做，这是已知缺口，别当成能绕。")
            return 4
    dup = {str(f.resolve()) for f in files}
    if len(dup) != len(files):
        print("× 同一组里有重复文件，去掉重复再试")
        return 4

    # ── 阶段二：逐个复制。任何一步失败，把已做的撤掉 ──
    made = []
    try:
        for f in files:
            s = fslots(f)
            shutil.copy2(s["target"], s["stage"])
            made.append(s)
    except OSError as e:
        for s in made:
            s["stage"].unlink(missing_ok=True)
        print("× 复制失败：%s" % e)
        print("  已把做过的那几个镜像删掉了。没做成镜像 ⇒ 没有退路 ⇒ 不许改原件。")
        return 4

    j = {
        "txid": txid(),
        "state": "staged",
        "anchor": str(anchor),
        "staged_at": now(),
        "by": who(),
        "note": a.note or "",
        "files": [{
            "target": str(s["target"]),
            "stage": str(s["stage"]),
            "bak": str(s["bak"]),
            "orig_sha256": sha(s["target"]),
            "orig_size": s["target"].stat().st_size,
            "state": "staged",
        } for s in map(fslots, files)],
    }
    save_journal(jp, j)
    print("✓ 镜像做好了：%d 个文件，这一组是一个 tx" % len(files))
    for f in j["files"]:
        print("    %s" % Path(f["target"]).name)
    print("  现在**只改 .stage**。原件在这段时间里被下游改了，接回去会被拦。")
    return 0


# ───────────────────── check ─────────────────────

def _validate(j: dict, force: bool):
    """阶段一：把整组的每条都验一遍，不只看第一条。返回问题列表。"""
    problems = []
    for f in _abs(j):
        if not Path(f["stage"]).exists():
            problems.append("%s：镜像不见了（没有镜像就没有退路）" % f["target"].name)
            continue
        cur = sha(Path(f["target"]))
        if cur != f.get("orig_sha256"):
            problems.append("%s：原件在镜像期间被改过（现在 %s…／做镜像时 %s…）" % (
                f["target"].name, cur[:12], str(f.get("orig_sha256"))[:12]))
    return problems


def cmd_check(a) -> int:
    anchor = Path(a.targets[0]).resolve()
    j, err = load_journal(journal_for(anchor))
    if not j:
        print("× " + err)
        return 4
    if j.get("state") == "committed":
        print("× 这次切换已经接进去了（state=committed）。要撤回用 rollback。")
        return 4
    problems = _validate(j, a.force)
    if problems:
        print("× 不能切，%d 个问题：" % len(problems))
        for p in problems:
            print("    - " + p)
        print("  接进去会把这些改动一起丢掉 —— 也就是「塞回去的那个器官，已经不是原来那个了」。")
        print("  出路：① 先把那些改动并进镜像；② 放弃这次切换（rollback）；")
        print("        ③ 确实知道自己在干什么，用 commit --force（会被记录）")
        return 2
    print("✓ 可以切：%d 个文件的原件都没动过" % len(j["files"]))
    return 0


# ───────────────────── commit ─────────────────────

def cmd_commit(a) -> int:
    anchor = Path(a.targets[0]).resolve()
    jp = journal_for(anchor)
    j, err = load_journal(jp)
    if not j:
        print("× " + err)
        print("  没有镜像就没有退路，不许切。")
        return 4
    if j.get("state") == "committed":
        print("× 已经接进去过了（state=committed）。要重来先 rollback。")
        return 2
    if j.get("state") == "rollingback":
        print("× 这一组正在回滚（state=rollingback）。先跑 recover 收干净。")
        return 2

    # ── 阶段一：全验 ──
    problems = _validate(j, a.force)
    if problems and not a.force:
        print("× 不能切，%d 个问题：" % len(problems))
        for p in problems:
            print("    - " + p)
        print("  接进去会把这些改动一起丢掉 —— 也就是「塞回去的那个器官，已经不是原来那个了」。")
        print("  出路：① 先把那些改动并进镜像；② 放弃这次切换（rollback）；")
        print("        ③ 确实知道自己在干什么，用 commit --force（会被记录）")
        return 2
    forced = bool(problems)
    if forced:
        print("！ 强制切换（%d 个文件的原件被改过，镜像是旧的）—— 会记进 journal" % len(problems))

    # ── 阶段二：逐个 rename，**每步落盘** ──
    j["state"] = "committing"
    j["forced"] = forced
    save_journal(jp, j)

    todo = [f for f in _abs(j) if f.get("state") != "done"]
    if len(todo) < len(j["files"]):
        print("  续做：已经有 %d 个切好了，接着切剩下的 %d 个" % (
            len(j["files"]) - len(todo), len(todo)))

    for i, f in enumerate(todo, 1):
        t, b, s = Path(f["target"]), Path(f["bak"]), Path(f["stage"])
        if b.exists():
            print("× 第 %d 个（%s）已经有 .bak 了 —— 上一次的上一步没清。先 recover。" % (i, t.name))
            return 2
        try:
            os.replace(t, b)                 # 原件 → 退路
            os.replace(s, t)                 # 镜像 → 原位
        except OSError as e:
            print("× 第 %d 个（%s）失败：%s" % (i, t.name, e))
            print()
            print("  现在这组是**半切状态**：前面 %d 个已经接进去了，这个没成。" % (i - 1))
            print("  推回全旧的唯一正确动作是：`swap.py recover %s`" % Path(jp).parent)
            print("  （recover 不看 state 只认 .bak，逆序塞回，不会问你）")
            return 3
        # ★ 原件刚被换掉 ⇒ 如果它在免扫名单里，把签名跟上（2026-10-27）。
        #   不续签的话，agent 走镜像这条路改完代码，自己跑验收还是会被拦 ——
        #   第五轮实测就卡在这里：改了、落了盘，却被说"内容和签过的不一样"。
        try:
            from loop.ruleedit import resign_after_write as _resign
            _r = _resign(t)
            if _r and _r.get("ok"):
                print("   ↻ 已续签免扫：%s" % t.name)
        except Exception as _e:                # noqa: BLE001
            print("   ！续签失败（不影响落盘）：%s" % _e)
        for raw in j["files"]:
            if Path(raw["target"]).resolve() == t:
                raw["state"] = "done"
                raw["bak_sha256"] = sha(b)
        save_journal(jp, j)                  # ★ 每切一个就落盘

    j["state"] = "committed"
    j["committed_at"] = now()
    save_journal(jp, j)
    print("✓ 整组接进去了：%d 个文件" % len(j["files"]))
    print("  退路都还在（*.bak）。")
    print("  ① 下游验过、你满意 ⇒ 跑 `swap.py tidy %s` 收尾（清掉 .bak / .stage / journal）"
          % a.targets[0])
    print("  ② 不满意 ⇒ swap.py rollback %s（整组一起塞回）" % a.targets[0])
    return 0


# ───────────────────── tidy ─────────────────────
#
# ★ 为什么需要它（2026-10-31 实测）：commit 之后 .bak 与 journal 是**故意保留**的
#   （人确认满意之前唯一的退路），但 agent 干活的目录里没有"人"会去手工删 ——
#   于是每交付一个工程，旁边就躺着一堆 `.bak` / `.stage` / `*.swap.json`。
#   实测那次留下 15 个：9 个 .bak、3 个 .stage、4 个 journal，全在交付目录里。
#   而没有这个命令时，模型会把这些副本**改写成一行说明**（平台不给删除工具），
#   垃圾照样在，还多花几次写盘。
#   范围严格限定：只删 swap 自己生成的三个位置，**绝不碰原件**；
#   而且只在整组已经 commit 之后才允许（没 commit 就删等于把退路烧了）。

def cmd_tidy(a) -> int:
    removed, skipped = 0, []
    for t in a.targets:
        anchor = Path(t).resolve()
        jp = journal_for(anchor)
        j, _err = load_journal(jp)
        if j:
            state = j.get("state")
            if state != "committed":
                skipped.append("%s（state=%s）" % (anchor.name, state))
                continue
            files = _abs(j)
        else:
            files = [fslots(anchor)]
        for f in files:
            for k in ("bak", "stage"):
                p = Path(f[k])
                if p.exists():
                    p.unlink(missing_ok=True)
                    removed += 1
        if jp.exists():
            jp.unlink(missing_ok=True)
            removed += 1
    if skipped:
        print("× 这些还没 commit，不能清（清了就没有退路了）：%s" % "、".join(skipped))
        return 5
    print("✓ 清掉 %d 个镜像中间文件（原件一个都没动）" % removed)
    return 0


# ───────────────────── rollback ─────────────────────

def cmd_rollback(a) -> int:
    anchor = Path(a.targets[0]).resolve()
    jp = journal_for(anchor)
    j, err = load_journal(jp)

    if not j:
        # 没有 journal，但可能还有 .bak（journal 被误删/手工操作过）
        s = fslots(anchor)
        if s["bak"].exists():
            os.replace(s["bak"], s["target"])
            s["stage"].unlink(missing_ok=True)
            print("✓ 没有 journal，但退路在 —— 单文件塞回去了：%s" % s["target"].name)
            return 0
        print("× " + err)
        return 4

    j["state"] = "rollingback"
    save_journal(jp, j)

    # ★ 逆序，且**只认 .bak 不看 state**：
    #   半途崩了的时候 state 可能是 staged，但原件已经在 .bak 里了（d60 那个场景）。
    done = 0
    for f in reversed(_abs(j)):
        b, t = Path(f["bak"]), Path(f["target"])
        if b.exists():
            os.replace(b, t)
            done += 1
        Path(f["stage"]).unlink(missing_ok=True)
        for raw in j["files"]:
            if Path(raw["target"]).resolve() == t:
                raw["state"] = "reverted"
        save_journal(jp, j)

    jp.unlink(missing_ok=True)
    if done:
        print("✓ 原器官塞回去了：%d 个文件（整组逆序）" % done)
    else:
        print("✓ 这次切换取消了（还没接进去，原件没动过）。镜像已删。")
    print("  要再试一次，重新 stage。")
    return 0


# ───────────────────── recover ─────────────────────

def cmd_recover(a) -> int:
    d = Path(a.dir)
    jps = sorted(d.glob("*.swap.json"))
    if not jps:
        print("（%s 下没有进行中的切换）" % d)
        return 0
    bad = 0
    for jp in jps:
        j, err = load_journal(jp)
        if not j:
            print("× %s：%s" % (jp.name, err))
            bad += 1
            continue
        st = j.get("state")
        n = len(j["files"])
        if st == "staged":
            for f in _abs(j):
                Path(f["stage"]).unlink(missing_ok=True)
            jp.unlink(missing_ok=True)
            print("· %s（%d 个文件）还没开始切 ⇒ 取消，原件没动过" % (jp.name, n))
        elif st in ("committing", "rollingback", "reverting"):
            # 可能切了一半：逆序把 .bak 塞回去，回到全旧
            k = 0
            for f in reversed(_abs(j)):
                if Path(f["bak"]).exists():
                    os.replace(Path(f["bak"]), Path(f["target"]))
                    k += 1
                Path(f["stage"]).unlink(missing_ok=True)
            jp.unlink(missing_ok=True)
            print("· %s（%d 个文件）停在 %s ⇒ 已逆序塞回 %d 个，回到全旧" % (jp.name, n, st, k))
        elif st == "committed":
            print("· %s（%d 个文件）已经整组接进去了 ⇒ **不动**。要撤请 rollback。" % (jp.name, n))
        else:
            print("× %s：不认识的状态 %r，不敢乱动" % (jp.name, st))
            bad += 1
    return 0 if bad == 0 else 2


# ───────────────────── status ─────────────────────

def cmd_status(a) -> int:
    d = Path(a.dir or ".")
    metas = sorted(d.glob("*.swap.json"))
    if not metas:
        print("（%s 下没有进行中的切换）" % d)
        return 0
    print("%-30s %-11s %-6s %-20s %s" % ("锚文件", "状态", "文件数", "开始时间", "备注"))
    for p in metas:
        j, err = load_journal(p)
        if not j:
            print("%-30s %s" % (p.name, err))
            continue
        print("%-30s %-11s %-6s %-20s %s" % (
            Path(j.get("anchor") or "?").name, j.get("state"), len(j["files"]),
            j.get("staged_at"), (j.get("note") or "")[:28]))
    return 0


# ───────────────────── 入口 ─────────────────────

class _AP(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("用法错：%s\n" % message)
        raise SystemExit(4)


def main() -> int:
    ap = _AP(description="在镜像里改，验过再接回去。一次可以是一组文件；接回去前会核漂移。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stage", help="给一组文件做镜像（之后只改 .stage）")
    p.add_argument("targets", nargs="+", help="第一个是锚：journal 挂在它旁边")
    p.add_argument("--note", default="", help="这一组打算改什么")
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("check", help="镜像还在吗、原件漂移了吗（逐个查）")
    p.add_argument("targets", nargs="+")
    p.add_argument("--force", action="store_true", help="只是让 check 别被漂移挡住（不切）")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("commit", help="两阶段接入：全验 → 逐个切，每步落盘")
    p.add_argument("targets", nargs="+")
    p.add_argument("--force", action="store_true",
                   help="原件漂移了也硬切（会记进 journal）")
    p.set_defaults(fn=cmd_commit)

    p = sub.add_parser("rollback", help="整组逆序塞回（只认 .bak，不看 state）")
    p.add_argument("targets", nargs="+")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("tidy", help="收尾：清掉已 commit 的 .bak / .stage / journal（原件不动）")
    p.add_argument("targets", nargs="+", help="任一组员即可，写锚文件最省事")
    p.set_defaults(fn=cmd_tidy)

    p = sub.add_parser("recover", help="把目录里没结束的切换推回一致状态")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(fn=cmd_recover)

    p = sub.add_parser("status", help="看这个目录下有哪些进行中的切换")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
