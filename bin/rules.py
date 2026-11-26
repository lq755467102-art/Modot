#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rules.py —— 沙箱规则文件里**免扫名单**的命令行入口（人用的，不是 agent 用的）。

为什么要有它
    `harness/sandbox_rules.json` 里的 `script_allow` 是"这些脚本跳过内容扫描"。
    免扫现在带哈希（`sha256`）：**内容是这份**才免扫，改一个字就失效、退回扫描。
    所以每次改完 `bin/accept.py` 这类文件，得有人重新签一下 —— 这个脚本就是那只手。

为什么 agent 不能自己签
    能签 = 能给自己发免死金牌。所以这个文件不给 agent 的文件工具改
    （`grants.is_protected` 挡读写），命令层也禁读（`runcheck.DENY_PATH`）。
    ⇒ agent 想改，只能写进结论让人来跑这一条。

五个动作
    list        看现在签了哪些、哪些已经对不上了（顺带看免问例外）
    sign        重签（默认签名单里**所有**存在的文件；也可指定一个路径）
    add         把一个新脚本加进名单并签名
    allow       加一条「以后这类都别问」的例外（**直接生效**，不走提案）
    revoke      撤一条例外（给 id，或者给路径/工具名）

★ `allow` 和 `grant.py never` 的区别（两条路都有，因为用途不同）：
  · `allow` = 人已经想清楚了，直接写下来（像编辑规则文件，但有留底和底线校验）；
  · `never` = 人刚看到被拦的那一步，批这一次 **并且** 记一条待采纳的例外。
  两条都只盖 `ask`、盖不住 `deny`，都不许全局范围（`**` 开头 / 盘根 / 家目录根）。

★ 为什么 revoke 也放这里：例外是"以后不问"，它唯一的安全阀就是**随时能收回**。
  只在界面上能撤的话，人换了机器 / 服务没起来的时候就撤不掉 —— 那不叫能收回。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import paths as P            # noqa: E402


def _rules_path() -> Path:
    """和数据根同一套约定：改了数据根那份就改那份，否则改程序根那份。"""
    cand = P.data_root() / "harness" / "sandbox_rules.json"
    if cand.exists():
        return cand
    return P.app_root() / "harness" / "sandbox_rules.json"


def _load() -> dict:
    p = _rules_path()
    if not p.exists():
        raise SystemExit("找不到规则文件：%s" % p)
    return json.loads(p.read_text(encoding="utf-8"))


def _save(d: dict) -> None:
    # ★ 走 `ruleedit` 的统一写入口（2026-10-26）：它会先留底、原子替换、清 mtime 缓存。
    #   这里自己 write_text 的话，"签完不生效"和"改了没法退回去"就会各出现一次。
    from loop import ruleedit as _RE
    r = _RE.write(d)
    if not r.get("ok"):
        raise SystemExit(r.get("error") or "规则文件写不进去")


def _items(d: dict) -> list:
    sa = d.setdefault("script_allow", {})
    if "items" not in sa:
        # 老格式（`paths` 纯列表）自动升级成带哈希的形态
        sa["items"] = [{"path": str(x), "sha256": ""} for x in (sa.get("paths") or [])]
        sa.pop("paths", None)
    return sa["items"]


def _real(pat: str) -> Path:
    """`<work>/bin/x.py` → 真实路径（名单里就是这么写的，别在两边各算一次）。"""
    return Path(str(pat).replace("<work>", str(P.data_root()).replace("\\", "/")))


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rules.py", description="免扫名单（人用）")
    ap.add_argument("act", nargs="?", default="list",
                    choices=("list", "sign", "add", "allow", "revoke"))
    ap.add_argument("path", nargs="?", default="",
                    help="哪个脚本（相对仓库根）；allow 时这里是 file / mcp")
    ap.add_argument("rest", nargs="*", default=[],
                    help='allow 的参数：`allow file read "<目录>/**"` / `allow mcp <server> <tool>`')
    ap.add_argument("--why", default="", help="为什么放这一条（写进规则文件，也会出现在留底里）")
    ap.add_argument("--action", default="read", choices=("read", "write"),
                    help="配合 `allow mcp`：这个工具的哪类调用")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    d = _load()
    items = _items(d)

    if a.act == "list":
        rows = []
        for it in items:
            fp = _real(it.get("path") or "")
            want = str(it.get("sha256") or "")
            got = _sha(fp) if fp.exists() else ""
            state = ("未签" if not want else
                     ("对不上" if (got and got != want) else
                      ("文件不在" if not got else "已签")))
            rows.append({"path": it.get("path"), "state": state,
                         "signed": want[:8], "now": got[:8]})
        if a.json:
            print(json.dumps({"file": str(_rules_path()), "items": rows},
                             ensure_ascii=False, indent=1))
            return 0
        print("规则文件：%s" % _rules_path())
        for r in rows:
            print("  [%-6s] %-42s 签过 %s ｜ 现在 %s"
                  % (r["state"], r["path"], r["signed"] or "—", r["now"] or "—"))
        bad = [r for r in rows if r["state"] in ("未签", "对不上")]
        if bad:
            print("\n有 %d 条要重签：python bin/rules.py sign" % len(bad))
        # ★ 免问例外也要在这里看得见（2026-10-26）：它是这个文件里**唯一扩大权力**的部分。
        from loop import ruleedit as RE
        ex = RE.listing()
        print("\n免问例外（以后这类都别问）：%d 条" % ex["count"])
        for it in ex["items"]:
            print("  [%s] %-48s %s%s" % (it.get("id") or "—", it.get("label") or it.get("key"),
                                         it.get("at") or "",
                                         (" 来自 " + it["src"]) if it.get("src") else ""))
        if ex["count"]:
            print("  撤一条：python bin/rules.py revoke <id 或路径>")
        return 0

    if a.act == "allow":
        # ★ 人直接写一条例外（2026-10-26）。走 `ruleedit`：留底 → 校验 → 原子替换。
        #   为什么还留着这条路：`grant.py never` 需要"刚被拦的那一步"作上下文，
        #   而有些例外是人事先就想好的（"这个目录以后别问"），那时没有那一步可批。
        from loop import ruleedit as RE
        _why = a.why.strip() or "人跑的 bin/rules.py allow"
        _sub = (a.path or "").strip().lower()
        _r0 = a.rest or []
        if _sub == "file":
            if len(_r0) < 2:
                raise SystemExit('用法：python bin/rules.py allow file <read|write> "<目录>/**" '
                                 '--why "为什么"')
            r = RE.add_file(_r0[0], _r0[1], why=_why, src="cli")
        elif _sub == "mcp":
            if len(_r0) < 2:
                raise SystemExit("用法：python bin/rules.py allow mcp <server> <tool> "
                                 "[--action read] --why 为什么")
            r = RE.add_mcp(_r0[0], _r0[1], a.action, why=_why, src="cli")
        else:
            raise SystemExit("用法：… allow file <read|write> <范围> ｜ … allow mcp <server> <tool>")
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        elif r.get("ok"):
            print("✓ %s" % (r.get("note") or "记下了"))
        else:
            print("✗ %s" % (r.get("error") or "没成"), file=sys.stderr)
        return 0 if r.get("ok") else 2

    if a.act == "revoke":
        # ★ 撤一条免问例外（2026-10-26）。给的可以是 id，也可以是路径/工具名。
        from loop import ruleedit as RE
        if not a.path:
            raise SystemExit("revoke 要说清撤哪条（先跑 `python bin/rules.py list` 看 id）")
        r = RE.revoke(rid=a.path, domain="", key="")
        if not r.get("ok"):
            # id 没命中就按路径/工具名撤（命令行里路径比 8 位哈希好记）
            r = RE.revoke(rid="", domain="file", key=a.path)
        if not r.get("ok"):
            # ★ 这一段原来调的是 `revoke(domain="script")`，而 `revoke()` 只遍历
            #   allow_rules 的 file / mcp —— 这条分支**从来没命中过**，
            #   `script_allow` 里的条目根本撤不掉（2026-10-27 自测时发现的）。
            r = RE.revoke_script(a.path)
        if not r.get("ok"):
            r = RE.revoke(rid="", domain="mcp", key=a.path)
        if not r.get("ok"):
            print("没撤成：%s" % r.get("error"), file=sys.stderr)
            return 2
        print(r.get("note") or "撤了")
        return 0

    if a.act == "add":
        if not a.path:
            raise SystemExit("add 要说清加哪个脚本：python bin/rules.py add bin/xxx.py")
        rel = "<work>/" + a.path.replace("\\", "/").lstrip("./")
        if any(str(it.get("path")) == rel for it in items):
            print("名单里已经有了：%s（要更新哈希就用 sign）" % rel)
            return 0
        items.append({"path": rel, "sha256": ""})
        print("加进去了：%s（下面顺手签）" % rel)
        a.act = "sign"
        a.path = a.path

    # ── sign ──
    if not a.path:
        # 默认：名单里所有存在的文件全签（这是"我刚改完一批"的常用形态）
        n = 0
        for it in items:
            fp = _real(it.get("path") or "")
            if fp.exists():
                it["sha256"] = _sha(fp)
                n += 1
        _save(d)
        print("签了 %d 条（规则文件：%s）" % (n, _rules_path()))
        return 0

    rel = "<work>/" + a.path.replace("\\", "/").lstrip("./")
    hit = [it for it in items if str(it.get("path")) == rel]
    if not hit:
        print("名单里没有这条：%s（要新加用 add）" % rel, file=sys.stderr)
        return 2
    fp = _real(rel)
    if not fp.exists():
        print("文件不在：%s" % fp, file=sys.stderr)
        return 2
    hit[0]["sha256"] = _sha(fp)
    _save(d)
    print("签好了：%s（%s…）" % (rel, hit[0]["sha256"][:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
