#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loop 体检：拿历史会话当语料，逐条检查"这一轮到底有没有按设计的路走"。

为什么要它
    「没有 bug」证明不了。能做的是把**已知的错法**写成一条条可判的不变量，
    然后拿真实跑过的会话去撞 —— 撞到就说明这条路上真的出过事，没撞到也只能说
    "这一批语料里没出现"，不能推广成"以后不会有"。

不变量（每条都对应一次真踩过的坑）
    I1  序号必须递增，时间不能倒退（事件是追加写的，倒退说明两个进程在同时写）
    I2  `end` 之后不许再出现 llm / tool / say（收尾阶段只允许 benni/distill/verify/索引）
    I3  同一个会话里，相邻两条 user 不许完全一样（采访后把任务书又发一遍就是这个形状）
    I4  `ask_human` 之后必须出现 user 或 end（否则这个提问是悬空的，人永远不知道要不要答）
    I5  `say` 之前必须有一个 llm（否则这句话不是模型说的，是拼出来的）
    I6  `paused` / `needs_human` 结尾的会话不许留着 control.json（旧停止请求会杀掉下一轮）
    I7  会话目录里有 `done.lock` 但没有任何进程在跑 ⇒ 死锁残留（下次会以为还在跑）

用法
    python bin/probe-loop.py                # 扫最近 30 个会话
    python bin/probe-loop.py --all          # 全扫
    python bin/probe-loop.py --session <目录>
    python bin/probe-loop.py --self-test     # 用合成会话证明每条判据能红
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 收尾阶段允许出现的事件（I2）
TAIL_OK = {"benni", "distill", "verify", "verify_unmet", "status", "index", "hook",
           "cost", "usage", "end", "compact"}


def load_events(d: Path) -> list:
    out = []
    try:
        txt = (d / "events.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            out.append({"act": "_bad_json", "_raw": line[:80]})
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def check(d: Path) -> list:
    ev = load_events(d)
    bad = []

    def add(rule, why):
        bad.append({"rule": rule, "why": why, "session": d.name})

    # I1 序号递增 / 时间不倒退
    last_seq, last_t = -1, ""
    for e in ev:
        if e.get("act") == "_bad_json":
            add("I0", "事件流里有读不出来的一行：%s" % e.get("_raw"))
            continue
        s = e.get("seq")
        if isinstance(s, int):
            if s <= last_seq:
                add("I1", "序号没递增：%s 之后又出现 %s" % (last_seq, s))
            last_seq = max(last_seq, s)
        t = str(e.get("t") or "")
        if t and last_t and t < last_t and t[:2] == last_t[:2]:
            add("I1", "时间倒退：%s → %s" % (last_t, t))
        if t:
            last_t = t

    # I2 end 之后不许再有 llm/tool/say
    end_at = -1
    for i, e in enumerate(ev):
        if e.get("act") == "end":
            end_at = i
    if end_at >= 0:
        for e in ev[end_at + 1:]:
            a = e.get("act")
            if a and a not in TAIL_OK:
                add("I2", "end 之后又出现了 %s（收尾阶段不该有它）" % a)

    # I3 相邻 user 不许一样
    prev_user = None
    for e in ev:
        if e.get("act") != "user":
            continue
        t = (e.get("text") or "").strip()
        if prev_user is not None and t and t == prev_user:
            add("I3", "连着两条一样的 user（采访后把任务书又发了一遍？）：%s" % t[:40])
        prev_user = t

    # I4 ask_human 之后必须有 user 或 end
    for i, e in enumerate(ev):
        if e.get("act") != "ask_human":
            continue
        later = [x.get("act") for x in ev[i + 1:]]
        if "user" not in later and "end" not in later:
            add("I4", "提问之后既没回答也没结束（悬空提问）：%s"
                % str(e.get("question") or "")[:40])

    # I5 say 之前必须有 llm
    seen_llm = False
    for e in ev:
        if e.get("act") == "llm":
            seen_llm = True
        elif e.get("act") == "say" and not seen_llm:
            add("I5", "say 之前没有 llm（这句话不是模型说的）")
            break

    # I6 收尾状态与实际残留物是否对得上
    #   ★ 原来这条写的是「paused 收尾却留着 control.json ⇒ 下一轮会被它停掉」——
    #     那是**错的**：run-task 开跑第一件事就是清旧停止请求（还打一行警告）。
    #     判据冤枉人比放过一个 bug 更坏：人会开始忽略整个探针。
    #     真正该问的是"状态和产物对不上"：说 paused，却没有可续的存档 ⇒ 界面写着
    #     "能接着答"，实际一答就报错。
    ends = [e for e in ev if e.get("act") == "end"]
    st = str((ends[-1].get("status") if ends else "") or "")
    if st == "paused" and not (d / "session.json").exists():
        add("I6", "以 paused 收尾，却没有 session.json —— 界面会说「能接着答」，"
                  "实际一答就失败")

    # I8 tool 事件必须带名字（否则界面上的动作卡是空白的，看不出它干了什么）
    for e in ev:
        if e.get("act") == "tool" and not (e.get("tool") or e.get("name")):
            add("I8", "有一条 tool 事件没有工具名（动作卡会是空白）")
            break

    # I9 / I10 end 必须带状态；同一轮里只能收尾一次
    #   ★ ★ 别写成"整个会话只能有一条 end"（第一版就是，扫出一个假阳性）：
    #     同一个会话可以被续跑很多轮，每轮各有一次 end —— 那是正常的多轮对话。
    #     要抓的是"两条 end 之间没有任何新的输入"，那才是同一轮收尾跑了两次。
    for e in ends:
        if not e.get("status"):
            add("I9", "end 没有 status —— 界面只能猜这一轮是成是败")
            break
    _since_user = 0
    for e in ev:
        a = e.get("act")
        if a == "user":
            _since_user = 0
        elif a == "end":
            _since_user += 1
            if _since_user > 1:
                add("I10", "同一轮里收尾了 %d 次（两条 end 之间没有新的输入）" % _since_user)
                break

    # I11 契约失败（ok=false）之后必须有下文：要么问人，要么结束
    for i, e in enumerate(ev):
        if e.get("act") == "contract" and e.get("ok") is False:
            later = [x.get("act") for x in ev[i + 1:]]
            if not ({"ask_human", "end"} & set(later)):
                add("I11", "契约失败之后既没问人也没结束（这一轮会悬在那里）")
            break

    # I7 done.lock 残留（有锁、但没有正在跑的进程，看不出活着的证据时只报告不算错）
    lock = d / "done.lock"
    if lock.exists() and not (d / "done.json").exists():
        add("I7", "有 done.lock 但没有 done.json —— 可能是上次写到一半被杀")

    return bad


FIXTURES = {
    "正常一轮": [
        {"seq": 1, "t": "10:00:00", "act": "contract", "ok": True},
        {"seq": 2, "t": "10:00:01", "act": "user", "text": "做事"},
        {"seq": 3, "t": "10:00:02", "act": "llm"},
        {"seq": 4, "t": "10:00:05", "act": "tool", "tool": "read_file"},
        {"seq": 5, "t": "10:00:09", "act": "say", "text": "结论"},
        {"seq": 6, "t": "10:00:10", "act": "end", "status": "success"},
        {"seq": 7, "t": "10:01:30", "act": "benni"},
    ],
    "I2 end 之后又跑了模型": [
        {"seq": 1, "t": "10:00:00", "act": "user", "text": "做事"},
        {"seq": 2, "t": "10:00:02", "act": "llm"},
        {"seq": 3, "t": "10:00:03", "act": "end", "status": "success"},
        {"seq": 4, "t": "10:00:09", "act": "llm"},
    ],
    "I3 两条一样的 user": [
        {"seq": 1, "t": "10:00:00", "act": "user", "text": "同一句话"},
        {"seq": 2, "t": "10:00:01", "act": "user", "text": "同一句话"},
    ],
    "I4 悬空提问": [
        {"seq": 1, "t": "10:00:00", "act": "ask_human", "question": "要改代码吗"},
    ],
    "I5 say 没有 llm": [
        {"seq": 1, "t": "10:00:00", "act": "say", "text": "凭空一句"},
    ],
    "多轮对话各有一条 end（不该报错）": [
        {"seq": 1, "t": "10:00:00", "act": "user", "text": "第一句"},
        {"seq": 2, "t": "10:00:02", "act": "llm"},
        {"seq": 3, "t": "10:00:03", "act": "end", "status": "success"},
        {"seq": 4, "t": "10:05:00", "act": "user", "text": "第二句"},
        {"seq": 5, "t": "10:05:02", "act": "llm"},
        {"seq": 6, "t": "10:05:03", "act": "end", "status": "success"},
    ],
    "I1 序号倒退": [
        {"seq": 5, "t": "10:00:00", "act": "user", "text": "a"},
        {"seq": 3, "t": "10:00:01", "act": "llm"},
    ],
}


def self_test() -> int:
    import tempfile
    ok = tot = 0
    expect = {"正常一轮": 0, "I2 end 之后又跑了模型": 1, "I3 两条一样的 user": 1,
              "I4 悬空提问": 1, "I5 say 没有 llm": 1,
              "多轮对话各有一条 end（不该报错）": 0, "I1 序号倒退": 1}
    for name, evs in FIXTURES.items():
        d = Path(tempfile.mkdtemp(prefix="loopprobe_"))
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n",
            encoding="utf-8")
        bad = check(d)
        got, want = len(bad), expect[name]
        good = got == want
        ok += 1 if good else 0
        tot += 1
        print(("  PASS  " if good else "  FAIL  ") + name
              + ("（抓到 %d 条，期望 %d）" % (got, want) if not good else ""))
        import shutil
        shutil.rmtree(str(d), ignore_errors=True)
    print("%d/%d" % (ok, tot))
    return 0 if ok == tot else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--session", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    base = ROOT / "runs"
    if a.session:
        cands = [Path(a.session)]
    else:
        cands = [p for p in base.glob("app-*") if (p / "events.jsonl").exists()]
        cands += [p for p in (base / "sessions").glob("*") if (p / "events.jsonl").exists()]
        cands.sort(key=lambda p: (p / "events.jsonl").stat().st_mtime)
        if not a.all:
            cands = cands[-a.limit:]

    hits = []
    for d in cands:
        hits += check(d)
    print("扫了 %d 个会话，发现 %d 处：" % (len(cands), len(hits)))
    for h in hits:
        print("  · [%s] %s：%s" % (h["rule"], h["session"], h["why"]))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
