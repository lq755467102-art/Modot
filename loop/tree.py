"""把记忆树读成"层 → 节点"的结构，给可视化用。

为什么节点是 `##` 而不是文件：
  现在的树是 4 个平铺的 .md，每个里面有若干 `##` 小节 —— 那才是真的"节点"。
  `L1.`/`L3.` 只是**文件名前缀**，不是层级（`all_layers` 是平铺扫的）。
  所以可视化只能按标题解析，不然画出来是 4 个方块。

为什么分化不用改读的逻辑：
  `layer_files()` 本来就支持 `tree/<层名>/` 子目录（子层算进父层）。
  所以"新叶子"有两个落点，两种都合法：
    ① 在层文件里加一个 `## 标题`（默认，简单）
    ② 建 `tree/<层名>/NN.标题.md`（要独立成文件时）
  这里默认走 ①。
"""
# 主要引用方包括 `tools.py` 和 `proposals.py`。
from __future__ import annotations

from pathlib import Path


def _section_nodes(body: str, fname: str) -> list[dict]:
    """按 `##` 切节点。`#` 那一级是层标题，不算节点。"""
    nodes: list[dict] = []
    cur: dict | None = None
    lead = 0
    for line in body.splitlines():
        if line.startswith("## "):
            if cur:
                nodes.append(cur)
            cur = {"title": line[3:].strip(), "chars": 0, "from": fname}
            continue
        if cur is None:
            lead += len(line)          # 第一个 ## 之前的引言
        else:
            cur["chars"] += len(line)
    if cur:
        nodes.append(cur)
    if not nodes:
        # 整段没有 ## ⇒ 它自己就是一个节点（别丢，丢了这个层就是空的）
        nodes = [{"title": "（整段）", "chars": lead, "from": fname}]
    elif lead > 40:
        nodes.insert(0, {"title": "（层引言）", "chars": lead, "from": fname})
    return nodes


def outline(root: Path) -> dict:
    """返回 {layers: [...], total_chars, total_nodes}。

    每一层带 nodes（`##` 小节）。字数用 strip_meta 之后的长度 ——
    和注入时的计量口径一致，不然界面上显示的字数和实际注入的对不上。
    """
    from loop import charter as CH

    if root is None:
        return {"layers": [], "total_chars": 0, "total_nodes": 0}
    tdir = Path(root) / "tree"
    if not tdir.is_dir():
        return {"layers": [], "total_chars": 0, "total_nodes": 0}

    layers: list[dict] = []
    total_chars = 0
    total_nodes = 0
    for name in CH.all_layers(root):
        files = CH.layer_files(root, name)
        if not files:
            continue
        nodes: list[dict] = []
        chars = 0
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            body = CH.strip_meta(text)
            chars += len(body)
            nodes.extend(_section_nodes(body, f.name))
        layers.append({
            "layer": name,
            "title": name.split(".", 1)[1] if "." in name else name,
            "chars": chars,
            "files": [f.name for f in files],
            "nodes": nodes,
        })
        total_chars += chars
        total_nodes += len(nodes)
    # 字数大的排前面（那是内容重的层）
    layers.sort(key=lambda x: -x["chars"])
    return {"layers": layers, "total_chars": total_chars,
            "total_nodes": total_nodes}


def add_node(root: Path, layer: str, title: str, body: str = "") -> dict:
    """在某层加一个 `## 标题` 节点。

    ★ 这是**改树正文** —— 树每轮都注入，写脏一条就是每轮都被带偏。
      所以：只允许人主动建（界面上手写标题），机器不自动调它。
      这和「机器不写教训」是同一条。
    """
    from loop import charter as CH

    if root is None:
        return {"ok": False, "error": "树的根没找到"}
    name = (layer or "").strip()
    if name not in CH.all_layers(root):
        return {"ok": False, "error": "没有这一层：%s" % name}
    t = (title or "").strip().replace("\n", " ")
    if not t:
        return {"ok": False, "error": "节点标题不能空"}
    if len(t) > 40:
        return {"ok": False, "error": "标题太长了（%d 字），节点标题是一句话" % len(t)}

    f = Path(root) / "tree" / ("%s.md" % name)
    if not f.exists():
        return {"ok": False, "error": "层文件不存在：%s" % f.name}
    text = f.read_text(encoding="utf-8", errors="replace")
    if ("## " + t) in text:
        return {"ok": False, "error": "这一层已经有同名节点了：%s" % t}

    add = "\n## %s\n\n%s\n" % (t, (body or "").strip() or "（待写）")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(add)
    return {"ok": True, "file": f.name, "title": t,
            "note": "树变了 ⇒ 下一轮任务开工前注入的就是新树，不用重启"}


def set_node(root: Path, layer: str, title: str, body: str = "") -> dict:
    """把某层里一个 `## 标题` 节点的**正文**换掉；节点不存在就加一个。

    ★ 为什么不复用 `add_node`：`add_node` 遇到同名是**拒**的。
      而"改一条已有规则"是个正当需求（规则写错了、写窄了），拒掉只会逼人去手改文件 ——
      那条路径**没有备份、没有审计**，比这个函数危险得多。
      所以这里给一条受控的路：只改正文，不动标题、不动别的节点。

    ★ 边界：正文在 `## 标题` 与下一个 `## `（或文件尾）之间。
      标题行本身一个字都不动 —— 节点 id 是 `层::标题`，动了标题等于换了一个节点，
      而勾选表、提案去重、审计记录全都按 id 对，标题一动它们就全对不上了。
    """
    from loop import charter as CH

    if root is None:
        return {"ok": False, "error": "树的根没找到"}
    name = (layer or "").strip()
    if name not in CH.all_layers(root):
        return {"ok": False, "error": "没有这一层：%s" % name}
    t = (title or "").strip().replace("\n", " ")
    b = (body or "").strip()
    if not t:
        return {"ok": False, "error": "节点标题不能空（要新建就写标题）"}
    if len(b) < 10:
        return {"ok": False, "error": "正文太短（%d 字）—— 改完之后人要看懂它管什么" % len(b)}

    f = Path(root) / "tree" / ("%s.md" % name)
    if not f.exists():
        return {"ok": False, "error": "层文件不存在：%s" % f.name}
    text = f.read_text(encoding="utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").split("\n")
    head = None
    for i, ln in enumerate(lines):
        if ln.strip() == "## " + t:
            head = i
            break
    if head is None:
        r = add_node(root, name, t, b)
        if r.get("ok"):
            r["created"] = True
        return r
    end = len(lines)
    for j in range(head + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    # 保留原节点前面那几个空行/尾随空行，只换中间的内容
    # noqa: R4 —— 改的是记忆树的 markdown，不是 Python 源码。
    #   它自己的判据是"命中数必须为 1"：`head is None` 时走 add_node、不走这里。
    new = lines[:head + 1] + ["", b, ""] + lines[end:]
    f.write_text("\n".join(new), encoding="utf-8")
    return {"ok": True, "file": f.name, "title": t, "created": False,
            "note": "节点正文换了（标题没动 —— 动了标题就等于换了个节点）"}

