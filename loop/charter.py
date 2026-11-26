#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""总纲 = 从记忆树里选出来的几层，不是一份手写的文本

为什么是这个形状
    手写一份总纲，就等于树里那份多了一个副本，两份必漂 —— 这是这个项目一直在防的事。
    所以这里只认「**选哪几层**」，正文每次从树里现拼。拼出来的东西是产物，不进仓库。

和 system prompt 的区别
    system 是框架的地盘（我们改不动、也不知道它怎么拼）。总纲是我们自己的，
    而且**每轮都重新拼一次注入**，不是开场读一次 ——
    开场读一次的东西会被后续对话、工具结果、压缩一路挤到后面，而这件事不报警。

软上限，不是硬上限
    它进的是上下文，所以上限要按上下文的比例算：
        cap = context_limit × charter_ratio      （默认 150000 × 0.5 = 75000 字符）
    超了**不拒绝**，只报警，并且**列出丢哪层能省多少**，把人叫过来做抉择。
    理由是：该丢哪层是判断，判断不该由这里替人做。

字符当 token 用
    粗略按「中文字符数 ≈ token 数」估。这是**估算**，不是实测 ——
    真要精确得看 provider 的 usage，而那时候钱已经花了。
"""
# 主要引用方包括 `tools.py` 和 `memperms.py`。
from __future__ import annotations

import json
import re
from pathlib import Path

from . import rulenodes as RN

from . import paths as P            # 程序根 / 数据根，只此一处
ROOT = P.data_root()               # 数据根（打包后是用户目录；开发时就是仓库根）

DEFAULT_LIMIT = 150000
DEFAULT_RATIO = 0.5


def load_config(path: Path | None = None) -> dict:
    p = path or (ROOT / "harness" / "charter.json")
    if not p.exists():
        return {"roots": [], "charter": [], "context_limit": DEFAULT_LIMIT,
                "charter_ratio": DEFAULT_RATIO, "_missing": str(p)}
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.setdefault("roots", [])
    cfg.setdefault("charter", [])
    cfg.setdefault("context_limit", DEFAULT_LIMIT)
    cfg.setdefault("charter_ratio", DEFAULT_RATIO)
    return cfg


def resolve_root(roots: list[str]) -> tuple[Path | None, list[str]]:
    """按顺序找第一个真实存在的根。都不在就返回 None + 找过哪些 —— 不静默。"""
    tried = []
    for r in roots:
        p = Path(r)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        tried.append(str(p))
        if p.is_dir():
            return p, tried
    return None, tried


def layer_files(root: Path, name: str) -> list[Path]:
    """一层的组成：本体 + 同名的子层目录里的全部文件。

    子层算进父层，因为这棵树里子层就是父层的内容（父层只留索引和准则）。
    不把子层算进来，选"L3.守卫与判据"会只拿到 798 字的目录。
    """
    out: list[Path] = []
    f = root / "tree" / ("%s.md" % name)
    if f.exists():
        out.append(f)
    d = root / "tree" / name
    if d.is_dir():
        out.extend(sorted(d.glob("*.md")))
    return out


def layer_sections(root: Path, name: str) -> list[dict]:
    """一层拆成节点：`[{id, title, text}]`。`text` 是**原文切片**。

    ★ 为什么用切片而不是重新渲染（2026-10-26）：全选时必须拼回**一字不差**的原样，
      否则"full = 全带"这句话就不成立了，而那种偏差只有 diff 才看得出来。
    """
    out: list[dict] = []
    for fi, f in enumerate(layer_files(root, name)):
        try:
            # ★ state 要从**原始文本**里读（2026-10-29）：它写在 HTML 注释里，
            #   而下面的 strip_meta 会把注释整段删掉 —— 从剥过的文本里找不到（踩过）。
            #   ★ 逐行扫描而不是正则：这段要能一眼看懂，也不受转义影响。
            raw = f.read_text(encoding="utf-8", errors="replace")
            _states, _cur = {}, None
            for _ln in raw.splitlines():
                if _ln.startswith("## "):
                    _cur = _ln[3:].strip()
                elif _cur and "<!--" in _ln and "state:" in _ln:
                    _val = _ln.split("state:", 1)[1].replace("-->", "").strip()
                    if _val:
                        _states[_cur] = _val
            body = strip_meta(raw)
        except OSError:
            continue
        # ★ 一层可能有多个文件（本体 + 子层目录）。老路径是用 `"\n\n".join` 拼文件，
        #   所以这里的切片也得带上那个分隔符，否则"全选＝原样"在多层文件上就不成立了
        #   —— 而这个差只有逐字节比才看得出来。
        _sep = "\n\n" if fi > 0 else ""
        starts = [m.start() for m in re.finditer(r"(?m)^## ", body)]
        if not starts:
            out.append({"id": "%s::（整段）" % name, "title": "（整段）",
                        "text": _sep + body})
            continue
        lead = body[:starts[0]]
        if lead.strip():
            # ★ 层引言**不是可选项**（2026-10-26）：它是"这一层在讲什么"，
            #   勾了这层里任何一条，就得带上它 —— 否则节点会被读成没头没尾的句子。
            out.append({"id": "%s::（层引言）" % name, "title": "（层引言）",
                        "text": _sep + lead, "fixed": True})
            _sep = ""                     # 分隔符只挂一次
        for k, st in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(body)
            seg = body[st:e]
            title = seg.splitlines()[0][3:].strip()
            # ★ state（2026-10-29）：决定「这条还要不要每轮注入」。写在 key 注释里：
            #   <!-- key: xxx | state: reference -->  三态 active / reference / retired；
            #   缺省 = active（向后兼容，老节点不用改一个字）。
            #   ★ 与「归属」严格分开：树形决定「这条属于哪」，state 决定「带不带」。
            #     不用「移到更深的层」表达降级 —— 位置一旦承担两种含义，就没人解释得清。
            sec_state = _states.get(title, "active")
            out.append({"id": "%s::%s" % (name, title), "title": title,
                        "state": sec_state, "text": _sep + seg})
            _sep = ""
    # ★ 同一层里两个文件可能有同名 `##`：id 会撞。撞了加序号 —— 宁可 id 丑，
    #   也不能让两个节点共用一个 id（那样勾一个会动两个）。
    seen: dict[str, int] = {}
    for sec in out:
        n = seen.get(sec["id"], 0) + 1
        seen[sec["id"]] = n
        if n > 1:
            sec["id"] = "%s#%d" % (sec["id"], n)
    return out


def all_node_ids(root: Path) -> list[str]:
    """可以勾的节点。★ 不含「层引言」—— 它跟着层走，不是选项。"""
    ids: list[str] = []
    for name in all_layers(root):
        ids.extend(sec["id"] for sec in layer_sections(root, name)
                   if not sec.get("fixed"))
    return ids


def rules_store() -> Path:
    """勾选表落点：**仓库根**（`<repo>/.harness/rules.json`），不是记忆库根。

    ★ 这两个不是一回事（2026-10-26 踩过）：记忆库根是 `memory/`，
      而勾选表是"这个项目带哪些规则"，得跟仓库走。第一版写的时候
      写的是仓库根、读的是记忆库根，于是"切了档位没反应"，
      而文件确实被写出来了 —— 那种错查起来最费劲。
    """
    return ROOT


def mode_base_ids(cfg: dict, root: Path, mode: str) -> list[str]:
    """这个档位的**默认集**，按它的**声明**算（不看名字）：

      `auto: true`      ⇒ 全部节点（`full` 那一类）
      `layers: [...]`   ⇒ 那几层里的全部节点（`lean` 那一类）
      `layers: []`      ⇒ 空（`off` 那一类）
      都没写            ⇒ 顶层 `charter` 名单（老行为）

    ★ 为什么用"原来那几层"而不是写死一个层名（2026-10-26 修正）：
      写死 `L1.收束与验收` 在别的项目里根本不存在，lean 会变成空档 ——
      而这是个要发出去的工具，不能只在我们的记忆树上成立。

    ★ 为什么用"原来那几层"而不是写死一个层名（2026-10-26 修正）：
      写死 `L1.收束与验收` 在别的项目里根本不存在，lean 就会变成空档 ——
      而这是个要发出去的工具，不能只在我们的记忆树上成立。
      配置里 `modes.<档>.layers` 本来就写着"这一档带哪几层"，直接用它平移。
    """
    m = (cfg.get("modes") or {}).get(mode) or {}
    if m.get("auto"):
        return all_node_ids(root)
    names = m.get("layers")
    if names is None:
        names = cfg.get("charter") or []
    if not names:
        return []
    ids: list[str] = []
    for n in names:
        ids.extend(sec["id"] for sec in layer_sections(root, n)
                   if not sec.get("fixed"))
    return ids


def strip_meta(text: str) -> str:
    """去掉 `<!-- ... -->` 和 `wb-layer` frontmatter —— 那些是给维护者看的，
    进了上下文只会让模型照着元信息说话。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    return text.strip()


def drop_inactive_habits(text: str) -> tuple:
    """按 `### ` 切段，丢掉 `state:` 非 active 的段。返回 (保留文本, 丢掉的标题)。

    ★ 为什么要在消费端做（2026-10-29）：树的节点走 `layer_sections`，能按 state 过滤；
      习惯区是「整份文本直接注入」的另一条路径 —— 不在这里补一刀，就会出现
      「树能降级、习惯区不能」的不对称，而那种不对称最难被发现（看代码像是都支持）。

    ★ 先按段读 state、再剥注释：注释一剥，state 就没了。
    """
    blocks, cur = [], []
    for ln in text.split("\n"):
        if ln.startswith("### ") and cur:
            blocks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)

    keep, dropped = [], []
    for b in blocks:
        title = b[0][4:].strip() if b[0].startswith("### ") else ""
        state = "active"
        if title:
            for ln in b[:3]:
                if "state:" in ln:
                    state = ln.split("state:", 1)[1].replace("-->", "").strip()
                    break
        if title and state != "active":
            dropped.append(title)
            continue
        keep.append("\n".join(b))
    return "\n".join(keep), dropped


def build(cfg: dict | None = None, layers: list[str] | None = None,
          mode: str | None = None) -> dict:
    """拼总纲。`layers` 给了就用它（算档位成本时用），否则用当前档位的层。"""
    cfg = cfg or load_config()
    root, tried = resolve_root(cfg.get("roots") or [])
    res = {"root": str(root) if root else "", "tried": tried,
           "parts": [], "missing": [], "text": ""}
    if root is None:
        return res

    # ★ 两条路（2026-10-26）：
    #   `layers` 给了 ⇒ 老路径「整层拼」，一个字不改（回归、按层算成本都还用它）；
    #   没给 ⇒ 新路径「按节点挑」，挑哪些由 `rulenodes` 的表 + 档位默认集决定。
    if layers is not None:
        names, sel = list(layers), None
        mode_now = ""
    else:
        mode_now = mode or effective_mode(cfg)
        _mcfg0 = (cfg.get("modes") or {}).get(mode_now) or {}
        # ★ 层这一级先过一道闸（老机制：按 priority 装到 max_chars 就停），
        #   节点再在**闸内的层**里挑（2026-10-26）。
        #   为什么不一上来就按节点算上限：`priority` 是**层**的属性，
        #   按文件顺序裁节点等于把"优先级"换成"谁先写"，那是个假的名字。
        if _mcfg0.get("auto"):
            names = auto_layers(cfg, root, int(_mcfg0.get("max_chars") or 0))
        else:
            names = all_layers(root)
        _keep = set(RN.effective(mode_now, rules_store(), all_node_ids(root),
                                 mode_base_ids(cfg, root, mode_now)))
        _inside = {sec["id"] for n in names for sec in layer_sections(root, n)}
        sel = _keep & _inside
    chunks = []
    # ★ 习惯区（跨项目）排在最前（2026-10-26）。
    #   它写的是"默认怎么做"，项目层写的是"这个项目必须怎么做" —— 更具体的赢，
    #   所以标题上明写"可被下面的覆盖"。不写这句，模型会把两边当同一优先级，
    #   然后在冲突时随便挑一个。
    try:
        from . import habits as HAB
        from .memory_providers import default_active
        _hb = HAB.read() if default_active() else dict(text='',missing=False)
    except Exception as _e:
        _hb = {"text": "", "missing": True, "hint": "习惯区没读出来：%s" % _e}
    if _hb.get("text"):
        res["parts"].append({"name": "习惯（跨项目）", "chars": _hb.get("chars") or 0,
                             "files": [Path(_hb.get("path") or "").name]})
        # ★ 习惯自己的 `## 组` / `### 叶子` 要降一级（2026-10-26）：
        #   不然它们和外层那句「## 习惯（跨项目，可被下面的项目规则覆盖）」同级，
        #   于是"下面"既指习惯组、又指项目规则，模型分不清边界在哪。
        #   先降深的再降浅的，否则 `###` 会被连降两级。
        # ★ 按 state 过滤（2026-10-29）：reference / retired 不进注入。
        #   习惯区是整段注入，过滤只能在这里做（树那边由 layer_sections 负责）。
        _hb_text, _hb_dropped = drop_inactive_habits(_hb["text"])
        if _hb_dropped:
            res["habits_inactive"] = _hb_dropped
        # ★ 剥掉主题键注释（2026-10-29）：习惯区也带 <!-- key: ... -->，
        #   那是给查重工具看的，不是给模型看的；不剥会原样进注入（树那边本来就剥了）。
        _ht = re.sub(r"<!--.*?-->", "", _hb_text, flags=re.S)
        _ht = re.sub(r"(?m)^### ", "#### ", _ht)
        _ht = re.sub(r"(?m)^## ", "### ", _ht)
        chunks.append("## 习惯（跨项目，可被下面的项目规则覆盖）\n%s" % _ht)
    elif _hb.get("missing"):
        # ★ 没有习惯区不是错，但要说一句 —— 否则"没注入"和"注入了空的"看起来一样。
        res["habits_missing"] = _hb.get("hint", "")

    for name in names:
        secs = layer_sections(root, name)
        # ★ state 过滤（2026-10-29）：reference / retired 不进注入。
        #   放在「层内全集」这一步，两条路径（整层拼 / 按节点挑）都自动生效，不用各写一遍。
        _all = secs
        secs = [x for x in secs if x.get("state", "active") == "active"]
        if not secs and _all:
            res.setdefault("all_inactive", []).append(name)   # 不是「缺层」，是整层都不注入
            continue
        if not secs:
            res["missing"].append(name)
            continue
        if sel is None:
            keep = secs
        else:
            keep = [x for x in secs if x["id"] in sel]
            if keep:
                # 层引言跟着走（见 `layer_sections` 里那段）
                keep = [x for x in secs
                        if x["id"] in {y["id"] for y in keep} or x.get("fixed")]
        if not keep:
            continue
        body = ("".join(x["text"] for x in keep) if len(keep) == len(secs)
                else "\n\n".join(x["text"].strip() for x in keep))
        res["parts"].append({"name": name, "chars": len(body),
                             "files": sorted({f.name for f in layer_files(root, name)})})
        chunks.append("## %s\n%s" % (name, body))
    res["text"] = "\n\n".join(chunks)
    res["mode"] = mode_now
    res["nodes"] = sorted(sel) if sel is not None else []
    return res


def budget(cfg: dict, used: int) -> dict:
    limit = int(cfg.get("context_limit") or DEFAULT_LIMIT)
    ratio = float(cfg.get("charter_ratio") or DEFAULT_RATIO)
    cap = int(limit * ratio)
    return {"limit": limit, "ratio": ratio, "cap": cap, "used": used,
            "pct_ctx": (100.0 * used / limit) if limit else 0.0,
            "pct_cap": (100.0 * used / cap) if cap else 0.0,
            "over": used > cap,
            "left": max(0, cap - used)}


def plan(res: dict, cfg: dict) -> list[dict]:
    """按体量从大到小，算「丢掉这一层能省多少、剩多少」。

    这是软上限的配套：报警之后要给人一个能做决定的东西，
    只说"超了"等于没说 —— 人得知道丢哪个。
    """
    b = budget(cfg, len(res["text"]))
    running = b["used"]
    rows = []
    for p in sorted(res["parts"], key=lambda x: -x["chars"]):
        if running <= b["cap"]:
            break
        running -= p["chars"]
        rows.append({"name": p["name"], "chars": p["chars"], "after": running,
                     "pct_after": 100.0 * running / b["limit"] if b["limit"] else 0.0})
        if running <= b["cap"]:
            break
    return rows


def wrap(text: str) -> str:
    return ("【本轮约束 · 由 harness 每轮注入，不是用户说的话】\n%s\n"
            "【约束结束，以下才是本轮的实际内容】" % text)


# ───────────────────── 轮内扩容：模型只能申请，批不批在 harness ─────────────────────

class CharterState:
    """本轮总纲 + 预算。**唯一的写入口是 `try_add`，而它要过预算检查。**

    为什么不给模型一个"直接改总纲"的工具
        它会把整库拉进来 —— 预算当场失效，而且是在最需要约束的时候失效。
        所以模型只能**申请**：说清要哪层、为什么。批不批由这里按字符算。

    为什么用字符不精确算 token
        精确算要在调模型之后才拿得到 usage，那时候钱已经花了。
        字符是花之前就能算的。误差靠比例留余量吃掉。
    """

    def __init__(self, text: str, cap: int, root: Path | None, limit: int = 0):
        self.text = text
        self.cap = cap
        self.root = root
        self.limit = limit
        self.granted: list[dict] = []
        self.refused: list[dict] = []

    def used(self) -> int:
        return len(self.text)

    def remaining(self) -> int:
        return self.cap - self.used()

    def try_add(self, layer: str, why: str = "") -> dict:
        """申请把一层并进总纲。返回 {ok, chars, remaining, msg}，不抛异常。"""
        if self.root is None:
            return {"ok": False, "msg": "记忆库的根不可达，这次加不了。别当成加上了。"}

        files = layer_files(self.root, layer)
        if not files:
            return {"ok": False,
                    "msg": "树里没有这一层：%s。先确认层名，别猜。" % layer}

        body = "\n\n".join(strip_meta(f.read_text(encoding="utf-8", errors="replace"))
                           for f in files)
        chunk = "## %s\n%s" % (layer, body)
        need = len(chunk)
        left = self.remaining()

        if need > left:
            # ★ 拒绝也要给东西：差多少、能不能靠丢别的腾出来。只说"不行"等于没说。
            self.refused.append({"layer": layer, "need": need, "left": left, "why": why})
            return {"ok": False, "need": need, "remaining": left,
                    "msg": ("没批。这一层要 %d 字符，只剩 %d，差 %d。\n"
                            "  ⇒ 别试图绕过预算。要么用完现有的再申请，"
                            "要么让人跑 bin/charter.py plan 看该丢哪层。"
                            % (need, left, need - left))}

        self.text = (self.text + "\n\n" + chunk).strip()
        self.granted.append({"layer": layer, "chars": need, "why": why})
        return {"ok": True, "chars": need, "remaining": self.remaining(),
                "msg": "批了：%s，%d 字符。从下一轮起一直带着。剩余预算 %d。"
                       % (layer, need, self.remaining())}


# ───────────────────── 档位：不是开关，是"这一档花多少" ─────────────────────

def active_mode(cfg: dict) -> str:
    return cfg.get("active_mode") or "full"


_META_RE = re.compile(r"<!--\s*wb-layer(.*?)-->", re.S)


def layer_meta(root: Path, name: str) -> dict:
    """读一层自述的元数据（priority / kind / reads_when）。

    ★ 为什么现在才读：这些字段一直写在每个层文件开头，而选层逻辑从来没看过 ——
      结果是「名单」和「层自己的声明」双向脱节：
        L1.记忆维护 priority 15、写着「任何会话开始」，却没进名单；
        L3.守卫与判据 priority 45、写着「改检查器时才读」，却进了名单。
      字段写在那儿不用，等于没有。

    ★ reads_when 是自然语言，**不参与决策，只给人看**。
      机器判不了「跑具体任务时」是什么；假装能判，就是又一处「看似客观」。
    """
    for f in layer_files(root, name):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _META_RE.search(txt)
        if not m:
            continue
        out = {}
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        out["_from"] = f.name
        return out
    return {}


def all_layers(root: Path) -> list[str]:
    """树里所有**真层**。`_` 开头的是索引/术语表，它们自己写着「不算一层」。"""
    tdir = root / "tree"
    if not tdir.is_dir():
        return []
    names = ({p.stem for p in tdir.glob("*.md")}
             | {d.name for d in tdir.iterdir() if d.is_dir()})
    return sorted(n for n in names if not n.startswith("_"))


def layer_chars(root: Path, name: str) -> int:
    return sum(len(strip_meta(f.read_text(encoding="utf-8", errors="replace")))
               for f in layer_files(root, name))


def auto_layers(cfg: dict, root: Path, max_chars: int) -> list[str]:
    """按各层自述的 priority 从低到高装，**装到 max_chars 就停**。

    ★ 为什么「停」而不是「跳过继续试」：跳着装会让顺序和行为都不可预测
      （装完 15 跳过 45 又回头装 30，人看不懂为什么带了这几层）。
      宁可少带一层，也要让结果能一句话解释：按优先级，装到这为止。

    ★ priority 缺失按 999 排最后：不写就是最不重要，比猜一个中间值安全。
    """
    rows = []
    for n in all_layers(root):
        meta = layer_meta(root, n)
        try:
            pr = int(meta.get("priority") or 999)
        except ValueError:
            pr = 999
        rows.append((pr, n, layer_chars(root, n)))
    return select_layer_rows(rows, max_chars)


def select_layer_rows(rows, max_chars=0):
    """Shared by original Markdown trees and managed fact trees. Stop, never skip."""
    out: list[str] = []
    tot = 0
    for _pr, n, c in sorted(rows):
        if max_chars and tot + c > max_chars:
            break
        out.append(n)
        tot += c
    return out


def effective_mode(cfg: dict | None = None) -> str:
    """真正生效的档位。**只有一个真源：配置**（`harness/charter.json`）。

    ★ 2026-10-26 踩过：我第一版让勾选表里的 `active` 优先，于是"切了档位没反应"
      和"测试里的临时配置被全局表压住"同时出现 —— 那就是第二份真源。
      档位只此一处；勾选表只存**例外**（每个档位自己那一份）。
    """
    return active_mode(cfg or load_config())


def active_nodes(cfg: dict | None = None, root: Path | None = None,
                 mode: str | None = None) -> list[str]:
    """当前档位要带的**节点**（`层::标题`）。顺序按树，稳定。"""
    cfg = cfg or load_config()
    if root is None:
        root, _ = resolve_root(cfg.get("roots") or [])
    if root is None:
        return []
    m = mode or effective_mode(cfg)
    return RN.effective(m, rules_store(), all_node_ids(root),
                        mode_base_ids(cfg, root, m))


def active_layers(cfg: dict, root: Path | None = None) -> list[str]:
    """当前档位**涉及**的层 = 至少有一个节点被选中的层。

    ★ 2026-10-26 起它是**派生值**，不再自己决定带什么。
      底层单位变成了节点；保留这个函数是因为界面/成本表都按层展示。
    """
    if root is None:
        root, _ = resolve_root(cfg.get("roots") or [])
    if root is None:
        return []
    # ★ 以 `build` 为准（2026-10-26）：它是唯一真正拼文本的地方。
    #   自己再算一遍"哪些层"就会出现"界面说带 3 层、实际注入 2 层"。
    res = build(cfg, mode=effective_mode(cfg))
    return [p["name"] for p in res["parts"] if p["name"] != "习惯（跨项目）"]


def allow_request(cfg: dict) -> bool:
    """这一档允不允许模型申请加层。"""
    m = (cfg.get("modes") or {}).get(active_mode(cfg)) or {}
    return bool(m.get("request"))


def mode_table(cfg: dict) -> list[dict]:
    """每个档位**按真的去树里拼一遍**来算字符 —— 不是估的。

    这一点很要紧：如果这里拍一个数，用户看到的成本就是编的，
    而"开启后要多花多少"正是他做决定唯一依据。
    """
    rows = []
    _rt, _ = resolve_root(cfg.get("roots") or [])
    for name, m in (cfg.get("modes") or {}).items():
        # ★ 按**节点**真拼一遍（2026-10-26）：档位的成本必须等于它实际带的东西。
        res = build(cfg, mode=name)
        rows.append({"mode": name,
                     "layers": [p["name"] for p in res["parts"]
                                if p["name"] != "习惯（跨项目）"],
                     "nodes": len(res.get("nodes") or []),
                     "chars": len(res["text"]),
                     "files": len(res["parts"]), "request": bool(m.get("request")),
                     "note": m.get("note", "")})
    return rows


def switch_mode(cfg: dict, name: str, force: bool = False, why: str = "",
                path: Path | None = None) -> tuple[int, str, int]:
    """切档。(rc, 说明, 每轮多花多少字符)。rc: 0 切了 / 6 要确认 / 3 没这档。

    ★ 为什么这条判断要在这里、而不是各入口自己写
      现在有两个入口会切档（命令行、控制台页面）。各写一遍的话，
      以后改确认规则必然漏一边 —— 而漏的那一边是"人点一下按钮就多花钱"。
      文案可以各写各的，判断不行。

    ★ 升档要确认，和「不可逆动作先停」是同一条道理：
      花出去的钱收不回来，所以要在花之前让人知道花多少。
    """
    modes = cfg.get("modes") or {}
    if name not in modes:
        return 3, "没有这个档位：%s（可选：%s）" % (name, "、".join(modes) or "无"), 0
    old = active_mode(cfg)
    rows = {r["mode"]: r for r in mode_table(cfg)}
    new_c = rows[name]["chars"]
    old_c = rows.get(old, {}).get("chars", 0)
    delta = new_c - old_c
    if delta > 0 and not force:
        return 6, ("%s → %s 会让每轮多 %d 字符（%d → %d）"
                   % (old, name, delta, old_c, new_c)), delta
    cfg["active_mode"] = name
    save_config(cfg, path)
    log_mode(name, old, new_c, why)
    return 0, "已切到 %s：每轮 %d 字符（%s）" % (
        name, new_c, "、".join(rows[name]["layers"]) or "不带层"), delta


def save_config(cfg: dict, path: Path | None = None) -> Path:
    p = path or (ROOT / "harness" / "charter.json")
    # 保持键的书写顺序：json.dump 默认按插入序，读进来的顺序就是文件里的顺序
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def log_mode(name: str, old: str, chars: int, why: str = "") -> None:
    """切换留痕。为什么不写进树：这是**这台机器此刻的开关状态**，不是记忆。"""
    import datetime as _dt
    p = ROOT / "harness" / "mode.log"
    line = "%s  %s → %s  %d 字符  %s\n" % (
        _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), old, name, chars, why)
    with p.open("a", encoding="utf-8") as f:
        f.write(line)

