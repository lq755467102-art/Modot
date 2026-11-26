#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""contract.py —— 开工前把"什么算完成"写成文件，并锁住

为什么要有这个（spec §4.1 / research 修正 2）
    Anthropic 的 sprint contract 是这套东西里我们唯一**完全缺**的一环：
    动手之前，做事的人和验收的人先谈定"这一轮什么算完成"。
    没有它，本我审出来的东西没有参照 —— 它只能问"你说得对吗"，
    问不出"你说的够不够格叫完成"。**没有参照的反驳只是骂街。**

它防的具体失败模式
    Anthropic 的观察：上下文快满时，模型会悄悄把"完成"的标准调低。
    ⇒ `done.json` 一旦能被模型改，这条防线等于没有。
    ⇒ 所以这里不放一句"请勿修改"的注释，放一把锁：
      **改得动，但改完会红。** 红线不是纪律，是哈希。

三个文件（放在同一轮的目录里，比如 `runs/round-01/`）
    done.json   契约本身（人写，或人确认后由 new 起草）
    done.lock   锁定时的 sha256 + 时间 + 机器名
    done.log    amend 的流水（谁、什么时候、因为什么改的）

五条规矩（每条都对应一次踩过的坑）
    1. **`check` 必须是"退出码就是判据"的命令。**
       判据只写在 `pass` 里、命令却永远是 0 的，跑出来的 rc 什么也说明不了。
       ⇒ 所以 `run` 只报 rc，**不打印 PASS/FAIL**。不假装判过了。
    2. **改契约必须走 amend 并且给理由。** 不是防人，是留痕。
    3. **没锁的契约不算契约。** verify 直接判 2。
    4. **run 之前先 verify。** 被改过的契约，跑出来的结论不可信。
    5. **本我可以跑 run，不能 amend。** 前者只读，后者改世界。

退出码
    0 通过 / 2 契约不可信（没锁、被改过、或想蒙混重新 lock）/
    3 有 check 命令 rc≠0 / 4 用法或文件错
"""
# 主要引用或加载入口包括 `harness_core.py`。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TIMEOUT = 300

# ── 不可逆动作清单：**挪到 loop/policy.py 了**，这里 import 回来 ──
# 契约检查和运行时的门禁要用同一张表。各存一份就是第二个真源，
# 而"两处判据不一致"的后果是：契约挡住了、运行时放过了（或者反过来）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loop.policy import DANGER                       # noqa: E402


# ───────────────────── 基础 ─────────────────────

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def who() -> str:
    u = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    return "%s@%s" % (u, platform.node())


def load(d: Path):
    """读契约。返回 (obj, err)。"""
    p = d / "done.json"
    if not p.exists():
        return None, "这里没有 done.json：%s" % p
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return None, "done.json 不是合法 JSON：%s" % e
    if not isinstance(obj.get("done"), list):
        return None, "done.json 缺 done 数组"
    return obj, ""


def digest(obj) -> str:
    """语义哈希 —— 只认内容，不认格式。

    ★ 为什么不直接哈希文件字节：
      人在编辑器里打开保存一次，行尾从 LF 变 CRLF，字节就变了。
      那会让"什么都没改"也报红，红几次人就不看了。
      改成按规范化 JSON 算：改一个标点里的字会红，重排版不会红。
    """
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def pick_shell() -> str | None:
    """找一个能用的 shell；找不到返回 None（**不要拿 None 去跑命令**）。

    ★ 为什么不能只靠 shutil.which：
      这台机器上"有没有 bash"取决于**谁在哪个 shell 里启动验收** ——
      Git Bash 里有，PowerShell / cmd 里没有。于是同一份代码、同一个仓库，
      两个 agent 跑出 818/818 和 801/804。**把"能不能验收"交给宿主 PATH，
      等于验收的结论不由代码决定。**

    ★ 三档兜底，顺序是有意的：
      ① PATH（正常情况，最快的那个就是对的）
      ② 从**已经在 PATH 里的 git** 反推 —— 这台机器上 `which bash` 命中的是
         WorkBuddy 自带的 PortableGit，别的入口 PATH 里没有；但 git 在，
         它旁边就有 bash。这不是猜安装位置，是从已经存在的东西推。
      ③ 几个常见安装路径（给没装 git 但装了 Git for Windows 的机器）
    """
    for c in ("bash", "sh"):
        w = shutil.which(c)
        if w:
            return w

    g = shutil.which("git")
    if g:
        try:
            d = Path(g).resolve().parent          # .../cmd 或 .../bin
        except OSError:
            d = None
        if d is not None:
            for cand in (d.parent / "bin" / "bash.exe", d / "bash.exe",
                         d.parent / "usr" / "bin" / "bash.exe",
                         d.parent / "bin" / "sh.exe"):
                try:
                    if cand.exists():
                        return str(cand)
                except OSError:
                    pass

    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe",
                 r"C:\Program Files (x86)\Git\bin\bash.exe",
                 "/bin/bash", "/usr/bin/bash", "/bin/sh"):
        try:
            if Path(cand).exists():
                return cand
        except OSError:
            pass
    return None


def _shell() -> str | None:
    # 保留这个名字：几处老调用方（run_cmd / 判据体检）用的还是它。
    # 逻辑只有一份，在 pick_shell 里。
    return pick_shell()


def normalize_cmd(cmd: str) -> str:
    """把命令开头的 `python` / `python3` 换成本机解释器的**全路径**。

    ★ 为什么必须做：判据是模型写的，几乎都写 `python -c "..."`。
      而这台机器上 `python` 有两种坏法 ——
        ① Windows 商店 alias：命中了但 rc=9009；
        ② 干脆不在 PATH 里（换一个 shell 启动就没了）：rc=127。
      两种都不是"判据写错了"，却都会被记成判据失败。
      ⇒ 换成 `sys.executable` 之后，判据的结果只取决于它自己。
    ★ 只换**开头**那个：命令中间的 `| python` 或字符串里的 python 不动 ——
      那可能是被测对象的一部分，换了就改变了判据的语义。
    """
    t = (cmd or "").lstrip()
    for pre in ("python3 ", "python "):
        if t.startswith(pre):
            return '"%s" ' % sys.executable + t[len(pre):]
    return cmd


def run_cmd(cmd: str, cwd=None, timeout: int | None = None) -> dict:
    """跑一条 check 命令。

    沿用 audit.py 踩出来的两条 Windows 经验：
      ① `shell=True` 在 Windows 走 cmd.exe，而 cmd 不把单引号当引号
        ⇒ 优先用 bash，引号和管道才按作者的意思走。
      ② 输出编码 GBK / UTF-8 混着，先按 UTF-8 解，替换字符多了再按 GBK 重解。
    """
    cmd = re.sub(r"^python3(?=\s)","python",str(cmd).lstrip()) if os.name=="nt" else normalize_cmd(cmd)
    sh = _shell()
    # cwd=None 时行为完全不变（老调用方不受影响）。
    # 传了一个目录 ⇒ 这条命令在那个目录里跑 —— 判据里的相对路径就指向那里。
    kw = {} if cwd is None else {"cwd": str(cwd)}
    # 判据体检要跑四遍同一条命令，所以它需要自己的（更短的）超时。
    # 不传就用模块默认 TIMEOUT —— 老调用方行为完全不变。
    _to = TIMEOUT if timeout is None else int(timeout)
    try:
        from loop import safe_executor
        argv = [os.environ.get("COMSPEC","cmd.exe"),"/d","/s","/c",cmd] if os.name=="nt" else [sh or "/bin/sh","-c",cmd]
        # Contract predicates are untrusted model-authored code. They remain
        # isolated even if the surrounding application's mode changes.
        r = safe_executor.run(argv,timeout=_to,isolated=True,**kw)
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": "（超时 %d 秒）" % TIMEOUT}
    except FileNotFoundError as e:
        return {"rc": 127, "out": "（跑不起来：%s）" % e}
    raw_out = r.stdout or b""
    raw_err = r.stderr or b""

    def _dec(b):
        s = b.decode("utf-8", "replace")
        return b.decode("gbk", "replace") if s.count("\ufffd") > 8 else s

    return {"rc": r.returncode, "out": _dec(raw_out).strip()[-1200:],
            "err": _dec(raw_err).strip()[-600:]}


# ───────────────────── 五个动作 ─────────────────────

def cmd_new(a) -> int:
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "done.json"
    if p.exists() and not a.force:
        print("× 已经有 done.json 了。要改请用 amend（要留痕）；确实要推倒重来加 --force")
        return 2
    # ★★ 推倒重来必须连旧锁一起清（实测踩到）：
    #   `new --force` 只覆盖 done.json，旧的 done.lock 还留着旧哈希 ⇒
    #   **新契约一出生就被判「被改过」**，之后所有 verify 都是 rc=2。
    #   这个形状特别坑：你不知道该怀疑新契约写错了，还是锁坏了。
    if p.exists() and a.force:
        _lk = d / "done.lock"
        if _lk.exists():
            _lk.unlink()
            print("  旧的锁一并清掉了（推倒重来不是「改」，不该留旧锁）")
    # ── task.md：一个文件说清做什么 / 什么算完 / 不许做什么 ──
    # ★ 和 --task/--done 互斥，不是懒，是因为两处描述同一件事迟早不一致，
    #   而不一致的时候没有任何检查器能发现——每份单看都自洽。
    task_path = Path(a.from_task).resolve() if a.from_task else None
    tf: dict[str, list[str]] = {}
    tf_body = ""
    if task_path is not None:
        if a.task or a.done:
            print("× --from-task 和 --task/--done 不能一起用：同一件事只留一处描述")
            return 4
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from taskfile import body_sha as _tf_sha, check as _tf_check
        _tf_rc, _tf_msgs, tf, tf_body = _tf_check(task_path)
        if _tf_rc != 0:
            for _m in _tf_msgs:
                print("× " + _m)
            print("  先改 task.md：%s" % task_path)
            return 2
        raw_done = tf.get("done") or []
    else:
        raw_done = a.done

    # ── 采访制度 ──
    # 为什么放在写文件之前：契约一旦落盘，后面所有 verify/run 都拿它当参照。
    # 出处对不上的契约不该有机会落盘 —— 落盘了就得靠 amend 去修，而 amend 是留痕，
    # 会让"这份契约本来就没想清楚"变成一条看起来像"后来改了主意"的记录。
    iv_path = Path(a.from_interview).resolve() if a.from_interview else None
    iv_meta: dict[str, str] = {}
    iv_anchors: dict[str, int] = {}
    if iv_path is not None:
        if not iv_path.exists():
            print("× --from-interview 指的 %s 不存在" % iv_path)
            return 4
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from interview import parse as _iv_parse
        iv_meta, iv_anchors, _iv_err = _iv_parse(iv_path)
        if _iv_err:
            print("× 采访记录不合格：%s" % _iv_err)
            print("  先把它弄对：python bin/interview.py check %s" % iv_path)
            return 2

    items = []
    for i, raw in enumerate(raw_done, 1):
        parts = [x.strip() for x in raw.split("::")]
        if len(parts) < 2 or not parts[1]:
            print("× --done 要至少两段：claim::check[::pass]（第 %d 条只给了 %d 段）" % (i, len(parts)))
            print('  例：--done "既有验收没被破坏::python bin/accept.py::127/127 通过"')
            return 4
        items.append({
            "id": "r%d" % i,
            "claim": parts[0],
            "check": parts[1],
            "pass": parts[2] if len(parts) > 2 and parts[2] else "退出码 0",
            "ref": parts[3] if len(parts) > 3 else "",
        })

    # ── 不可逆动作无条件要采访 ──
    # ★ 必须排在出处校验**前面**：这是语义层的判断，说出了"你的访谈结论和契约打架"；
    #   出处是结构层的，说"你漏写了引用"。先报结构会把人引到补引用上，
    #   补完还是过不了，因为根子上是判断错了。报错指错方向比不报还费时间。
    _blob = " ".join("%s %s %s" % (x["claim"], x["check"], x["pass"]) for x in items)
    _hit = sorted({w for w in DANGER if w in _blob})
    if _hit:
        if iv_path is None:
            print("× 这几条提到不可逆的动作：%s" % "、".join(_hit))
            print("  不可逆的不许跳过采访。先跑：")
            print("    python bin/interview.py new --out <路径> --irreversible yes")
            print("    填完 Q/A 再回来，带上 --from-interview <路径>")
            return 2
        if (iv_meta.get("irreversible") or "").lower() == "no":
            print("× 采访记录说 irreversible: no，但契约里出现了：%s" % "、".join(_hit))
            print("  两者矛盾：要么采访判断错了，要么这几条写错了。先改再锁。")
            return 2

    # ── 出处校验：只有真的采访过，才要求每条指得回去 ──
    # ★ 采访过 = 有 Q 或有反向问。**跳过采访的那种记录没有锚点，不该逼它编引用。**
    #   第一版没加这个条件，于是"跳过采访"的文件必然过不了 ——
    #   等于把「只在歧义代价高时才采访」变成「每次都得采访」，正好违背原来的约束。
    _interviewed = any(k.startswith("q") or k == "counter" for k in iv_anchors)
    if iv_path is not None and _interviewed:
        for it in items:
            if not it["ref"]:
                print("× %s 没写出处。有采访记录时每条 done 要带第 4 段，"
                      "形如 ...::q1（可用：%s）"
                      % (it["id"], "、".join(sorted(iv_anchors)) or "无"))
                return 2
            if it["ref"] not in iv_anchors:
                print("× %s 的出处 %r 在采访记录里找不到。现有锚点：%s"
                      % (it["id"], it["ref"], "、".join(sorted(iv_anchors)) or "无"))
                return 2
    if not items:
        items = [
            {"id": "r1", "claim": "（换成这一轮真正要验的事）",
             "check": "python bin/accept.py", "pass": "退出码 0，分母不掉"},
        ]
    # ★★ 「这次到底要改变什么行为」—— 一句话，写进契约（2026-10-28）。
    #   为什么要单独一个字段：本我（复核那个）现在只能从 diff **反推**意图，
    #   而反推出来的东西没法当判据 —— 它会把"改成了什么样"当成"本来要改成什么样"。
    #   有了这一行，它就能做"意图 vs 实际"的对照，而不是"我觉得哪里怪"。
    #   取法优先级：命令行显式给 > task 正文里的「意图 / intent」段 > 任务首行。
    _intent = (getattr(a, "intent", "") or "").strip()
    if not _intent:
        _m = re.search(r"(?mi)^\s*#{0,3}\s*(?:意图|intent)\s*[:：]?\s*(.+)$", tf_body or "")
        if _m:
            _intent = _m.group(1).strip()
    if not _intent:
        for _ln in (tf_body or a.task or "").splitlines():
            if _ln.strip():
                _intent = _ln.strip()
                break
    if len(_intent) > 200:
        _intent = _intent[:200] + "…"
    obj = {
        "task": tf_body.strip()[:2000] if task_path is not None
                else (a.task or "（写清这一轮要什么）"),
        # 一句话：这一轮要**改变什么行为**（不是"我要改哪个文件"）
        "intent": _intent,
        "note": "这份只能人改。改了必须走 contract.py amend 并写理由 —— 否则 verify 会红。",
        # ★ 记下正文的指纹。正文是需求本身，但它不参与 json 锁（改一个标点就换哈希太脆），
        #   所以单独算一个。改正文而不 amend ⇒ verify 会红 —— 否则
        #   "验收的还是旧标准，但需求已经变了"这种事没人能发现。
        "task_file": ({"path": str(task_path), "body_sha256": _tf_sha(tf_body)}
                      if task_path is not None else None),
        # ★ not_build 不是装饰。只有正面清单时，「顺手多做」没有任何东西挡得住，
        #   事后也验不出来——因为没人写下"本来不该做这个"。
        "not_build": [
            {"text": x.split("::")[0].strip(),
             "check": (x.split("::", 1)[1].strip() if "::" in x else "")}
            for x in (tf.get("not_build") or [])
        ],
        # ★ 记下这轮走没走采访。null 不是中性的：它意味着"没人问过"，
        #   事后能在轨迹里一眼看出哪些轮次是"我想到了就做"的。
        "interview": ({"path": str(iv_path),
                       "irreversible": (iv_meta.get("irreversible") or "")}
                      if iv_path is not None else None),
        "done": items,
    }
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("写了 %s（%d 条）" % (p, len(items)))
    print("下一步：人看一遍 → `contract.py lock --dir %s`。没锁的契约不算契约。" % a.dir)
    return 0


def _older_than(a: Path, b: Path) -> bool:
    """a 比 b 旧吗（b 改过之后，a 那份结果就过期了）。

    ★ 拿不到 mtime 时按"过期"算：**把过期的检查结果当成有效的**，
      比没有检查结果更坏 —— 人会以为自己查过了。
    """
    try:
        return a.stat().st_mtime < b.stat().st_mtime
    except OSError:
        return True


def _weak_judges(d: Path) -> list:
    """这份契约里被判过"没用"的判据（恒真 / 自指）。

    ★ 只在**锁**的时候用（2026-10-28）：起草完 `plan.py` 会跑一遍判据体检
      （把判据扔进"什么都没做"的空世界，看它还绿不绿），结果写进 `probe.json`。
      一条恒真的判据 = 不管做没做都绿 = 整份验收永远通过。
      L4 靠这份契约自转几十轮，那是**没有靶子还烧钱**。

    ★ 没有 probe.json 时返回空列表，并**不假装验过** —— 手工写的契约本来就不经过
      体检这一步，硬说"没问题"比不说更坏。
    """
    p = d / "probe.json"
    if not p.exists():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = obj.get("rows") or []
    return [r for r in rows
            if str(r.get("verdict") or "") in ("恒真", "自指", "自指·内容", "自指·存在")]


def cmd_lock(a) -> int:
    d = Path(a.dir)
    obj, err = load(d)
    if err:
        print("×", err)
        return 4

    # ★ 锁之前先看体检结果（2026-10-28）：锁 = 批准，批准前该知道哪几条根本判不了。
    weak = _weak_judges(d)
    if weak and not getattr(a, "allow_weak", False):
        print("× 这份契约里有 %d 条**判不了什么**的判据 —— 先看一眼再锁。" % len(weak))
        for r in weak[:6]:
            print("   · %s：%s" % (r.get("label") or r.get("id") or "?",
                                  r.get("why") or ""))
        print("")
        print("  它们的绿**不代表做完了**：把整个世界清空，它们照样绿。")
        print("  L4 靠这份契约自己跑几十轮 —— 恒真的判据等于没有靶子，而钱照烧。")
        print("")
        print("  三种改法：")
        print("    ① 改判据：contract.py amend --dir %s --reason \"...\"" % a.dir)
        print("    ② 删掉那几条：同上，amend 之后重跑 plan.py 起草")
        print("    ③ 我知道它是弱的，但还是要用：加 --allow-weak")
        return 2
    lk = d / "done.lock"
    h = digest(obj)
    if lk.exists():
        old = json.loads(lk.read_text(encoding="utf-8"))
        if old.get("sha256") != h:
            print("× 契约内容和锁里的不一样 —— 不许重新 lock 把改动蒙过去")
            print('  要改就明说：contract.py amend --dir %s --reason "为什么改"' % a.dir)
            return 2

    # ★ 锁的时候把「三问」摆出来（2026-10-28）：这是决策时刻 ——
    #   三个角色对这份契约各提了一句最尖的质疑，人在这里看到它们才有用。
    #   ★ 但**必须先判它是不是过期的**：契约改过之后，旧的三问说的是别的题目，
    #     把它当成"已经查过了"比没有更坏。
    _cc = d / "crosscheck.json"
    if _cc.exists():
        try:
            cc = json.loads(_cc.read_text(encoding="utf-8"))
            if _older_than(_cc, d / "done.json"):
                # ★ 过期就**不列内容**（2026-10-28）：列出来人一定会读，
                #   读了就会拿它判断 —— 而它说的是旧题目。宁可只说"过期了"。
                print("三问：**已过期**（契约在那之后改过）—— 下面这些质疑说的是旧题目，"
                      "这次不显示。要新的就重跑 bin/crosscheck.py")
                print("")
            else:
                print("三问（三个角色对这份契约的质疑）：")
                for it in (cc.get("items") or []):
                    if it.get("ok"):
                        print("  · [%s] %s" % (it.get("who"), str(it.get("ask"))[:110]))
                    else:
                        print("  · [%s] 这次没问到：%s"
                              % (it.get("who"), str(it.get("reason"))[:80]))
                print("  ⇒ 有哪条扎心，就 amend 完再锁。没有的话就锁。")
                print("")
        except Exception:
            pass

    lk.write_text(json.dumps({"sha256": h, "at": now(), "by": who(),
                              "count": len(obj["done"]),
                              # ★ 把"锁的时候知道有 M 条弱判据"记进锁里（2026-10-28）：
                              #   以后有人问"当初为什么锁这份",能看出他是知情还是不知道。
                              "weak": len(weak)},
                             ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("✓ 锁上了：%d 条，sha256 %s…" % (len(obj["done"]), h[:12]))
    # ★ 锁契约同时签发一张**可重校验**的授权（2026-11-01）。
    #   只锁不签，续跑时就没东西可回答"当初批准的是哪一份、范围到哪"。
    try:
        from loop import mandate as MD
        _mr = MD.issue(d.parent, strength="explicit", by=who())
        if _mr.get("ok"):
            print("  授权凭证：%s" % MD.describe(_mr["mandate"]))
        else:
            print("  ⚠ 授权凭证没签发：%s" % _mr.get("error"))
    except Exception as _e:
        print("  ⚠ 授权凭证签发失败：%s" % _e)
    if weak:
        print("  ⚠ 其中 %d 条是弱判据（你选了 --allow-weak）—— 这 %d 条绿了不代表做完了"
              % (len(weak), len(weak)))
    if not (d / "probe.json").exists():
        print("  （这份契约没跑过判据体检 —— 锁之前没验过它判不判得了）")
    return 0


def _verify(d: Path):
    """(rc, obj, msg)。rc: 0 绿 / 2 不可信 / 4 文件错。"""
    obj, err = load(d)
    if err:
        return 4, None, err
    lk = d / "done.lock"
    if not lk.exists():
        return 2, obj, "这份契约没锁过 —— 没锁的契约不算契约（先看一遍再 lock）"
    old = json.loads(lk.read_text(encoding="utf-8"))
    h = digest(obj)
    if h != old.get("sha256"):
        return 2, obj, ("契约被改过，且没走 amend\n"
                        "  锁里 %s… / 现在 %s…\n"
                        '  是人改的、理由成立 ⇒ amend --reason "..."\n'
                        "  没人认这笔改动 ⇒ **这一轮的完成判据不可信，别用**"
                        % (str(old.get("sha256"))[:12], h[:12]))
    # ★ 契约自己没动，不代表它还在描述同一个需求 —— 正文是需求本身。
    tf = obj.get("task_file") or {}
    if tf.get("path"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from taskfile import body_sha as _ts, parse as _tp
        _tm, _tb, _terr = _tp(Path(tf["path"]))
        if _terr:
            return 2, obj, ("契约声明的 task.md 读不出来了：%s\n"
                            "  需求源没了，这份契约的参照也就不在了" % _terr)
        if _ts(_tb) != tf.get("body_sha256"):
            return 2, obj, ("task.md 的正文被改过，但契约没跟着 amend\n"
                            "  正文就是需求本身：需求变了、判据没变 ⇒ 验收的还是旧标准\n"
                            '  确实要改 ⇒ amend --reason "..."；不是你改的 ⇒ 别用这份契约')
    return 0, obj, "契约未动（%d 条，锁于 %s，锁的人 %s）" % (
        len(obj["done"]), old.get("at"), old.get("by"))


def cmd_verify(a) -> int:
    rc, _, msg = _verify(Path(a.dir))
    print(("✓ " if rc == 0 else "× ") + msg)
    return rc


def cmd_run(a) -> int:
    d = Path(a.dir)
    rc, obj, msg = _verify(d)
    if rc != 0:
        print("× " + msg)
        print("（契约不可信 ⇒ 不跑。跑出来的 rc 说明不了任何事。）")
        return rc
    items = obj["done"]
    bad = 0
    # ★ 机读出口（2026-10-28）：`--result` 让上游（纪要里的规划者）能拿到
    #   **逐条**的通过情况，而不是从 stdout 里往回抠 `[r2] rc=3 …` 这种给人看的花样。
    #   ★ 默认不写：不给这个参数时行为一字不变，人看的输出也不动 ——
    #     改 stdout 格式等于把"人读的东西"和"机器读的东西"绑在一起，
    #     下次谁调一下措辞就把解析打碎了。
    _per: list = []
    print("契约：%s" % obj.get("task", ""))
    print()
    for it in items:
        t0 = time.time()
        r = run_cmd(it.get("check", ""))
        dt = time.time() - t0
        mark = "rc=0" if r["rc"] == 0 else "rc=%s" % r["rc"]
        _per.append({"n": len(_per) + 1, "id": it.get("id", "?"),
                     "rc": r["rc"], "ok": r["rc"] == 0,
                     "claim": str(it.get("claim", ""))[:200],
                     "pass": str(it.get("pass", ""))[:200]})
        print("[%-3s] %-6s %5.1fs  %s" % (it.get("id", "?"), mark, dt, it.get("claim", "")))
        print("        命令：%s" % it.get("check", ""))
        for line in (r["out"] or "").rstrip().splitlines()[-6:]:
            print("        │ " + line)
        # ★ stderr 单独摆出来。实测踩过：`grep -q 不走 Aider docs/research.md`
        #   —— 空格没转义，grep 去打开一个叫 "Aider" 的文件，报 No such file，
        #   但因为另一个参数恰好匹配上了，退出码是 0。**假绿。**
        if r.get("err"):
            # ★ 取**最后一行**（2026-10-27 自测逮到）：python 的报错第一行永远是
            #   `Traceback (most recent call last):`，真正有用的那句
            #   （AssertionError / FileNotFoundError …）在最后一行。
            #   取第一行等于只让人看见"它报错了"，看不见"报的什么错" ——
            #   我自己调这条契约时就是被这个卡住的，还得手跑一遍才知道原因。
            _ls = [x for x in r["err"].splitlines() if x.strip()]
            _show = (_ls[-1] if _ls else "")
            if _show.strip() == "Traceback (most recent call last):" and len(_ls) > 1:
                _show = _ls[-2]
            print("        ！它自己报错了：" + _show.strip()[:200])
            if r["rc"] == 0:
                print("        ！（rc 仍是 0 —— 这条判据可能是假的，先看这行再信它）")
        if r["rc"] != 0:
            bad += 1
            print("        └ 这条没通过。判据（原话）：%s" % it.get("pass", ""))

    # ── 不该做的，到底做了没有 ──
    # ★ 只有写了 check 的能自动验；只写一句话的**列出来算人核**，不假装验过。
    #   这一段的价值不在"验得多严"，在于「顺手多做」第一次有地方摆出来 ——
    #   在此之前，多做了根本无从发现，因为没人写下过"本来不该做这个"。
    nb = obj.get("not_build") or []
    nb_bad = 0
    if nb:
        print()
        print("不该做的（not_build）：")
        for it in nb:
            text = it.get("text", "")
            ck = it.get("check", "")
            if not ck:
                print("  [人核] %s" % text)
                continue
            # `!` 前缀表示"这条命令不成立才算没做"。自己剥，不依赖 shell 解释 ——
            # 不同 shell 对 `!` 的处理不一样，判据不能押在这上面。
            neg = ck.startswith("!")
            r = run_cmd(ck[1:].strip() if neg else ck)
            ok = (r["rc"] != 0) if neg else (r["rc"] == 0)
            print("  [%s] %s" % ("没做" if ok else "做了", text))
            print("        验证：%s" % ck)
            if not ok:
                nb_bad += 1

    print()
    print("%d/%d 条命令 rc=0%s" % (
        len(items) - bad, len(items),
        ("；not_build 违反了 %d 条" % nb_bad) if nb_bad else ""))
    # ★ 这句的措辞被验收反过来检查过：run 的输出里**不许出现 PASS/FAIL 字样**。
    #   所以这里只能说"判据栏"，不能说那个英文单词 —— 否则检查就没法机械判。
    print("★ rc=0 只说明这条命令自己认为成立。**判据有没有真的写进命令里**"
          "（而不是只写在「判据」那一栏），得人看 —— 这里不替你判。")

    # ★ 写机读结果：**写不进去也不改结论**。
    #   这是附带产物，磁盘只读之类的问题不该把"判据全过"变成一次失败。
    if getattr(a, "result", ""):
        try:
            Path(a.result).write_text(json.dumps({
                "total": len(items), "bad": bad, "nb_bad": nb_bad,
                "items": _per,
                "at": now(),
            }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        except OSError as e:
            print("（机读结果没写成：%s —— 不影响上面的结论）" % e, file=sys.stderr)
    return 0 if (bad == 0 and nb_bad == 0) else 3


def cmd_amend(a) -> int:
    if not a.reason or not a.reason.strip():
        print("× amend 必须给 --reason。没有理由的改动等于没有留痕。")
        return 4
    d = Path(a.dir)
    obj, err = load(d)
    if err:
        print("×", err)
        return 4
    _new_intent = (getattr(a, "intent", "") or "").strip()
    if _new_intent:
        obj["intent"] = _new_intent[:200]
        # ★ 必须**写回 done.json**（2026-10-28 自查抓到）：amend 原来的语义是
        #   "人手工改了 done.json，我来重新盖章" —— 它只更新 lock，不写正文。
        #   于是 `amend --intent` 只改了内存里的 obj，文件里一个字没变，
        #   而锁却按新内容算了 → 下一次 verify 认为"契约被改过但没 amend"。
        (d / "done.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lk = d / "done.lock"
    old = json.loads(lk.read_text(encoding="utf-8")) if lk.exists() else {}
    h = digest(obj)
    with open(d / "done.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now(), "by": who(), "reason": a.reason.strip(),
                            "from": old.get("sha256", ""), "to": h,
                            "count": len(obj["done"])}, ensure_ascii=False) + "\n")
    lk.write_text(json.dumps({"sha256": h, "at": now(), "by": who(),
                              "count": len(obj["done"])},
                             ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("✓ 改了，并留痕：%s" % a.reason.strip())
    print("  done.log：from %s… → to %s…" % (str(old.get("sha256", ""))[:12], h[:12]))
    return 0


# ───────────────────── 入口 ─────────────────────

class _AP(argparse.ArgumentParser):
    """★ argparse 出错默认 exit(2)，但 2 在这里的意思是"契约不可信"。
    两者撞了，调用方就没法区分"你参数写错了"和"这份契约不能信"。
    ⇒ 用法错统一改判 4。这是踩过的那种"拿一个数字当两件事用"。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("用法错：%s\n" % message)
        raise SystemExit(4)


def main() -> int:
    ap = _AP(
        description="开工前定契约、锁住、逐条跑。改契约只能走 amend。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="起草一份 done.json（人确认后再 lock）")
    p.add_argument("--dir", required=True)
    p.add_argument("--task", default="")
    p.add_argument("--intent", default="",
                   help="一句话：这一轮要改变什么行为（不写就从任务正文里取）")
    p.add_argument("--done", action="append", default=[],
                   help='claim::check[::pass]，可重复。例：--done "验收不掉::python bin/accept.py"')
    p.add_argument("--force", action="store_true", help="覆盖已有 done.json（会丢原契约）")
    p.add_argument("--from-interview", default="",
                   help="采访记录路径。给了它，每条 done 必须带第 4 段出处（::q1 / ::counter / ::r2）；"
                        "契约里提到不可逆动作时，必须给且 irreversible 不能是 no")
    p.add_argument("--from-task", default="",
                   help="task.md 路径（做什么 / 什么算完 / 不许做什么）。"
                        "给了它就别再用 --task/--done")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("lock", help="锁定当前契约（算 sha256）")
    p.add_argument("--dir", required=True)
    p.add_argument("--allow-weak", action="store_true",
                   help="契约里有恒真/自指判据时仍然锁（知情地接受）")
    p.set_defaults(fn=cmd_lock)

    p = sub.add_parser("verify", help="契约还是不是原来那份")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("run", help="先 verify，再逐条跑 check，只报 rc")
    p.add_argument("--dir", required=True)
    p.add_argument("--result", default="",
                   help="另存一份机读结果（逐条 rc）到这个路径；不给就不写")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("amend", help="人改契约：重锁 + 写 done.log")
    p.add_argument("--dir", required=True)
    p.add_argument("--reason", default="", help="为什么改（必给，否则 4）")
    p.add_argument("--intent", default="", help="顺带改这一轮的意图（一句话）")
    p.set_defaults(fn=cmd_amend)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
