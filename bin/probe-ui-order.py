# 拆文件之后特有的一种错：**顶层就调用了一个"后面文件里才定义"的函数**。
#
# 为什么会踩（2026-10-27 真冒烟逮到）：函数声明提升只在**同一个脚本**内有效。
# 拆成多个 <script src> 之后，`on('mself', ...)` 这种顶层绑定如果写在 panels.js，
# 而 `function on()` 还在 main.js，那页面一打开就是 ReferenceError ——
# 而且只报一次，后面的初始化全断，人看到的是"界面怎么没反应"。
#
# 静态就能查：把"项目自己定义的函数/常量"按文件顺序排，
# 任何**顶层**对它们的调用都不许出现在定义之前。
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "desktop" / "renderer"


def load_order():
    """按 index.html 里的引用顺序取脚本（不许写死，顺序本身就是被检查的东西）。"""
    html = (D / "index.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="(js/[^"]+)"', html)


def main():
    order = load_order()
    if not order:
        print("index.html 里没有外链脚本 —— 前端是不是又合回单文件了？")
        return 1
    texts = [(n, (D / n).read_text(encoding="utf-8")) for n in order]

    defined = {}
    for i, (name, text) in enumerate(texts):
        for m in re.finditer(r"^(?:async\s+)?function\s+([\w$]+)\s*\(", text, re.M):
            defined.setdefault(m.group(1), i)
        for m in re.finditer(r"^(?:const|let|var)\s+([\w$]+)\s*=", text, re.M):
            defined.setdefault(m.group(1), i)

    bad = []
    for i, (name, text) in enumerate(texts):
        for m in re.finditer(r"^([\w$]+)\s*\(", text, re.M):
            fn = m.group(1)
            if fn in defined and defined[fn] > i:
                line = text[:m.start()].count("\n") + 1
                bad.append("%s:%d 顶层调用 `%s()`，但它定义在后面的 %s"
                           % (name, line, fn, order[defined[fn]]))

    print("脚本加载顺序：" + " → ".join(order))
    if bad:
        print("\n顺序依赖错误 %d 处：" % len(bad))
        for b in bad[:20]:
            print("  " + b)
        return 1
    print("没有「提前使用后面文件里的函数」（共查了 %d 个顶层定义）" % len(defined))
    return 0


if __name__ == "__main__":
    sys.exit(main())
