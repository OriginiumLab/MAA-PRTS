# -*- coding: utf-8 -*-
"""资源包数据分支的仓库契约：服务端生成（tools/sync_pack.py，CI 里跑）与
客户端下载（packsync.py）共用。

为什么单点：PACK_REPO / PACK_BRANCH 的默认值以前在 packsync.py 与
tools/sync_pack.py 各写一份（注释自己也承认「改名后要一起改」），漏一处
客户端就被指向不存在的分支（#63）。manifest 源顺序也一并收敛在这里：
新鲜度优先 raw → gh-proxy → jsDelivr——jsDelivr 对 gh 分支内容是长缓存且
忽略 ?v=，强推后可能持续吐旧字节，只能当最后兜底；sync_pack 生成的
包下载 URL 用同一顺序，两侧不再各排各的。

工具侧经命名空间包导入本模块（sys.path 指到仓库根后 from backend.core
import packrepo）：保持本文件只依赖标准库、import 零副作用，别往里加
对 backend 其他模块的依赖。
"""
from __future__ import annotations

import os

PACK_REPO = os.environ.get('MAA_PRTS_PACK_REPO') or 'OriginiumLab/MAA-PRTS'
PACK_BRANCH = os.environ.get('MAA_PRTS_PACK_BRANCH') or 'pack-data'
# 头像单独一个数据分支：pack.yml 每次都是全新 git init + 强推，只放三个数据
# 文件，头像要是混在 pack-data 里会被下一次资源同步整个冲掉
AVATAR_BRANCH = os.environ.get('MAA_PRTS_AVATAR_BRANCH') or 'avatar-data'

PACK_MIRRORS = (
    'https://raw.githubusercontent.com/{repo}/{branch}/',
    'https://gh-proxy.com/https://raw.githubusercontent.com/{repo}/{branch}/',
    'https://cdn.jsdelivr.net/gh/{repo}@{branch}/',
)


def mirror_urls(rel_path: str, suffix: str = '') -> list:
    """某文件在数据分支各镜像上的地址（顺序即回退顺序）"""
    return [m.format(repo=PACK_REPO, branch=PACK_BRANCH) + rel_path + suffix
            for m in PACK_MIRRORS]


def avatar_urls(rel_path: str) -> list:
    """某文件在头像数据分支各镜像上的地址（顺序即回退顺序）"""
    return [m.format(repo=PACK_REPO, branch=AVATAR_BRANCH) + rel_path
            for m in PACK_MIRRORS]
