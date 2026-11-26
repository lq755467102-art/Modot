#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型选择链路探针 —— 逐段走一遍 `/api/models` 的每个动作。

为什么要它（2026-10-27 用户：「你模拟用户操作跑一遍模型选择系统的所有链路」）
    界面点一遍只能证明「点下去有反应」，证明不了「点了之后世界真的变了」。
    这条链上的坑恰好都在后半句：空手互换返回成功、删掉正在用的那份还显示它、
    key 从响应里漏出去、环境变量把选择盖住却不吭声。
    ⇒ 按接口分段验，每段只问一件事，并且**真去看磁盘上那份文件变了没有**。

纪律
    · 只用临时目录里的档案文件（`HARNESS_MODELS`），真档案只在「读」这一侧被复制一份；
    · 不点任何会改世界的东西（不 select 真档案、不 swap 真档案）；
    · 服务由本脚本起、本脚本收，不留后台进程；
    · 判据只认「响应 + 磁盘」两处事实，不认我们自己写的注释。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "probe-token-not-a-secret"
FAILS = []
PASSES = []


# 收集单条判据结果；探针退出状态取决于完整失败列表。
def say(ok, name, detail=""):
    (PASSES if ok else FAILS).append((name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  —— " + detail) if detail else ""),
          flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    # 探针把临时档案交给 `bin/server.py`。
    # 清掉会覆盖模型选择的继承变量，特意测试覆盖时再通过 `extra_env` 注入。
    def __init__(self, models_file: Path, extra_env: dict = None):
        self.port = free_port()
        env = dict(os.environ)
        env.update({"HARNESS_MODELS": str(models_file),
                    "HARNESS_TOKEN": TOKEN,
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8"})
        for k in ("HARNESS_BASE_URL", "HARNESS_API_KEY", "HARNESS_MODEL",
                  "HARNESS_REVIEW_BASE_URL", "HARNESS_REVIEW_API_KEY",
                  "HARNESS_REVIEW_MODEL"):
            env.pop(k, None)
        env.update(extra_env or {})
        self.p = subprocess.Popen([sys.executable, "bin/server.py", "--port", str(self.port)],
                                  cwd=str(ROOT), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace")
        self.base = "http://127.0.0.1:%d" % self.port
        self._wait()

    # 以本机健康接口确认服务可以接请求；进程提前退出时取其末尾日志归因。
    def _wait(self, timeout=25.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.p.poll() is not None:
                out = self.p.stdout.read() if self.p.stdout else ""
                raise RuntimeError("服务起不来（退出码 %s）：\n%s" % (self.p.returncode, out[-2000:]))
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("服务 25 秒内没起来")

    # 统一返回 HTTP 状态和原正文，错误状态仍作为探针可断言的响应。
    def req(self, path, body=None, token=True, method=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Harness-Token"] = TOKEN
        m = method or ("POST" if body is not None else "GET")
        rq = urllib.request.Request(self.base + path, data=data, headers=headers, method=m)
        try:
            with urllib.request.urlopen(rq, timeout=40) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    # 让探针在 JSON 解码失败时仍能核对状态码和原响应内容。
    def j(self, path, body=None, token=True):
        code, raw = self.req(path, body, token=token)
        try:
            return code, json.loads(raw), raw
        except json.JSONDecodeError:
            return code, {}, raw

    # 正常关闭探针启动的子进程，超时后强制回收。
    def stop(self):
        try:
            self.p.terminate()
            self.p.wait(timeout=8)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


def read_file(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 一段：空档案（新建这条路） ───────────────────────────────────────
def run_empty(tmp: Path):
    f = tmp / "models.json"
    srv = Server(f)
    try:
        print("\n[一] 空档案：新建 / 校验 / 自动选上", flush=True)

        code, _, _ = srv.j("/api/models", token=False)
        say(code == 401, "不带钥匙取档案被拒（默认拒绝）", "实际 %s" % code)

        code, d, raw = srv.j("/api/models")
        say(code == 200 and d.get("profiles") == [], "空档案时档案列表是空的",
            "profiles=%d" % len(d.get("profiles") or []))
        say(d.get("effective", {}).get("main", {}).get("source") in ("creds", "env", "none"),
            "空档案时「自我」有回落来源（不是一片空白）",
            "source=%s" % d.get("effective", {}).get("main", {}).get("source"))

        # 四种不合法的存：每种都必须明确说不
        cases = [("不给 key", dict(name="x", base_url="https://api.deepseek.com/v1", model="m")),
                 ("不给模型名", dict(name="x", base_url="https://api.deepseek.com/v1", api_key="k")),
                 ("不给端点", dict(name="x", api_key="k", model="m")),
                 ("端点不是 http(s)", dict(name="x", base_url="ftp://a", api_key="k", model="m"))]
        for label, body in cases:
            code, r, _ = srv.j("/api/models", dict(body, act="save"))
            say(r.get("ok") is False and bool(r.get("error")),
                "存档案：%s 时要拒绝并说原因" % label, str(r.get("error") or r)[:90])

        code, r, _ = srv.j("/api/models", {"act": "save", "base_url": "https://api.deepseek.com/v1",
                                           "api_key": "sk-AAA-first-key-0001", "model": "deepseek-flash"})
        id1 = r.get("id") or ""
        say(r.get("ok") is True and bool(id1), "存第一份：成功且拿到 id", str(r.get("note") or r)[:80])

        code, r, _ = srv.j("/api/models", {"act": "save", "base_url": "https://api.moonshot.cn/v1",
                                           "api_key": "sk-BBB-second-key-0002", "model": "kimi-k3"})
        id2 = r.get("id") or ""
        say(r.get("ok") is True and id2 and id2 != id1, "存第二份：id 与第一份不同", "%s / %s" % (id1, id2))

        code, d, raw = srv.j("/api/models")
        profs = d.get("profiles") or []
        say(len(profs) == 2, "列表里是两份", "实际 %d 份" % len(profs))
        say(d.get("main") == id1, "第一份自动成了「自我」（新用户不用先想选哪份）",
            "main=%r 期望 %r" % (d.get("main"), id1))
        leak = [k for k in ("sk-AAA-first-key-0001", "sk-BBB-second-key-0002") if k in raw]
        say(not leak, "响应里没有明文 key（只有掩码）", "漏了：%s" % leak if leak else "两个 key 都没出现")
        has_mask = any(p.get("key_masked") and "…" in p.get("key_masked", "") for p in profs)
        say(has_mask, "档案里给的是掩码（能认出是哪把、认不出全文）",
            str([p.get("key_masked") for p in profs]))

        # 同一份身份再存一次：不许变两份
        code, r, _ = srv.j("/api/models", {"act": "save", "base_url": "https://api.deepseek.com/v1",
                                          "api_key": "sk-AAA-first-key-0001", "model": "deepseek-flash"})
        code, d, _ = srv.j("/api/models")
        say(len(d.get("profiles") or []) == 2, "同样内容再存一次不会多出一份（去重）",
            "实际 %d 份" % len(d.get("profiles") or []))

        print("\n[二] 选择 / 互换（假成功的重灾区）", flush=True)
        code, r, _ = srv.j("/api/models", {"act": "select", "role": "review", "id": id2})
        say(r.get("ok") is True, "把「本我」选到第二份", str(r.get("note") or r)[:80])
        say("kimi-k3" in str(r.get("note") or ""),
            "提示语说的是模型名（不是档案名，否则互换后会读出「自我用：本我」）",
            str(r.get("note") or "")[:80])
        code, d, _ = srv.j("/api/models")
        say(d.get("review") == id2 and d.get("main") == id1, "两边都选上之后，档案里的选择是对的",
            "main=%r review=%r" % (d.get("main"), d.get("review")))

        code, r, _ = srv.j("/api/models", {"act": "swap"})
        code, d, _ = srv.j("/api/models")
        say(r.get("ok") is True and d.get("main") == id2 and d.get("review") == id1,
            "两边都有档案时互换：真的换了（不是只换了个提示）",
            "main=%r review=%r" % (d.get("main"), d.get("review")))
        say(d.get("effective", {}).get("main", {}).get("model") == "kimi-k3",
            "互换后「自我」实际生效的模型跟着变了",
            "effective.main.model=%s" % d.get("effective", {}).get("main", {}).get("model"))

        # 只有一边选上 ⇒ 必须拒绝
        srv.j("/api/models", {"act": "select", "role": "review", "id": ""})
        code, r, _ = srv.j("/api/models", {"act": "swap"})
        say(r.get("ok") is False and bool(r.get("hint")),
            "只有一边选上时互换：拒绝，并说下一步怎么做", str(r.get("error") or "")[:80])

        # 两边都空 ⇒ 必须拒绝（这一条就是用户报的「按钮依旧有问题」）
        srv.j("/api/models", {"act": "select", "role": "main", "id": ""})
        code, r, _ = srv.j("/api/models", {"act": "swap"})
        say(r.get("ok") is False, "两边都空时互换：拒绝（不再是假成功）", str(r.get("error") or "")[:80])

        print("\n[三] 改 / 删（删掉正在用的那份）", flush=True)
        srv.j("/api/models", {"act": "select", "role": "main", "id": id1})
        code, before, _ = srv.j("/api/models")
        m_before = next((p.get("key_masked") for p in before.get("profiles") or []
                         if p.get("id") == id1), "")
        code, r, _ = srv.j("/api/models", {"act": "save", "id": id1, "model": "deepseek-v4-pro"})
        code, after, _ = srv.j("/api/models")
        m_after = next((p.get("key_masked") for p in after.get("profiles") or []
                        if p.get("id") == id1), "")
        say(r.get("ok") is True and m_before == m_after and m_after,
            "改档案时 key 留空 = 不动原来的 key（不会被清成空壳）",
            "%r → %r" % (m_before, m_after))
        say(next((p.get("model") for p in after.get("profiles") or [] if p.get("id") == id1), "") ==
            "deepseek-v4-pro", "改模型名生效了", "")

        code, r, _ = srv.j("/api/models", {"act": "select", "role": "main", "id": "p-nosuchid"})
        say(r.get("ok") is False, "选一份不存在的档案：拒绝", str(r.get("error") or "")[:60])

        code, r, _ = srv.j("/api/models", {"act": "del", "id": id1})
        code, d, _ = srv.j("/api/models")
        say(r.get("ok") is True and len(d.get("profiles") or []) == 1, "删一份：列表少一份", "")
        say(d.get("main") == "", "删掉正在用的那份后，「自我」的选择被一起清掉"
                                "（否则界面会显示一个不存在的东西）",
            "main=%r" % d.get("main"))
        say(d.get("effective", {}).get("main", {}).get("source") in ("creds", "env", "none"),
            "清掉选择后，「自我」回落到环境变量 / 凭据文件（不是变成没模型）",
            "source=%s" % d.get("effective", {}).get("main", {}).get("source"))

        code, r, _ = srv.j("/api/models", {"act": "del", "id": id1})
        say(r.get("ok") is False, "删一份已经没了的：拒绝，不假装成功", str(r.get("error") or "")[:60])

        code, r, _ = srv.j("/api/models", {"act": "乱写"})
        say(r.get("ok") is False, "不认识的动作：拒绝", str(r.get("error") or "")[:60])
    finally:
        srv.stop()


# ── 二段：真档案的一份副本（读的那一侧） ─────────────────────────────
def run_copy(tmp: Path):
    real = Path.home() / ".workbuddy" / "harness" / "models.json"
    if not real.exists():
        print("\n[四] 真档案副本：跳过（%s 不存在）" % real, flush=True)
        return
    f = tmp / "models-real.json"
    shutil.copyfile(str(real), str(f))
    src = read_file(f)
    real_keys = [str(x.get("api_key") or "") for x in (src.get("profiles") or [])
                 if str(x.get("api_key") or "")]
    srv = Server(f)
    try:
        print("\n[四] 真档案的一份副本：读对了没有 / key 漏没漏", flush=True)
        code, d, raw = srv.j("/api/models")
        say(d.get("path") == str(f), "读的是副本，不是真档案（探针不碰真档）",
            "%s" % d.get("path"))
        say(len(d.get("profiles") or []) == len(src.get("profiles") or []),
            "副本里的档案份数一致",
            "%d / %d" % (len(d.get("profiles") or []), len(src.get("profiles") or [])))
        say(d.get("main") == (src.get("main") or ""), "「自我」的选择与文件一致",
            "%r / %r" % (d.get("main"), src.get("main")))
        say(d.get("review") == (src.get("review") or ""), "「本我」的选择与文件一致",
            "%r / %r" % (d.get("review"), src.get("review")))
        leaked = [k[:6] + "…" for k in real_keys if k in raw]
        say(not leaked, "真档案的 key 没有明文出现在响应里",
            ("漏了 %d 把：%s" % (len(leaked), leaked)) if leaked else "%d 把 key 全部只给掩码" % len(real_keys))
        eff = d.get("effective") or {}
        for role in ("main", "review"):
            e = eff.get(role) or {}
            ok = bool(e.get("model"))
            say(ok, "「%s」能解析出一个模型" % ("自我" if role == "main" else "本我"),
                "%s（来源：%s）" % (e.get("model") or "空", e.get("source_cn") or "?"))
            if e.get("overridden"):
                print("        · 注意：「%s」有字段被环境变量盖住：%s"
                      % ("自我" if role == "main" else "本我", e.get("overridden")), flush=True)

        # 真档案的两边都有档案 ⇒ 互换应当真的动；动的是副本
        if (src.get("main") or "") and (src.get("review") or ""):
            a, b = src.get("main"), src.get("review")
            code, r, _ = srv.j("/api/models", {"act": "swap"})
            code, d2, _ = srv.j("/api/models")
            on_disk = read_file(f)
            say(r.get("ok") is True and d2.get("main") == b and d2.get("review") == a,
                "真档案形态下互换：两边真的对调了", "main=%r review=%r" % (d2.get("main"), d2.get("review")))
            say(on_disk.get("main") == b and on_disk.get("review") == a,
                "互换结果写进了文件（不是只在内存里换）",
                "文件 main=%r review=%r" % (on_disk.get("main"), on_disk.get("review")))
        else:
            print("        · 真档案只有一边（或都没选），互换这条跳过 —— 这正是空手互换那个坑的形状",
                  flush=True)
    finally:
        srv.stop()


# ── 三段：环境变量盖住选择（沉默失败的另一种形状） ───────────────────
def run_override(tmp: Path):
    f = tmp / "models-env.json"
    f.write_text(json.dumps({"version": 1, "main": "", "review": "", "profiles": [
        {"id": "p-fixed001", "name": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
         "api_key": "sk-CCC-third-key-0003", "model": "deepseek-flash", "at": "2026-10-27 10:00"}
    ]}, ensure_ascii=False), encoding="utf-8")
    srv = Server(f, extra_env={"HARNESS_MODEL": "deepseek-v4-pro"})
    try:
        print("\n[五] 环境变量盖住档案选择：要说出来", flush=True)
        code, d, _ = srv.j("/api/models")
        srv.j("/api/models", {"act": "select", "role": "main", "id": "p-fixed001"})
        code, d, _ = srv.j("/api/models")
        e = (d.get("effective") or {}).get("main") or {}
        say("model" in (e.get("overridden") or []),
            "档案选的模型被环境变量盖住时，界面上能看到「被盖了」", "overridden=%r" % (e.get("overridden"),))
        say(e.get("model") == "deepseek-v4-pro", "实际生效的是环境变量那个模型",
            "model=%s" % e.get("model"))
        say(e.get("source") == "models", "端点/key 仍来自档案（逐字段比，不整份覆盖）",
            "source=%s" % e.get("source"))
    finally:
        srv.stop()


# ── 四段：拉取可用模型（问端点有哪些模型） ───────────────────────────
def run_probe_action(tmp: Path):
    f = tmp / "models-probe.json"
    f.write_text(json.dumps({"version": 1, "main": "", "review": "", "profiles": []},
                            ensure_ascii=False), encoding="utf-8")
    srv = Server(f)
    bad_key = "sk-DDD-bogus-key-0004"
    try:
        print("\n[六] 拉取可用模型：错的时候要说清哪一种错", flush=True)

        code, r, raw = srv.j("/api/models", {"act": "probe",
                                            "base_url": "https://api.deepseek.com/v1",
                                            "api_key": bad_key})
        say(r.get("ok") is False and bool(r.get("why")),
            "key 不对时：明确失败并给原因（不是回一个空清单让人以为端点没模型）",
            str(r.get("why") or "")[:100])
        say(bad_key not in raw, "失败响应里不回显 key", "key 没出现在响应里" if bad_key not in raw else "漏了")
        say("401" in str(r.get("why") or "") or "连不上" in str(r.get("why") or ""),
            "原因里看得出是哪一类（端点拒绝 / 连不上）", str(r.get("why") or "")[:60])

        code, r, raw = srv.j("/api/models", {"act": "probe",
                                            "base_url": "https://no-such-host.invalid/v1",
                                            "api_key": bad_key})
        say(r.get("ok") is False and bool(r.get("why")),
            "端点不存在时：失败并说连不上", str(r.get("why") or "")[:80])
        say(bad_key not in raw, "连不上时也不回显 key", "")

        code, r, raw = srv.j("/api/models", {"act": "probe", "api_key": ""})
        # 空 body + 空档案 ⇒ 会回落到「当前生效的那套」，所以这一条只问它有没有给出结论
        say(isinstance(r.get("models"), list) and (r.get("ok") is True or bool(r.get("why"))),
            "什么参数都不给：要么给清单、要么说清楚为什么没有（不许沉默）",
            "ok=%r why=%r" % (r.get("ok"), str(r.get("why") or "")[:60]))
        say(bad_key not in raw, "这一条也没有回显 key", "")
    finally:
        srv.stop()


# 分开检查空配置、实际档案副本、环境覆盖和模型探测，临时目录随探针退出清理。
def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="probe-models-"))
    print("临时目录：%s" % tmpdir, flush=True)
    try:
        run_empty(tmpdir)
        run_copy(tmpdir)
        run_override(tmpdir)
        run_probe_action(tmpdir)
    finally:
        shutil.rmtree(str(tmpdir), ignore_errors=True)
    print("\n" + "=" * 56, flush=True)
    print("通过 %d 条，失败 %d 条" % (len(PASSES), len(FAILS)), flush=True)
    for n, dd in FAILS:
        print("  ✗ %s%s" % (n, ("（%s）" % dd) if dd else ""), flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
