#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diagnose.py（命令行）—— 打一个脱敏的现场包

出了问题时，人需要的是「一份能发给别人看的东西」。
现在最省事的做法是把整个仓库拷走 —— 里面有接口钥匙、模型 key、全部会话文本。
这个脚本只收摘要，全部先脱敏再落盘，产出后再拿真实密钥搜一遍。

用法
    python bin/diagnose.py                 # 打包，打印路径 + 十几行摘要
    python bin/diagnose.py --no-logs       # 不带日志尾（要发给外部时用）
    python bin/diagnose.py --dir           # 不压 zip，留目录自己看
    python bin/diagnose.py --json          # 只输出摘要 JSON（给别的程序吃）

★ 它不接受「给我一个路径我帮你打包」这种参数 —— 那等于开一个泄漏用户的输入框。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loop import diagnose as DG     # noqa: E402


if __name__ == "__main__":
    sys.exit(DG.main(sys.argv[1:]))
