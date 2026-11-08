# -*- coding: utf-8 -*-
"""整理 resources/oper-weights.json 的键顺序，方便手改权重后归位

做的事：
  1. 非 0 权重的干员提到开头，按权重从大到小排（同分保持原相对顺序）
  2. 权重为 0 的干员跟在后面，保持原有顺序
  3. 按原格式（4 空格缩进、非 ASCII 不转义）写回，条数不变

用法：
    python tools/sort_oper_weights.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_PATH = os.path.join(ROOT, 'resources', 'oper-weights.json')


def main() -> int:
    with open(WEIGHTS_PATH, encoding='utf-8') as f:
        raw = f.read()
    items = json.loads(raw, object_pairs_hook=list)

    non_zero = sorted(((k, v) for k, v in items if v != 0), key=lambda kv: -kv[1])
    zero = [(k, v) for k, v in items if v == 0]

    out = json.dumps(dict(non_zero + zero), ensure_ascii=False, indent=4)
    if raw.endswith('\n'):
        out += '\n'
    with open(WEIGHTS_PATH, 'w', encoding='utf-8', newline='') as f:
        f.write(out)

    print('non-zero: %d | zero: %d | total: %d' % (len(non_zero), len(zero), len(items)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
