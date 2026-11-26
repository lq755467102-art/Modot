#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_judge.py —— 判据体检：把判据扔进"什么都没做"的世界里，看它还绿不绿。

为什么需要它
    一条判据如果**不管世界怎样都绿**，那它什么都没核，只是让验收看起来过了。
    用户在新 agent 的复盘里点出过一例（r3）：判据要求「runs/ 下某个 json 里出现
    本我字样」—— 而 runs/ 下全是会话记录，契约文本自己里就写着"本我"。
    所以这条从第一天起就是绿的，和这一轮做没做无关。

它凭什么判断
    判据命令用的是**相对路径**（`pathlib.Path('runs')`、`'bin'`），
    所以换一个 cwd，它检查的就变成那个目录 —— **真仓库一个字不用动**。
    这是关键：不用"改真文件再恢复"，那种做法最坏会留下一个坏仓库，
    而它的目的本来是为了证明别的测试可靠。

三个世界，三种结论
    ① 真实仓库     —— 判据现在的真实结果
    ② 完全空目录   —— 连契约都没有。这里还绿 ⇒ **绝对恒真**，跟什么都无关
    ③ 只有契约     —— 空目录 + 把契约放在它在仓库里的原位置。
                      这里绿（②红）⇒ **被契约自己满足**，一样是废判据
    ②③ 都红 ⇒ 它至少依赖某些产出（不能证明它有效，但能证明它不是恒真）

用法
    python bin/probe_judge.py                    # 最近一个会话
    python bin/probe_judge.py --dir runs/xxx     # 指定会话
    python bin/probe_judge.py --json             # 给界面用
"""
# 主要引用或加载入口包括 `harness_core.py`。
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT))

from contract import run_cmd, normalize_cmd          # noqa: E402

TAGS = {"real": "真实", "void": "空世界", "self": "只有契约"}


class _TooSlow(Exception):
    """判据太慢，只做了真实世界那一次。"""


# ── 人看的称呼：编号进不了人眼，只有"它在核什么"有用 ──
_TALK = {
    "恒真": "永远绿",
    "自指·内容": "被自己的话满足",
    "自指·存在": "文件在就算过",
    "有依赖": "能分开做没做",
    "未通过": "本来就是红的",
    "太慢·略过": "太慢，没查",
    "空判据": "没有检查命令",
}


def _say(verdict: str) -> str:
    """判定词的人话版。术语留在 `verdict` 字段里给机器看。"""
    return _TALK.get(verdict, verdict)


def label_of(it: dict) -> str:
    """一条判据的"它在核什么"。优先人改过的名字，否则从原文提炼。

    提炼只做三件事，不重写：
      ① 切掉后半句的套话（"，回复对…""，说明…"）
      ② 去掉开头"仓库里确实存在"这类空话
      ③ 太长就在介词处断开
    ★ 不重写是刻意的：重写会失真，而判据失真比难读严重得多。
    """
    lab = str(it.get("label") or "").strip()
    if lab:
        return lab[:22]
    claim = str(it.get("claim") or "").strip()
    if not claim:
        return ""
    claim = re.split(r"[，,]\s*(回复|答复|说明|以证明|用来证明|证明)", claim)[0].strip()
    claim = re.sub(r"^(仓库里|仓库中|项目里)?(确实|的确)?(存在|有|能找到)",
                   "", claim).strip("：:，, ")
    if len(claim) > 20:
        m = re.search(r"^.{6,20}?(?=落在|进入|写进|出现在|被|）)", claim)
        claim = (m.group(0) if m else claim[:20]) + "…"
    return claim


def _labels_file(sess_dir: Path) -> Path:
    """人改过的名字放这儿 —— 不动契约本身（契约有哈希锁，改了要走 amend）。"""
    return sess_dir / "judge_labels.json"


def _load_labels(sess_dir: Path) -> dict:
    f = _labels_file(sess_dir)
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pick_dir(d: str | None) -> Path:
    if d:
        return Path(d).resolve()
    runs = ROOT / "runs"
    cands = [p for p in runs.glob("*") if (p / "done.json").exists()]
    if not cands:
        raise SystemExit("✗ runs/ 下没有任何 done.json")
    return max(cands, key=lambda p: p.stat().st_mtime)


def _fix_py(cmd: str) -> tuple[str, bool]:
    """把命令开头的 `python ` 换成本机解释器全路径。

    为什么：这台机器上 `python` 会命中 Windows 商店的 alias（rc=9009），
    而判据是模型写的、几乎都写 `python -c`。不换的话每条都会"跑不起来"，
    那是环境问题，不是判据的问题 —— 两件事不能混在一起报。
    """
    # 逻辑只有一份，在 contract.normalize_cmd 里（两处各写一遍必漂）
    out = normalize_cmd(cmd)
    return out, out != cmd


def make_world(tag: str, contract: Path) -> Path:
    """造一个尽可能空的世界。

    三种：
      void  —— 完全空（连目录都没有）
      self  —— 空 + 契约原件（放在它在仓库里的原位置）
      blank —— 空 + 契约**换成无关的占位内容**
    后两个的差别就是"内容满足"还是"存在满足"。
    """
    d = Path(tempfile.mkdtemp(prefix="judge-%s-" % tag))
    if tag in ("self", "blank"):
        # 判据如果扫 runs/，它看到的就是这一份 —— 这正是 r3 露馅的地方。
        try:
            rel = contract.relative_to(ROOT)
        except ValueError:
            rel = Path(contract.name)
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if tag == "self":
            shutil.copy2(contract, target)
        else:
            target.write_text(
                '{"note": "（判据体检：这个文件被故意换成无关内容，'
                '用来判断判据是在核内容还是只核文件在不在）"}',
                encoding="utf-8")
    return d


def probe_one(it: dict, contract: Path, timeout_note: list,
              labels: dict | None = None) -> dict:
    _lab = ((labels or {}).get(str(it.get("id") or "")) or "").strip()
    _name = label_of({"label": _lab, "claim": it.get("claim")}) if _lab \
        else label_of(it)
    raw = str(it.get("check") or "").strip()
    if not raw:
        return {"id": it.get("id"), "label": _name, "verdict": "空判据",
                "verdict_say": _say("空判据"), "why": "这条没有 check 命令"}
    cmd, swapped = _fix_py(raw)
    if swapped:
        timeout_note.append(True)

    # 单次 45 秒。验收那种 95 秒的判据跑四遍就是六分钟 ——
    # 那比不做体检还糟（它会把一轮拖死），所以慢判据只跑真实世界。
    ONE = 45
    import time as _t
    _t0 = _t.time()
    real = run_cmd(cmd, cwd=ROOT, timeout=ONE)
    _slow = (_t.time() - _t0) > ONE * 0.9
    worlds = {}
    tmps = []
    try:
        if _slow:
            raise _TooSlow()
        for tag in ("void", "self", "blank"):
            w = make_world(tag, contract)
            tmps.append(w)
            r = run_cmd(cmd, cwd=w, timeout=ONE)
            worlds[tag] = {"rc": r.get("rc"),
                           "out": str(r.get("out") or "")[:400]}
    except _TooSlow:
        return {"id": it.get("id"), "label": _name,
                "claim": str(it.get("claim") or "")[:80],
                "verdict": "太慢·略过", "verdict_say": _say("太慢·略过"), "rc": {},
                "why": "这条判据跑一次就接近 45 秒，跑四遍会拖死一轮；体检略过",
                "pass_note": str(it.get("pass") or "")[:100], "how": ""}
    finally:
        for w in tmps:
            shutil.rmtree(w, ignore_errors=True)

    rc_real = real.get("rc")
    rc_void = worlds["void"]["rc"]
    rc_self = worlds["self"]["rc"]
    rc_blank = worlds["blank"]["rc"]

    # 判定：绿 = rc == 0
    if rc_void == 0:
        verdict = "恒真"
        why = "连空目录都能过 —— 它跟这一轮做了什么无关"
    elif rc_self == 0:
        if rc_blank == 0:
            verdict = "自指·存在"
            why = ("只要那份文件在那儿就绿（内容换成无关文字也绿）"
                   "—— 它核的是「文件在不在」，不是「里面有没有真东西」")
        else:
            verdict = "自指·内容"
            why = ("被契约的正文本身满足了：换成无关内容就红，"
                   "说明它扫到的正是我们自己的契约话")
            if "runs" in raw:
                why += "（它扫 runs/，而契约就在 runs/ 下）"
    elif rc_real == 0:
        verdict = "有依赖"
        why = "空目录里不过、真实仓库里过 —— 至少能分开「有产出」和「没产出」"
    else:
        verdict = "未通过"
        why = "真实仓库里也没过（这一轮本来就没过，或者这条判据根本跑不起来）"

    return {
        "id": it.get("id"),
        "label": _name,
        "verdict_say": _say(verdict),
        "claim": str(it.get("claim") or "")[:80],
        "verdict": verdict,
        "why": why,
        "rc": {"real": rc_real, "void": rc_void,
               "self": rc_self, "blank": rc_blank},
        "pass_note": str(it.get("pass") or "")[:100],
        "how": ("python 换成本机解释器全路径" if swapped else ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="判据体检：造一个什么都没做的世界，看判据还绿不绿")
    ap.add_argument("--dir", default="", help="会话目录（默认最近一个有 done.json 的）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给界面用）")
    a = ap.parse_args()

    d = _pick_dir(a.dir or None)
    contract = d / "done.json"
    data = json.loads(contract.read_text(encoding="utf-8"))
    items = data.get("done") or []
    if not items:
        print("这个契约里没有判据")
        return 0

    notes: list = []
    _labels = _load_labels(d)
    rows = [probe_one(it, contract, notes, _labels) for it in items]
    bad = [r for r in rows
           if r["verdict"] in ("恒真", "自指", "自指·内容", "自指·存在")]

    if a.json:
        print(json.dumps({"dir": str(d), "rows": rows,
                          "bad": len(bad), "total": len(rows)},
                         ensure_ascii=False, indent=1))
        return 1 if bad else 0

    print("判据体检 · %s" % contract.relative_to(ROOT))
    print("（四个世界：真实仓库 / 完全空的目录 / 只有契约 / 契约换成无关内容）\n")
    for r in rows:
        rc = r["rc"] or {}
        _nm = r.get("label") or r.get("id") or "?"
        print("%s\n     %s" % (_nm, _say(r["verdict"])))
        if not rc:
            print("     %s" % r["why"])
            print("")
            continue
        # 不写 rc 和 0/1 —— 那是机器话。人只关心"哪个世界里过没过"。
        _w = lambda v: "过" if v == 0 else ("没过" if v else "没跑")
        print("     真实仓库 %s · 空目录 %s · 只有契约 %s · 契约换内容 %s"
              % (_w(rc["real"]), _w(rc["void"]), _w(rc["self"]), _w(rc["blank"])))
        print("     %s" % r["why"])
        if r["claim"]:
            print("     原文：%s" % r["claim"])
        if r.get("id"):
            print("     编号：%s" % r["id"])
        print("")
    if bad:
        print("✗ %d 条判据无效（恒真 / 自指）：它们绿不代表做完了。" % len(bad))
        for r in bad:
            print("   - %s：%s" % (r.get("label") or r["id"], r["why"]))
    else:
        print("✓ 没有恒真判据（注意：这只说明它们不是恒真，不等于它们判得准）")
    if notes:
        print("\n★ 提示：判据里的 `python` 已换成本机解释器全路径（这台机器上 python 会命中商店 alias）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
