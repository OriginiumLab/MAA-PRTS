# -*- coding: utf-8 -*-
"""账号配置：读取 config/*.json 账号文件、内置 token 同步"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import threading

from . import settings, tasks, updater
from . import paradox

ELITE_CN = {0: '精零', 1: '精一', 2: '精二'}


def _accounts_dir():
    return settings.accounts_dir()


def _load(path):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def list_accounts():
    out = []
    d = _accounts_dir()
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith('.json') or name.startswith(('_', '作业站')):
            continue
        if name in updater.NON_ACCOUNT_JSON:      # 同层的全局文件，不是账号
            continue
        snap = _load(os.path.join(d, name))
        if not snap or not (snap.get('account') or {}).get('uid'):
            continue
        a, s = snap.get('account') or {}, snap.get('stats') or {}
        out.append({
            'file': name,
            'uid': str(a.get('uid')),
            'nickName': a.get('nickName') or name,
            'alias': a.get('alias') or '',
            'channelName': a.get('channelName') or '',
            'level': a.get('level'),
            'secretary': a.get('secretary') or {},
            'maaProfile': (snap.get('maa') or {}).get('profile') or '',
            'maaDir': (snap.get('maa') or {}).get('dir') or '',
            'syncedAt': (snap.get('sync') or {}).get('syncedAt'),
            'stats': {
                'charCount': s.get('charCount'),
                'charTotal': s.get('charTotal'),
                'missingCount': s.get('missingCount'),
                'sixStarCount': s.get('sixStarCount'),
                'sixStarTotal': s.get('sixStarTotal'),
                'e2Count': s.get('e2Count'),
                'mastery3Count': s.get('mastery3Count'),
                'moduleCount': s.get('moduleCount'),
                'byRarity': s.get('byRarity') or {},
            },
        })
    # 排序：settings.accountOrder（长按拖动保存的手动序）里的按其顺序在前，
    # 没排过的按默认序（干员数降序、昵称）跟在后面；排序列表里的幽灵文件名忽略
    order = {f: i for i, f in enumerate(settings.load().get('accountOrder') or [])}
    out.sort(key=lambda r: (order.get(r['file'], 1 << 30),
                            -(r['stats']['charCount'] or 0), r['nickName']))
    return out


def get_account(file: str) -> dict:
    path = os.path.join(_accounts_dir(), os.path.basename(file))
    snap = _load(path)
    if not snap:
        raise FileNotFoundError('账号文件不存在：%s' % file)
    return snap


def account_detail(file: str) -> dict:
    snap = get_account(file)
    a, s = snap.get('account') or {}, snap.get('stats') or {}
    chars = []
    for c in snap.get('chars') or []:
        chars.append({
            'charId': c.get('charId'),
            'name': c.get('name'),
            'rarity': c.get('rarity'),
            'profession': c.get('profession'),
            'subProfession': c.get('subProfession'),
            'elite': c.get('elite'),
            'level': c.get('level'),
            'potential': c.get('potential'),
            'skillLevel': c.get('skillLevel'),
            'mastery': max([int(x.get('mastery') or 0) for x in (c.get('skills') or [])] or [0]),
            'modules': len(c.get('modules') or []),
            'favor': c.get('favor'),
        })
    chars.sort(key=lambda r: (-(r['rarity'] or 0), -(r['elite'] or 0), -(r['level'] or 0), r['name'] or ''))
    return {
        'file': file,
        'account': a,
        'stats': s,
        'missing': snap.get('missing') or [],
        'chars': chars,
        'maa': snap.get('maa') or {},
        'sync': snap.get('sync') or {},
        'paradox': paradox.plan(snap)['stats'],
    }


def save_account(file: str, snap: dict) -> None:
    """把整份账号快照写回磁盘（原子替换）"""
    settings.ensure_dirs()
    path = os.path.join(_accounts_dir(), os.path.basename(file))
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_maa_meta(file: str, patch: dict) -> dict:
    """把 MAA 配置名等用户字段写回账号 json"""
    settings.ensure_dirs()
    path = os.path.join(_accounts_dir(), os.path.basename(file))
    snap = _load(path)
    if not snap:
        raise FileNotFoundError(file)
    maa = snap.setdefault('maa', {})
    for k, v in (patch or {}).items():
        maa[k] = v
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return maa


# ---------------------------------------------------------------- token 存储
# R4/R5：token 密文随各账号 json 的 skd 段存放（{alias, addedAt, data}，
# data = base64(DPAPI(token))，与该账号绑定的 maa.dir 同处一个文件），
# 密钥绑定本机 + 当前 Windows 用户，拷到别的电脑无法解密。
# 旧版全局 config/tokens.json 只读兜底，不再写入；密文格式与迁移说明见
# docs/config.md。

from . import vault  # noqa: E402


def _migrate_txt_if_any():
    """旧版明文 tokens.txt（现落 config/ 本层）→ 加密 tokens.json

    先写 tokens.json 成功、再把原 txt 改名 .bak——中途任何一步失败，
    原数据都还在；txt 读不了（编码坏等）则改名 .broken 留证并报错。
    """
    txt = os.path.join(settings.CONFIG_DIR, 'tokens.txt')
    path = settings.tokens_file()
    if not os.path.isfile(txt) or os.path.isfile(path):
        return
    try:
        items = [{'alias': alias or '', 'token': token}
                 for alias, token, _out, _line in updater.parse_tokens_file(txt)
                 if token]
    except Exception:
        os.replace(txt, txt + '.broken')
        raise ValueError('旧 tokens.txt 读取失败（编码损坏？），已改名为 %s，'
                         '请手动检查。' % os.path.basename(txt + '.broken'))
    _save_tokens(items)
    os.replace(txt, txt + '.bak')


def _save_tokens(items: list):
    """逐条加密落盘：文件里能看到有几条、备注是什么，token 本身是密文。

    新布局下只在 tokens.txt 迁移时写 config/tokens.json（迁移产物），
    平时不再有别的写它的地方。
    """
    path = settings.tokens_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    env = {
        'version': 2,
        'cipher': 'dpapi',
        'note': 'Windows DPAPI(CurrentUser) 逐条加密：仅本机当前 Windows 用户可解，拷到别的电脑无效。',
        'tokens': [
            {
                'alias': (e.get('alias') or '').strip(),
                'addedAt': e.get('addedAt') or '',
                'data': base64.b64encode(
                    vault.protect((e.get('token') or '').strip().encode('utf-8'))
                ).decode('ascii'),
            }
            for e in items if (e.get('token') or '').strip()
        ],
    }
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(env, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def load_tokens() -> list:
    """读出 token 条目 [{"alias","token","addedAt","data"}]。

    来源 = 各账号 json 的 skd 段 ∪ 旧版 config/tokens.json 兜底，
    按明文 token 去重（账号文件优先）。单条解不开只跳过并把受影响的
    文件记进 TOKENS_WARNINGS，不让整库失败——密文绑定本机当前用户，
    换电脑后旧条目解不开是常态，不能拖死其他账号的同步。
    """
    global TOKENS_WARNINGS
    items, seen, warns = [], set(), []

    def add(alias, token, added_at, data):
        t = (token or '').strip()
        if t and t not in seen:
            seen.add(t)
            items.append({'alias': (alias or '').strip(), 'token': t,
                          'addedAt': added_at or '', 'data': data or ''})

    d = _accounts_dir()
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if not name.endswith('.json') or name.startswith(('_', '作业站')):
                continue
            if name in updater.NON_ACCOUNT_JSON:
                continue
            skd = (_load(os.path.join(d, name)) or {}).get('skd')
            if not isinstance(skd, dict) or not skd.get('data'):
                continue
            try:
                token = vault.unprotect(
                    base64.b64decode(skd.get('data') or '')).decode('utf-8')
            except Exception:
                warns.append('%s 里的 token 密文无法解密（换电脑/换用户后需重新粘贴）' % name)
                continue
            add(skd.get('alias'), token, skd.get('addedAt'), skd.get('data'))

    _migrate_txt_if_any()
    path = settings.tokens_file()
    txt = os.path.join(settings.CONFIG_DIR, 'tokens.txt')
    if os.path.isfile(txt) and os.path.isfile(path):
        # 并存时迁移按「json 已在就不动」跳过，txt 里的 token 永不参与同步
        # ——必须点名警告让用户知道少同步了，不能静默
        warns.append('tokens.txt 与 tokens.json 并存：txt 未迁移、里面的 token '
                     '不参与同步（文件原样保留未动）；如需并入请在界面重新添加，'
                     '确认后删除 txt')
    if os.path.isfile(path):
        env = _load(path) or {}
        if env.get('cipher') == 'dpapi' and isinstance(env.get('tokens'), list):
            for i, ent in enumerate(env['tokens'], 1):
                try:
                    token = vault.unprotect(
                        base64.b64decode(ent.get('data') or '')).decode('utf-8')
                except Exception:
                    warns.append('tokens.json 第 %d 条无法解密：密钥绑定本机与当前 '
                                 'Windows 用户，换电脑 / 换用户后需要重新粘贴 token。' % i)
                    continue
                add(ent.get('alias'), token, ent.get('addedAt'), ent.get('data'))
        else:
            warns.append('%s 不是有效的加密 token 文件，已忽略' % os.path.basename(path))
    TOKENS_WARNINGS = warns
    return items


# 启动自检把读不开的条目原因存这里，main 起来后写进 app.log
TOKENS_WARNING = ''
# load_tokens 每次重算：单条解密失败被跳过的受影响文件列表
TOKENS_WARNINGS = []


def ensure_tokens() -> list:
    """启动自检：读出全部可用 token。

    坏条目逐个跳过（TOKENS_WARNING 点名受影响文件），不再改名/重建
    任何文件——账号数据本身还在，重新同步一次就恢复了。
    """
    global TOKENS_WARNING
    TOKENS_WARNING = ''
    try:
        items = load_tokens()
    except ValueError as e:
        # 只剩 tokens.txt 迁移失败的留证报错（文件已改名 .broken，数据不丢）
        items = []
        TOKENS_WARNING = '%s。请手动检查后，在界面里重新添加账号。' % e
    if TOKENS_WARNINGS:
        TOKENS_WARNING = (TOKENS_WARNING + '；' if TOKENS_WARNING else '') + '；'.join(TOKENS_WARNINGS)
    return items


# ---------------------------------------------------------------- 刷新
def _run_updater(tid, token=None, alias=None, with_skins=False):
    """内置同步（不依赖外部 python 脚本，可直接打包进 exe）"""
    log = tasks.log_factory(tid)

    # 让 skland 把重试提示也打到界面日志里
    updater.skland.set_logger(log)

    if token:
        # token 密文随同步成功的账号文件落盘（R4）。DPAPI 不可用（罕见）
        # 就跳过加密，不影响同步本身——失败时 token 不落盘，界面里还能重试
        skd_meta = None
        try:
            skd_meta = {
                'alias': alias or '',
                'addedAt': updater.now_iso(),
                'data': base64.b64encode(
                    vault.protect(token.encode('utf-8'))).decode('ascii'),
            }
        except Exception as e:
            log('[提示] token 无法加密保存（%s），本次同步结果里不携带 token' % e)
        return updater.sync_one(token, alias or '', settings.accounts_dir(),
                                log, with_skins=with_skins,
                                cache_path=settings.subprof_cache(),
                                skd_meta=skd_meta)
    return updater.sync_many(load_tokens(), settings.accounts_dir(),
                             log, with_skins=with_skins,
                             cache_path=settings.subprof_cache())


def start_refresh(token=None, alias=None, with_skins=False, title=None):
    """启动后台同步，返回任务 id（前端轮询 /api/task）"""
    title = title or ('同步账号 ' + (token[:6] + '…' if token else '（全部）'))
    holder = {}

    def go():
        try:
            res = _run_updater(holder['tid'], token, alias, with_skins)
            rows = list_accounts()
            if isinstance(res, dict) and res.get('fail'):
                tasks.finish(holder['tid'], result={'accountCount': len(rows)},
                             error='%d 个 token 同步失败（看日志）' % res['fail'])
                return
            newest = None
            if token and isinstance(res, dict) and res.get('files'):
                newest = res['files'][0]['file']
            elif rows:
                newest = max(rows, key=lambda r: r.get('syncedAt') or '')['file']
            tasks.finish(holder['tid'], result={'accountCount': len(rows), 'newest': newest})
        except Exception as e:
            import traceback
            tasks.finish(holder['tid'], error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    tid = tasks.create(title, None)
    holder['tid'] = tid
    threading.Thread(target=go, daemon=True, name='sync').start()
    return tid


def remove_account(file: str, backup=True):
    path = os.path.join(_accounts_dir(), os.path.basename(file))
    if not os.path.isfile(path):
        raise FileNotFoundError(file)
    if backup:
        bak_dir = os.path.join(os.path.dirname(path), '_removed')
        os.makedirs(bak_dir, exist_ok=True)
        shutil.move(path, os.path.join(bak_dir, os.path.basename(path)))
    else:
        os.remove(path)
    return True
