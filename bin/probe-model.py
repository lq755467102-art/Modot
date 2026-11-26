#!/usr/bin/env python3
"""probe-model.py —— 拿到一把新 key，30 秒判断它能不能当本我。

为什么需要这个：
    推荐一个模型很容易，**"这把 key 到底能不能用、贵不贵、守不守格式"只能当场测。**
    这套脚本把四件事一次跑完，省掉来回试。

测四件事：
    ① 连通与认证      —— 端点对不对、key 有没有效
    ② 可用模型        —— 有没有你要的那个，上下文多大
    ③ 一次真实对话    —— 延迟、实际 usage
    ④ **本我格式合规** —— 给它一段真实轨迹，看它产出的五行过不过机械校验
                         这一条最关键：很多模型话说得漂亮，但守不住固定格式

另外算一笔账：按我们这种活（约 1200 token 进 / 600 出）跑一次要多少钱。

用法：
    python bin/probe-model.py --base https://open.bigmodel.cn/api/paas/v4 \\
                              --key sk-xxx --model glm-5.3-flash
    # 也可以只给 --profile，从凭据文件读（HARNESS_REVIEW_* 等）
    python bin/probe-model.py --profile HARNESS_REVIEW

退出码：0 全部通过｜1 有项目不通过｜3 连不上 / key 无效
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRED = os.path.expanduser("~/.workbuddy/harness/credentials.env")

MARKS = ("①", "②", "③", "④", "⑤")

# 一段极小的假轨迹：够触发"找缺口"的思考，又不烧 token
FAKE_TRACE = """## 人要的是什么
查清楚 X.py 有没有引用第三方库。

## 发生了什么（共 2 步，编号就是锚点要用的 N）
| # | 时间 | 内容 |
|---|---|---|
| 1 | 10:00:01 | 调用「读一个文件」｜读到 10867 字符（**被截断了，只给了前 4000 字**） |
| 2 | 10:00:03 | 调用了「跑一条只读命令」，退出码 0｜只匹配到行首的 import |

## 它最后交出来的话
这个文件没有第三方依赖，全文只导入了标准库。依据是第 21-26 行的 import 语句。
"""

FORMAT_REQ = """## 你的边界
- 只能读，不能改任何东西；不能给建议
- 不能说"我发现了一个问题"，只能说"这一条的 evidence 缺 X，你看算不算"

## 输出格式（五行，按顺序，不许扩写）
① 对得上吗   逐条对完成判据
② 我看到的   N 条事实，每条带命令或文件名
③ 我怀疑的   每条以 `- ` 开头，共四行：
             第一行：一句人话（不许出现命令、路径、文件名）
             第二行：`证据：<命令> → <结果>`
             第三行：`复核：<一条能重跑的命令>`
             第四行：`锚点：第 N 步`
④ 我没查的   不许写"无"
⑤ 我提醒的   必须由程序填，你先写 `测不到：占位`
"""

# 按我们这种活的典型量级（一次本我 ≈ 1200 进 / 600 出）
TYP_IN, TYP_OUT = 1200, 600


def read_cred() -> dict:
    p = Path(CRED)
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def http_json(url: str, key: str, body: dict | None = None, timeout: int = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    # ★ 用和 harness 同一个出口（默认直连）：探针测的是"端点通不通"，
    #   不该被本机那个会换端口的代理误导成"模型坏了"（2026-10-27）。
    from loop.netpath import opener as _netop
    with _netop().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ok(b): return "✓" if b else "✗"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--profile", default="", help="从凭据文件读，如 HARNESS_REVIEW")
    ap.add_argument("--price-in", type=float, default=0.0, help="每 1M 输入 token 的美元价")
    ap.add_argument("--price-out", type=float, default=0.0, help="每 1M 输出 token 的美元价")
    # ★ 思考型模型把大部分时间花在推理上：短问答 7~10 秒，但"写五行"要长得多。
    #   90 秒默认值实测不够（会 timeout），而且超时会被误读成"模型不行"。
    ap.add_argument("--timeout", type=int, default=240, help="单次请求超时（秒）")
    # ★ 思考档。实测智谱 glm-5.3-flash 默认思考很重：
    #   3000 token 预算能被推理吃光、正文为空；给到 6000 又会超时。
    #   用 OpenAI 标准的 reasoning_effort=low 可以压到 0 推理 token。
    #   （别用 "thinking":{"type":"disabled"} —— 智谱会报 1210「该模型始终思考」。）
    ap.add_argument("--reasoning-effort", default="",
                    help="low / medium / high；留空则用模型默认")
    a = ap.parse_args()

    cred = read_cred()
    pre = (a.profile or "").rstrip("_") + "_" if a.profile else ""
    # ★ 顺序要和 loop/provider.py 一致：**环境变量优先，其次凭据文件**。
    #   原来只读文件 ⇒ 设了环境变量也不生效，行为跟 provider 不一致，
    #   测出来的结论就不代表实际会跑的那条路。
    base = (a.base or os.environ.get(pre + "BASE_URL")
            or cred.get(pre + "BASE_URL", "")).rstrip("/")
    key = a.key or os.environ.get(pre + "API_KEY") or cred.get(pre + "API_KEY", "")
    model = a.model or os.environ.get(pre + "MODEL") or cred.get(pre + "MODEL", "")
    if not (base and key):
        print("缺参数：需要 --base 和 --key，或 --profile 指向凭据里的一套", file=sys.stderr)
        return 2
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]

    fails = 0
    print("探测：%s\n  model=%s\n" % (base, model or "(未指定)"))

    # ── ① 连通 + ② 模型列表 ─────────────────────────────
    print("① 连通与认证")
    models = []
    try:
        d = http_json(base + "/models", key, timeout=a.timeout)
        for m in (d.get("data") or [])[:40]:
            models.append({"id": m.get("id"), "ctx": m.get("context_length")})
        print("  %s 通，返回 %d 个模型" % (ok(True), len(models)))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print("  %s HTTP %s：%s" % (ok(False), e.code, body))
        if e.code in (401, 403):
            print("  ⇒ key 无效或没权限，先解决这个")
            return 3
        if e.code >= 500:
            # ★ 5xx 也算"连不上"：可能是本机代理转发的 upstream 失败（实测踩过 502）。
            #   语义一样 —— 端点用不了，不该报"通过"。
            print("  ⇒ 端点连不上（5xx，可能是代理转发失败），先解决网络/地址")
            return 3
        fails += 1
    except Exception as e:
        print("  %s 连不上：%s" % (ok(False), e))
        return 3

    if models:
        print("\n② 可用模型（前 8 个）")
        ids = [m["id"] for m in models if m["id"]]
        for m in models[:8]:
            ctx = m["ctx"]
            print("  %-34s %s" % (m["id"], ("上下文 %s" % format(ctx, ",")) if ctx else ""))
        if model and model not in ids:
            print("  ⚠ 你要的 %s 不在列表里 —— 拼写或版本号可能不对" % model)
            fails += 1
        elif not model and ids:
            model = ids[0]
            print("  （没指定 model，用第一个：%s）" % model)

    if not model:
        print("\n✗ 没有可用的 model 名，后面测不了", file=sys.stderr)
        return 1

    # ── ③ 一次真实对话 ────────────────────────────────
    print("\n③ 一次真实对话")
    t0 = time.time()
    try:
        _b = {"model": model, "max_tokens": 300,
              "messages": [{"role": "user", "content": "只回两个字：收到，不要推理"}]}
        if a.reasoning_effort:
            _b["reasoning_effort"] = a.reasoning_effort
        d = http_json(base + "/chat/completions", key, _b, timeout=a.timeout)
        ms = int((time.time() - t0) * 1000)
        u = d.get("usage") or {}
        print("  %s %d ms ｜ usage: prompt=%s output=%s"
              % (ok(True), ms, u.get("prompt_tokens"), u.get("completion_tokens")))
        if not u:
            print("  ⚠ 没返回 usage —— 那本我的 ⑤ 就只能写「测不到」")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:260]
        print("  %s HTTP %s" % (ok(False), e.code))
        print("  %s" % body)
        low = body.lower()
        if "quota" in low or "limit" in low or "balance" in low:
            print("  ⇒ 认证没问题，但**额度/余额不足** —— 充值或换 key 即可，配置不用改")
        fails += 1
    except Exception as e:
        print("  %s 失败：%s" % (ok(False), e))
        fails += 1

    # ── ④ 本我格式合规 ────────────────────────────────
    print("\n④ 本我格式合规（这一条最关键）")
    try:
        t0 = time.time()
        d = http_json(base + "/chat/completions", key,
                      # ★ 额度要给足：思考型模型会先花掉一大截在 reasoning 上，
                      #   给少了会得到 finish_reason=length 且正文为空 ——
                      #   那看起来像"模型不会写"，实际是"预算不够"。
                      {"model": model, "max_tokens": 8192,   # 和 provider 默认一致（3000 不够，思考会吃光）
                       "messages": [
                           {"role": "system", "content": "你只输出格式要求的那五行，不要额外解释。"},
                           {"role": "user", "content": FORMAT_REQ + "\n" + FAKE_TRACE}]},
                      timeout=a.timeout) if not a.reasoning_effort else http_json(
                          base + "/chat/completions", key,
                          {"model": model, "max_tokens": 8192,   # 和 provider 默认一致（3000 不够，思考会吃光）
                           "reasoning_effort": a.reasoning_effort,
                           "messages": [
                               {"role": "system", "content": "你只输出格式要求的那五行，不要额外解释。"},
                               {"role": "user", "content": FORMAT_REQ + "\n" + FAKE_TRACE}]},
                          timeout=a.timeout)
        ms = int((time.time() - t0) * 1000)
        ch0 = (d.get("choices") or [{}])[0]
        text = (ch0.get("message") or {}).get("content") or ""
        fr = ch0.get("finish_reason")
        u = d.get("usage") or {}
        if not text.strip():
            detail = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            print("  %s 正文是空的（finish_reason=%s%s）"
                  % (ok(False), fr, "，思考已花 %s token" % detail if detail else ""))
            print("     ⇒ 多半是 **max_tokens 不够**，思考吃光了预算；不是模型不会写")
            fails += 1
            print("\n===== 有 %d 项没过 =====" % fails)
            return 1
        tmp = ROOT / "runs" / "_probe_out.md"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "benni.py"),
                            "check", str(tmp)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        try:
            rep = json.loads(r.stdout)
        except Exception:
            rep = {"ok": False, "issues": ["输出不是合法 JSON：%s" % (r.stdout or r.stderr)[:120]]}
        print("  %s %d ms ｜ %s" % (ok(rep.get("ok")), ms, rep.get("verdict", "")))
        for i in (rep.get("issues") or [])[:3]:
            print("     问题：%s" % str(i)[:110])
        print("     （原始输出存在 %s，可自己看）" % tmp.relative_to(ROOT))
        if not rep.get("ok"):
            fails += 1
        # 顺手算钱
        if a.price_in or a.price_out:
            cost = (TYP_IN / 1e6) * a.price_in + (TYP_OUT / 1e6) * a.price_out
            print("\n⑤ 按我们这种活（约 %d 进 / %d 出）估算：**$%.5f / 次**"
                  % (TYP_IN, TYP_OUT, cost))
            print("   一天 50 轮 ⇒ $%.2f；一个月（22 天）⇒ $%.2f"
                  % (cost * 50, cost * 50 * 22))
        else:
            print("\n⑤ 没给 --price-in/--price-out，跳过成本估算")
    except Exception as e:
        print("  %s 失败：%s" % (ok(False), str(e)[:200]))
        fails += 1

    print("\n===== %s =====" % ("全部通过" if fails == 0 else "有 %d 项没过" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
