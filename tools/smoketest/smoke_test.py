# -*- coding: utf-8 -*-
"""MAA-PRTS 冒烟测试：不碰真实用户数据，全部在临时目录里跑

用法：
    .venv\\Scripts\\python.exe tools\\smoketest\\smoke_test.py
    python tools/smoketest/smoke_test.py     # 需已装运行依赖

三组检查，任何一组失败都以非 0 退出码结束：

    A. 静态检查
       - 全仓库 .bat 必须 UTF-8 无 BOM + CRLF 行尾（错编码会导致标签错位；
         代码实际遍历仓库里所有 .bat，不止根目录三个）
       - backend / tools 全部 .py 可编译
    B. 加密与 token 存储（隔离临时目录，monkeypatch 路径）
       - DPAPI 往返、密文篡改必报错
       - token 密文随账号文件 skd 段：跨文件去重、备注保留、不进 API 视图
       - 旧明文 tokens.txt（三种写法）自动迁移
       - 坏密文逐条跳过 + TOKENS_WARNING 点名文件（不再改名/重建）
       - 旧布局 config/skland 压平迁移（同名跳过、搬空才删、幂等）
       - MAA 干员识别 JSON（新 JObject / 旧裸数组）解析成练度结构
       - 作业 json 交给 MAA 前的修正（非法样本必须被修成 MaaCore 能解析的样子）
    C. 启动冒烟（临时项目副本）
       - backend/main.py --no-window 拉起，解析实际端口
       - 只读接口 /api/ping、/api/bootstrap、/api/gamedata 返回 200
       - bootstrap 报告的账号数据路径在 config/ 本层，config/skland 不再被创建
       - 结束后副本根目录除 config/ 与 debug/ 外零新增（防乱丢文件）
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 冒烟用的只读接口
PROBE_API = ('/api/ping', '/api/bootstrap', '/api/gamedata')


class Report:
    def __init__(self):
        self.fails = 0

    def check(self, name, fn):
        try:
            fn()
        except Exception as e:
            self.fails += 1
            print('[FAIL] %s：%s: %s' % (name, type(e).__name__, e))
        else:
            print('[ ok ] %s' % name)


# ---------------------------------------------------------------- A. 静态检查
def check_bat_encoding():
    # 仓库内全部 .bat 一并检查（含 tools/setup，新加脚本自动覆盖）
    bats = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in ('.venv', 'build', '.git', '__pycache__')]
        bats += [os.path.join(dirpath, f) for f in filenames if f.lower().endswith('.bat')]
    for path in sorted(bats):
        raw = open(path, 'rb').read()
        name = os.path.relpath(path, REPO)
        assert not raw.startswith(b'\xef\xbb\xbf'), '%s 带 UTF-8 BOM' % name
        assert b'\n' not in raw.replace(b'\r\n', b''), '%s 存在裸 LF 行尾' % name


def check_py_compile():
    roots = [os.path.join(REPO, 'backend'), os.path.join(REPO, 'tools')]
    files = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            files += [os.path.join(dirpath, f) for f in filenames if f.endswith('.py')]
    for f in files:
        py_compile.compile(f, doraise=True)


# ---------------------------------------------------------------- B. 加密与 token 存储
def _isolate_data_root():
    """进程起手就把 MAA_PRTS_DATA_ROOT 指到临时目录（#62）。

    core.settings 在 import 时就执行 _migrate_legacy_*，此时模块级 ROOT 若是
    真实用户数据目录（%LOCALAPPDATA% 下的 dev 根），迁移会碰真实用户数据——
    与文件头「不碰真实用户数据」的承诺相悖。必须在任何 `from core import ...`
    之前设好环境变量，让首次 import 就落在隔离根上（C 组子进程也继承它）。
    """
    root = tempfile.mkdtemp(prefix='maaprts-smoke-root-')
    os.environ['MAA_PRTS_DATA_ROOT'] = root
    return root


def _patch_settings(tmp):
    """把 core.settings 的数据路径指到临时目录。

    R4/R5 之后 tokens_file() / _migrate_txt_if_any / _migrate_legacy_layout
    读的都是模块级 CONFIG_DIR / LEGACY_SKLAND_DIR，三个全局必须一起指过去，
    否则 fixture 写进 tmp、代码却去读写真实仓库的 config/。
    SETTINGS_PATH 也一并指过去（B16 要写 settings 验证手动排序，
    不能在真实仓库留文件），顺手清掉 load() 的内存缓存。
    """
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from core import settings
    settings.CONFIG_DIR = os.path.join(tmp, 'config')
    settings.ACCOUNTS_DIR = settings.CONFIG_DIR
    settings.LEGACY_SKLAND_DIR = os.path.join(tmp, 'config', 'skland')
    settings.SETTINGS_PATH = os.path.join(tmp, 'config', 'settings.json')
    settings._cache = None
    return settings


def _reset_config(tmp):
    """每个 B 检查前清空临时 config，互相看不见对方的账号文件/token 库。

    顺手清 settings._cache：不清的话缓存还带着上一个检查保存的设置，
    当前断言恰好不受影响，属潜伏坑（#62）。"""
    cfg = os.path.join(tmp, 'config')
    if os.path.isdir(cfg):
        shutil.rmtree(cfg)
    os.makedirs(cfg, exist_ok=True)
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from core import settings
    settings._cache = None


def check_vault_roundtrip():
    from core import vault
    blob = vault.protect('冒烟测试 hello!@#'.encode('utf-8'))
    assert vault.unprotect(blob).decode('utf-8') == '冒烟测试 hello!@#'


def check_vault_tamper():
    from core import vault
    blob = bytearray(vault.protect(b'smoke'))
    blob[-1] ^= 0xFF
    try:
        vault.unprotect(bytes(blob))
    except OSError as e:
        m = re.search(r'错误码 (\d+)', str(e))
        assert m and int(m.group(1)) != 0, '错误码不可靠：%s' % e
        return
    raise AssertionError('篡改密文未被检出')


def check_tokens_bad_txt(tmp):
    """畸形（非 UTF-8）旧 txt：迁移失败要留证并给出警告，不能无声丢数据"""
    from core import accounts
    _reset_config(tmp)
    cfg = os.path.join(tmp, 'config')
    with open(os.path.join(cfg, 'tokens.txt'), 'wb') as f:
        f.write(b'\xb1\xea\xd7\xa2:SMOKE_BAD\n')  # GBK 的「备注:」，非法 UTF-8
    items = accounts.ensure_tokens()
    assert items == [], '畸形 txt 不应产出条目'
    assert os.path.isfile(os.path.join(cfg, 'tokens.txt.broken')), '坏 txt 未留证 .broken'
    assert '读取失败' in accounts.TOKENS_WARNING, '警告缺失：%s' % accounts.TOKENS_WARNING


def check_tokens_in_accounts(tmp):
    """R4：token 密文随账号文件 skd 段；跨文件按明文去重、备注保留、不进 API 视图"""
    from core import accounts, vault

    def skd(token, alias):
        return {'skd': {'alias': alias, 'addedAt': '2026-11-05T07:40:00',
                        'data': base64.b64encode(
                            vault.protect(token.encode('utf-8'))).decode('ascii')}}

    cfg = os.path.join(tmp, 'config')
    _reset_config(tmp)
    for name, snap in (('A.json', {'account': {'uid': '1'}, **skd('SMOKE_A', '大号')}),
                       ('B.json', {'account': {'uid': '2'}, **skd('SMOKE_B', '小号')}),
                       ('C.json', {'account': {'uid': '3'}, **skd('SMOKE_A', '重复')})):
        with open(os.path.join(cfg, name), 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
    # 同层的全局文件不是账号，skd 检索必须跳过（写个坏的进去验证不被读）
    with open(os.path.join(cfg, 'tokens.json'), 'w', encoding='utf-8') as f:
        json.dump({'cipher': 'not-dpapi'}, f)

    items = accounts.load_tokens()
    got = [(e['alias'], e['token']) for e in items]
    assert got == [('大号', 'SMOKE_A'), ('小号', 'SMOKE_B')], got
    assert items[0]['addedAt'] == '2026-11-05T07:40:00'
    assert items[0]['data'], '密文要随条目透传（sync_many 复用，避免重复加密）'
    # 落盘文件里 token 必须是密文，且 skd 不得进 API 视图
    raw = open(os.path.join(cfg, 'A.json'), encoding='utf-8').read()
    assert 'SMOKE_A' not in raw, 'token 明文泄漏'
    assert 'skd' not in json.dumps(accounts.account_detail('A.json'), ensure_ascii=False), \
        'skd 段不得随 API 响应外泄'


def check_tokens_migration(tmp):
    import io as _io
    from core import accounts
    cfg = os.path.join(tmp, 'config')
    _reset_config(tmp)
    with _io.open(os.path.join(cfg, 'tokens.txt'), 'w', encoding='utf-8') as f:
        f.write('# 注释\n'
                '大号:SMOKE_M1\n'
                '小号:SMOKE_M2:小号#1 官服.json\n'
                'SMOKE_M3\n')
    items = accounts.load_tokens()
    got = [(e['alias'], e['token']) for e in items]
    assert got == [('大号', 'SMOKE_M1'), ('小号', 'SMOKE_M2'), ('', 'SMOKE_M3')], got
    assert os.path.isfile(os.path.join(cfg, 'tokens.json')), '未生成加密 tokens.json'
    assert os.path.isfile(os.path.join(cfg, 'tokens.txt.bak')), '原 txt 未留 .bak'
    env = json.load(open(os.path.join(cfg, 'tokens.json'), encoding='utf-8'))
    assert env['version'] == 2 and env['cipher'] == 'dpapi'
    assert all('SMOKE_' not in (t.get('data') or '') for t in env['tokens']), 'token 明文泄漏'
    # tokens.txt 与 tokens.json 并存：txt 静默跳过必须变成点名警告（文件保留）
    with _io.open(os.path.join(cfg, 'tokens.txt'), 'w', encoding='utf-8') as f:
        f.write('并存:SMOKE_BOTH\n')
    items = accounts.load_tokens()
    assert all(e['token'] != 'SMOKE_BOTH' for e in items), '并存 txt 不该被偷偷并入'
    assert os.path.isfile(os.path.join(cfg, 'tokens.txt')), '并存时 txt 必须原样保留'
    assert any('tokens.txt' in w and '并存' in w for w in accounts.TOKENS_WARNINGS), \
        '并存场景缺警告：%s' % accounts.TOKENS_WARNINGS


def check_tokens_bad_skd(tmp):
    """R4：账号文件里坏密文 → 该条跳过、警告点名文件，其余可用，无 .broken 改名"""
    from core import accounts, vault
    cfg = os.path.join(tmp, 'config')
    _reset_config(tmp)
    good = {'skd': {'alias': '好', 'addedAt': '',
                    'data': base64.b64encode(
                        vault.protect(b'SMOKE_OK')).decode('ascii')}}
    bad = {'skd': {'alias': '坏', 'addedAt': '',
                   'data': base64.b64encode(b'garbage').decode('ascii')}}
    for name, snap in (('Good.json', {'account': {'uid': '1'}, **good}),
                       ('Bad.json', {'account': {'uid': '2'}, **bad})):
        with open(os.path.join(cfg, name), 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
    items = accounts.ensure_tokens()
    assert [e['token'] for e in items] == ['SMOKE_OK'], '坏条目不该拖死好条目'
    assert 'Bad.json' in accounts.TOKENS_WARNING, '警告应点名受影响文件：%s' % accounts.TOKENS_WARNING
    assert not any(n.endswith('.broken') for n in os.listdir(cfg)), \
        '新布局下不该再有任何 .broken 改名：%s' % os.listdir(cfg)


# ---------------------------------------------------------------- B13 旧布局迁移
def check_layout_migration(settings, tmp):
    """R5：旧布局 config/skland/ 压平进 config/ 本层（同名跳过、搬空才删、幂等）"""
    cfg = os.path.join(tmp, 'config')
    old = os.path.join(cfg, 'skland')
    _reset_config(tmp)
    for rel in ('accounts/A.json', 'accounts/作业站/x.json', 'accounts/_removed/b.json',
                'tokens.json', 'subprof_cache.json'):
        p = os.path.join(old, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'marker': rel}, f, ensure_ascii=False)

    settings._migrate_legacy_layout()
    for rel in ('A.json', '作业站/x.json', '_removed/b.json',
                'tokens.json', 'subprof_cache.json'):
        assert os.path.isfile(os.path.join(cfg, *rel.split('/'))), '没搬到位：%s' % rel
    assert json.load(open(os.path.join(cfg, 'tokens.json'), encoding='utf-8'))['marker'] \
        == 'tokens.json', '搬过去的文件内容不该变'
    assert not os.path.isdir(old), '搬空的 config/skland 应被删除'

    # 幂等：目录已不存在，再跑一遍什么也不发生
    settings._migrate_legacy_layout()
    assert os.path.isfile(os.path.join(cfg, 'A.json'))

    # 同名冲突：config 已有 A.json → 旧文件原地保留、绝不覆盖，目录非空不删
    os.makedirs(os.path.join(old, 'accounts'), exist_ok=True)
    with open(os.path.join(old, 'accounts', 'A.json'), 'w', encoding='utf-8') as f:
        json.dump({'marker': 'old'}, f)
    with open(os.path.join(cfg, 'A.json'), 'w', encoding='utf-8') as f:
        json.dump({'marker': 'new'}, f)
    settings._migrate_legacy_layout()
    assert json.load(open(os.path.join(cfg, 'A.json'), encoding='utf-8'))['marker'] == 'new', \
        '同名文件被覆盖了——这会直接丢用户账号数据'
    assert os.path.isfile(os.path.join(old, 'accounts', 'A.json')), '冲突文件应留在原地'
    assert os.path.isdir(old), '还有数据没搬完的 config/skland 不该被删'


# ---------------------------------------------------------------- B14 MAA 干员 JSON
def check_operbox_parse(tmp):
    """R3：MAA 干员识别 JSON（新 JObject / 旧裸数组）→ 账号练度结构映射

    fixture 按实测真实格式构造：顶层恰 done/own_opers/source/syncTime 四键，
    local 条目字段集恰 {id,name,elite,level,own,potential,rarity}（name 来自
    OCR 可能非中文）；一图流条目才有 mainSkillLevel/skills/equips。
    """
    import tempfile as _tf
    from core import copilot, operbox, resources, updater

    table = resources.char_table() or {}
    by_rarity = {}
    for cid, c in table.items():
        if isinstance(c, dict) and c.get('name'):
            by_rarity.setdefault(int(c.get('rarity') or 0) + 1, (cid, c['name']))
    six, five = by_rarity[6], by_rarity[5]
    skill_id = next(iter(resources.skill_table() or {}), 'skchr_xxx_1')
    equip_id = next((eid for eid in (resources.equip_table() or {})
                     if copilot._module_type(eid) in copilot._SLOT), None)
    assert equip_id, '资源包里找不到槽位型（X/Y/A/D）模组，无法构造样本'

    def entry(cid, name, elite, level, rarity, **kw):
        e = {'id': cid, 'name': name, 'elite': elite, 'level': level,
             'own': True, 'potential': 1, 'rarity': rarity}
        e.update(kw)
        return e

    local = {                      # 本机 MAA 实测形态（source=local）
        'done': True, 'source': 'local', 'syncTime': '2026-11-01T11:20:13.3592439+08:00',
        'own_opers': [
            entry(six[0], 'TITI-OCR', 2, 90, 6, potential=6),   # OCR 名非中文
            dict(entry(five[0], 'x', 1, 60, 5), own=False),     # 未拥有 → 跳过
            entry('char_9999_zzz', '新干员', 2, 90, 6),         # 资源表查不到 → 跳过
        ],
    }
    yituliu = {                    # 一图流形态：才有技能/专精/模组
        'done': True, 'source': 'yituliu', 'syncTime': '2026-11-01T12:00:00+08:00',
        'own_opers': [entry(five[0], 'x', 2, 80, 5, potential=2, mainSkillLevel=7,
                            skills=[{'id': skill_id, 'level': 3}],
                            equips=[{'id': equip_id, 'type': 'X', 'level': 3}])],
    }

    built = operbox.build_chars(local, updater.Tables(cache_path=None))
    assert built['skipped'] == 2 and len(built['chars']) == 1, (built['skipped'], len(built['chars']))
    c = built['chars'][0]
    assert c['charId'] == six[0]
    assert c['name'] == six[1], '应以 id 查表取名，OCR 名只能作回退：%r' % c['name']
    assert c['rarity'] == 6 and c['elite'] == 2 and c['level'] == 90 and c['potential'] == 6
    assert c['profession'], '职业该从资源表补上'
    assert c['skillLevel'] == 0 and c['skills'] == [] and c['modules'] == [], \
        'local 数据没有技能/模组，必须如实置空，不许编造'
    assert any('本地识别不含技能' in w for w in built['warnings']), built['warnings']
    assert any('char_9999_zzz' in w for w in built['warnings']), '查不到的 id 要计入 warnings'

    built2 = operbox.build_chars(yituliu, updater.Tables(cache_path=None))
    c2 = built2['chars'][0]
    assert c2['skillLevel'] == 7
    assert c2['skills'] and c2['skills'][0]['mastery'] == 3 and c2['skills'][0]['name']
    assert c2['modules'] and c2['modules'][0]['level'] == 3

    # 旧版（2025-05-29 前）裸数组形态 load() 也要认；非 JSON 要报可读错误
    with _tf.TemporaryDirectory() as td:
        p = os.path.join(td, 'OperBoxData.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(local['own_opers'], f, ensure_ascii=False)
        data = operbox.load(p)
        assert data['source'] == 'local' and len(data['own_opers']) == 3
        assert data['syncTime'] is None
        with open(p, 'w', encoding='utf-8') as f:
            f.write('not-json{')
        try:
            operbox.load(p)
        except ValueError as e:
            assert 'own_opers 为空' in str(e)
        else:
            raise AssertionError('非 JSON 未报错')

    # 映射结果要能直接喂给练度匹配：结构同构、放行/拦截语义正确
    owned = copilot.build_owned({'chars': built['chars'] + built2['chars']})
    o6 = owned[copilot.norm(six[1])]
    assert set(o6) == {'name', 'rarity', 'elite', 'level', 'mainSkillLevel',
                       'masteryLevels', 'moduleLevels', 'skillKnown'}, set(o6)
    assert copilot.req_met({'name': six[1], 'skill': 2,
                            'requirements': {'elite': 2, 'level': 60}}, o6), \
        '精二满级的干员该放行 elite/level 要求'
    assert copilot.req_met({'name': six[1], 'skill': 2,
                            'requirements': {'skill_level': 9}}, o6), \
        'local 数据没有技能维度，按约定只比等级、专精要求不再拦下（详见 B17）'
    o5 = owned[copilot.norm(five[1])]
    assert o5['masteryLevels'] == [3] and o5['moduleLevels'], '一图流的专精/模组没映射过去'
    assert copilot.req_met({'name': five[1], 'skill': 1,
                            'requirements': {'skill_level': 9}}, o5), '专三该放行专精要求'

    # import_into：done=false（识别中断的残缺名单）整单拒绝，绝不能覆盖现有练度
    _reset_config(tmp)
    cfg = os.path.join(tmp, 'config')
    old_char = {'charId': six[0], 'name': six[1], 'rarity': 6, 'elite': 0, 'level': 1,
                'favor': 122, 'skin': {'id': 's1', 'name': '皮'}, 'skinCount': 1}
    with open(os.path.join(cfg, '甲.json'), 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '1'}, 'chars': [old_char], 'skins': [{'id': 's1'}],
                   'stats': {}, 'missing': [], 'sync': {}}, f, ensure_ascii=False)
    maa = os.path.join(tmp, 'maa-sample')
    os.makedirs(os.path.join(maa, 'data'), exist_ok=True)

    def write_operbox(done):
        with open(os.path.join(maa, 'data', 'OperBoxData.json'), 'w', encoding='utf-8') as f:
            json.dump({'done': done, 'source': 'local', 'syncTime': None,
                       'own_opers': local['own_opers']}, f, ensure_ascii=False)

    write_operbox(False)
    try:
        operbox.import_into('甲.json', maa)
    except ValueError as e:
        assert '未完成' in str(e), e
    else:
        raise AssertionError('done=false 的残缺名单不该被导入')
    snap = json.load(open(os.path.join(cfg, '甲.json'), encoding='utf-8'))
    assert snap['chars'] == [old_char], '拒绝导入时不能动现有练度'

    # done=true 正常导入：皮肤/信赖按 charId 沿用旧值，与保留的 skins 列表不打架
    write_operbox(True)
    res = operbox.import_into('甲.json', maa)
    assert res['done'] is True, '摘要要带 done 供界面展示'
    snap = json.load(open(os.path.join(cfg, '甲.json'), encoding='utf-8'))
    c = {x['charId']: x for x in snap['chars']}[six[0]]
    assert c['favor'] == 122 and c['skin'] == {'id': 's1', 'name': '皮'} \
        and c['skinCount'] == 1, 'MAA 没有的皮肤/信赖该沿用旧值，而不是清零'
    assert snap['skins'] == [{'id': 's1'}] and snap['stats']['skinCount'] == 1

    # 森空岛同步来的练度不许被识别结果顶掉：识别结果每个 MAA 目录只有一份
    # （当时登录的那个号），批量加号时挨个导入会让所有账号练度一样（2026-11-15）
    tok_snap = {'account': {'uid': '2', 'nickName': '乙'},
                'chars': [dict(old_char, charId=six[0], level=80)],
                'stats': {'charCount': 1},
                'sync': {'source': 'zonai.skland.com/api/v1/game/player/info'}}
    with open(os.path.join(cfg, '乙.json'), 'w', encoding='utf-8') as f:
        json.dump(tok_snap, f, ensure_ascii=False)
    r = operbox.import_into('乙.json', maa)
    assert r.get('skippedBy') == 'token' and r.get('imported') == 0, \
        'token 同步的练度该被拦下，实际：%r' % r
    assert r.get('reason'), '跳过要给界面一句人话原因'
    after = json.load(open(os.path.join(cfg, '乙.json'), encoding='utf-8'))
    assert after['chars'] == tok_snap['chars'], '拦下时不能动现有练度'
    # 只存了 token、没写 sync.source 的老账号同样受保护（有 token 就是能同步的号）
    with open(os.path.join(cfg, '丙.json'), 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '3'}, 'chars': [dict(old_char, level=70)],
                   'stats': {}, 'skd': {'data': 'x'}}, f, ensure_ascii=False)
    assert operbox.import_into('丙.json', maa).get('skippedBy') == 'token'
    # 账号页手动导入（用户确认过）带 force，必须照旧能覆盖
    r = operbox.import_into('乙.json', maa, force=True)
    assert r['imported'] == 1 and 'skippedBy' not in r, 'force 必须放行：%r' % r
    after = json.load(open(os.path.join(cfg, '乙.json'), encoding='utf-8'))
    assert after['chars'][0]['charId'] == six[0] and after['sync']['source'] == 'maa-operbox'


# ---------------------------------------------------------------- B15 从 MAA 直接建号
def check_create_from_maa(tmp):
    """向导方式二：不用森空岛 token，用 MAA 识别结果直接建本地账号

    建出的账号要能被 list_accounts 认出（合成 uid + 昵称取目录名/备注），
    不写 skd 段（load_tokens 不把它当森空岛账号），重名顺延 (2)(3)，
    done=false 残缺名单整单拒绝。
    """
    from core import accounts, operbox, resources

    table = resources.char_table() or {}
    cid, name = next(((k, v['name']) for k, v in table.items()
                      if isinstance(v, dict) and v.get('name')), ('char_x', 'x'))
    maa = os.path.join(tmp, 'MAA-fake')
    os.makedirs(os.path.join(maa, 'data'), exist_ok=True)

    def write_operbox(done):
        with open(os.path.join(maa, 'data', 'OperBoxData.json'), 'w', encoding='utf-8') as f:
            json.dump({'done': done, 'source': 'local', 'syncTime': '2026-11-06T12:00:00+08:00',
                       'own_opers': [{'id': cid, 'name': name, 'elite': 2, 'level': 90,
                                      'own': True, 'potential': 0, 'rarity': 6}]},
                      f, ensure_ascii=False)

    _reset_config(tmp)
    write_operbox(True)
    r = operbox.create_from_dir(maa, '')
    assert r['imported'] == 1 and r['file'] == 'MAA-fake.json', r['file']
    snap = accounts.get_account(r['file'])
    assert snap['account']['uid'].startswith('local-'), snap['account']
    assert snap['account']['nickName'] == 'MAA-fake', '没给备注时昵称应取 MAA 目录名'
    assert snap['account']['channelName'] == '本地导入'
    assert 'skd' not in snap, '本地账号不该有 skd 段'
    assert snap['maa']['dir'].endswith('MAA-fake'), snap['maa']
    assert snap['chars'] and snap['chars'][0]['charId'] == cid
    assert snap['stats']['charCount'] == 1
    assert any(x['file'] == r['file'] for x in accounts.list_accounts()), '新建账号没进账号列表'
    # 无 skd 段 → 不产生任何 token（不参与森空岛同步）
    assert accounts.load_tokens() == [], '本地账号不该被当成森空岛账号'

    # 重名顺延：同目录再建一个 → MAA-fake(2).json；带备注则用备注命名
    r2 = operbox.create_from_dir(maa, '')
    assert r2['file'] == 'MAA-fake(2).json', r2['file']
    r3 = operbox.create_from_dir(maa, '小号')
    assert r3['file'] == '小号.json', r3['file']
    snap3 = accounts.get_account(r3['file'])
    assert snap3['account']['nickName'] == '小号' and snap3['account']['alias'] == '小号'

    # done=false（识别中断）整单拒绝，不落任何文件
    write_operbox(False)
    try:
        operbox.create_from_dir(maa, '')
    except ValueError as e:
        assert '未完成' in str(e), e
    else:
        raise AssertionError('done=false 的残缺名单不该建号')
    assert not os.path.isfile(os.path.join(tmp, 'config', 'MAA-fake(3).json'))


# ---------------------------------------------------------------- B16 账号手动排序
def check_account_order(tmp):
    """账号列表排序：settings.accountOrder（长按拖动保存的顺序）优先，
    没排过的账号按默认序（干员数降序、昵称）跟在后面；幽灵文件名忽略
    """
    from core import accounts, settings

    _reset_config(tmp)
    cfg = os.path.join(tmp, 'config')

    def mk(name, uid, count):
        with open(os.path.join(cfg, name), 'w', encoding='utf-8') as f:
            json.dump({'account': {'uid': uid, 'nickName': name[:-5]},
                       'stats': {'charCount': count}}, f, ensure_ascii=False)

    mk('甲.json', '1', 10)
    mk('乙.json', '2', 20)
    mk('丙.json', '3', 5)
    names = [r['file'] for r in accounts.list_accounts()]
    assert names == ['乙.json', '甲.json', '丙.json'], names      # 默认：干员数降序

    settings.save({'accountOrder': ['丙.json', '乙.json']})
    names = [r['file'] for r in accounts.list_accounts()]
    assert names == ['丙.json', '乙.json', '甲.json'], names      # 手动序在前

    # 排序列表里的幽灵文件名排序时忽略；非 .json 的脏值被清洗掉
    settings.save({'accountOrder': ['乙.json', 'ghost.json', 42]})
    names = [r['file'] for r in accounts.list_accounts()]
    assert names == ['乙.json', '甲.json', '丙.json'], names
    assert settings.load()['accountOrder'] == ['乙.json', 'ghost.json'], \
        settings.load()['accountOrder']


# ------------------------------------------------- B17 MAA 导入账号只比等级
def check_maa_import_level_only():
    """MAA 干员识别导入的账号没有技能/专精/模组数据。

    用户定的妥协：这类账号只比精英化与等级——
      v1 老作业：按星级补默认等级，不再要求技能；
      v2 作业：只认 requirements 里的 elite/level，skill_level 与 module 不判。
    有技能数据的账号（森空岛同步、一图流来源）行为必须保持不变。
    """
    from core import copilot, resources

    table = resources.char_table() or {}
    by_stars = {}
    for c in table.values():
        if isinstance(c, dict) and c.get('name'):
            by_stars.setdefault(int(c.get('rarity') or 0) + 1, c['name'])

    def snap(name, stars, elite, level, with_skills):
        c = {'charId': 'x', 'name': name, 'rarity': stars, 'elite': elite,
             'level': level, 'skillLevel': 7, 'favor': 0, 'skin': None}
        if with_skills:
            c['skills'] = [{'id': 's1', 'name': '', 'mastery': 3},
                           {'id': 's2', 'name': '', 'mastery': 0},
                           {'id': 's3', 'name': '', 'mastery': 0}]
            c['modules'] = [{'id': 'uniequip_002_x1', 'name': '', 'level': 3}]
        else:                                   # MAA 本地识别的形态
            c['skills'] = []
            c['modules'] = []
        return {'chars': [c]}

    six, five = by_stars[6], by_stars[5]
    maa = copilot.build_owned(snap(six, 6, 2, 60, with_skills=False))
    o = maa[copilot.norm(six)]
    assert o['skillKnown'] is False, 'MAA 导入的干员应标 skillKnown=False'

    skl = copilot.build_owned(snap(six, 6, 2, 60, with_skills=True))
    assert skl[copilot.norm(six)]['skillKnown'] is True, '有技能数据的账号不能放宽'

    # v2 作业：技能/模组要求一律不判（改动前这里全是 False）
    for req in ({'elite': 2, 'level': 60, 'skill_level': 9},
                {'elite': 2, 'level': 60, 'skill_level': 7},
                {'elite': 2, 'level': 60, 'module': 3}):
        assert copilot.req_met({'name': six, 'skill': 2, 'requirements': req}, o), \
            'MAA 导入号不该被技能/模组要求拦下：%r' % req
    # 但等级照旧要卡
    assert not copilot.req_met({'name': six, 'skill': 2,
                                'requirements': {'elite': 2, 'level': 61}}, o), \
        '等级不够必须拦下'
    low = copilot.build_owned(snap(six, 6, 1, 60, with_skills=False))
    assert not copilot.req_met({'name': six, 'skill': 2,
                                'requirements': {'elite': 2, 'level': 60}},
                               low[copilot.norm(six)]), \
        '精英阶段不够必须拦下（精一 60 不满足精二 60）'

    # v1 作业：按星级补等级默认，且不带技能要求
    for stars, want in ((6, (2, 60)), (5, (1, 60)), (4, (1, 60)),
                        (3, (1, 55)), (2, (0, 30)), (1, (0, 30))):
        name = by_stars[stars]
        req = copilot.effective_req({'name': name, 'skill': 1}, stars, strict_skill=False)
        assert (req['elite'], req['level']) == want, \
            '%d 星默认等级应为 %r，实际 %r' % (stars, want, (req['elite'], req['level']))
        assert 'skill_level' not in req and 'module' not in req, \
            '没有技能数据时不该再要求技能/模组：%r' % req
        assert req.get('implied') is True, '默认要求仍要带 implied 标记'

    maa_five = copilot.build_owned(snap(five, 5, 1, 60, with_skills=False))
    assert copilot.req_met({'name': five, 'skill': 1}, maa_five[copilot.norm(five)]), \
        '五星精一 60 该满足 v1 默认'
    maa_low = copilot.build_owned(snap(five, 5, 0, 55, with_skills=False))
    assert not copilot.req_met({'name': five, 'skill': 1}, maa_low[copilot.norm(five)]), \
        '五星精零 55 不该满足 v1 默认（精一 60）'

    # 有技能数据的账号：行为保持原样（专精 0 该被专三要求拦下）
    assert not copilot.req_met({'name': six, 'skill': 2,
                                'requirements': {'skill_level': 9}},
                               skl[copilot.norm(six)]), \
        '有技能数据的账号不能被顺手放宽'
    assert copilot.effective_req({'name': six, 'skill': 1}, 6)['skill_level'] == 9, \
        'v1 默认对正常账号仍要含专三'

    # 详情副本：带账号时展示与判定一致，不带账号时保持原样
    content = {'opers': [{'name': six, 'skill': 2,
                          'requirements': {'elite': 2, 'level': 60,
                                           'skill_level': 9, 'module': 3}}]}
    dop = copilot.detail_opers(content, maa)
    # 展示副本的合并要求键与搜索侧统一为 'req'（#63）；'requirements' 是
    # 原始作业内容的字段，保留不动
    assert 'skill_level' not in dop[0]['req'], dop[0]['req']
    assert 'module' not in dop[0]['req'], dop[0]['req']
    assert dop[0]['skillKnown'] is False
    assert copilot.detail_opers(content, skl)[0]['req']['skill_level'] == 9
    assert copilot.detail_opers(content)[0]['req']['skill_level'] == 9, \
        '不带账号上下文时不该改变展示'
    v1 = {'opers': [{'name': six, 'skill': 1}]}      # v1 条目：本来没有 requirements
    copilot.detail_opers(v1, maa)
    assert 'requirements' not in v1['opers'][0], '展示副本不能污染原始 content'

    # 搜索用的 detail 也要如实反映（前端据此显示「只比等级」提示）
    mode, missing, training, _total, detail, _six, _w = copilot.match_operation(content, maa)
    assert mode == 'ready' and not training, (mode, training)
    assert detail[0]['skillKnown'] is False
    assert 'skill_level' not in detail[0]['req'], detail[0]['req']


def _assert_maa_parsable(content):
    """按 MaaCore CopilotConfig::parse 的致命校验复检，非法就抛 AssertionError

    对应 src/MaaCore/Config/Miscellaneous/CopilotConfig.cpp：
      parse_basic_info  读 stage_name
      parse_oper_usage  skill / skill_level / module 推出的精英要求 > 声明的 elite 直接判错
      parse_actions     Click 必须给 rect 或 location，Swipe 必须给 begin 与 end
    """
    from core import copilot

    assert content.get('stage_name'), 'stage_name 缺失'
    idx = copilot._char_index()
    opers = list(content.get('opers') or [])
    for g in (content.get('groups') or []):
        opers.extend(g.get('opers') or [])
    for op in opers:
        hit = idx.get(op.get('name') or '')
        if not hit:                       # MAA 查不到干员时跳过检查
            continue
        need = max(int(op.get('skill') or 0) - 1, 0)
        req = op.get('requirements') or {}
        sl = req.get('skill_level')
        if sl is not None:
            need = max(2 if int(sl) > 7 else (1 if int(sl) > 4 else 0), need)
        mod = req.get('module')
        if mod is not None and int(mod) > 0:
            need = max(2, need)
        if 'elite' in req and need > int(req.get('elite') or 0):
            raise AssertionError('干员 %s 需要精%d，但作业写的是精%s'
                                 % (op.get('name'), need, req.get('elite')))
    for a in (content.get('actions') or []):
        if a.get('type') == 'Click' and 'rect' not in a and 'location' not in a:
            raise AssertionError('Click 动作缺 rect/location')
        if a.get('type') == 'Swipe' and ('begin' not in a or 'end' not in a):
            raise AssertionError('Swipe 动作缺 begin/end')


def check_copilot_normalize():
    """作业站原始 json 常带非法练度要求，MaaCore 会 parse failed（作业集整体起不来）。

    这里造一份必然被 MaaCore 拒收的样本，断言 normalize_for_maa 能修成合法，
    并且修的是副本、不动调用方手里那份。
    """
    from core import copilot, resources

    chars = list((resources.char_table() or {}).values())
    low = next((c['name'] for c in chars if int(c.get('rarity') or 0) == 0), None)
    mid = next((c['name'] for c in chars if int(c.get('rarity') or 0) == 3), None)
    assert low and mid, '资源包里找不到一星/四星干员，无法构造样本'

    content = {
        'stage_name': 'smoke_01',
        'opers': [
            # 四星、技能 1、技能等级 7 → 需要精一，但写的是精 0（真实踩过的坑）
            {'name': mid, 'skill': 1, 'requirements': {'elite': 0, 'level': 45, 'skill_level': 7}},
            # 一星，没有技能 1
            {'name': low, 'skill': 1},
        ],
        'groups': [
            # 技能专精九级 → 需要精二
            {'name': 'g1', 'opers': [
                {'name': mid, 'skill': 2, 'requirements': {'elite': 0, 'skill_level': 9}}]},
        ],
        'actions': [
            {'type': 'Deploy', 'name': mid, 'location': [1, 2], 'direction': 'Right'},
            {'type': 'Skill', 'name': mid, 'location': [1, 2]},          # 名与坐标同时给
            {'type': 'Click'},                                           # 缺 rect/location
            {'type': 'Swipe', 'begin': [1, 2]},                          # 缺 end
            {'type': 'Click', 'rect': [0, 0, 10, 10], 'location': [1, 2]},
            {'type': 'UnknownType'},                                     # Core 直接跳过
        ],
        'doc': {'title': 'smoke'},
    }

    try:
        _assert_maa_parsable(content)
    except AssertionError:
        pass
    else:
        raise AssertionError('样本本身应当是 MaaCore 不接受的，测试失去意义')

    fixed, notes = copilot.normalize_for_maa(content)
    assert notes, '修正过程没有留下任何说明'
    _assert_maa_parsable(fixed)

    assert fixed['opers'][0]['requirements']['elite'] == 1, fixed['opers'][0]
    assert fixed['opers'][1]['skill'] == 0, '一星的技能 1 应被清成 0'
    assert fixed['groups'][0]['opers'][0]['requirements']['elite'] == 2

    types = [a.get('type') for a in fixed['actions']]
    assert types == ['Deploy', 'Skill', 'Click', 'UnknownType'], types
    skill = fixed['actions'][1]
    assert 'name' not in skill and 'role' not in skill, '名与坐标同时给时该清掉名字'
    click = fixed['actions'][2]
    assert 'rect' in click and 'location' not in click, 'rect 与 location 同时给时该只留 rect'
    assert fixed['actions'][0].get('name') == mid, 'Deploy 的干员名不该被清'

    # 深拷贝：调用方手里的原始内容不能被改
    assert content['opers'][0]['requirements']['elite'] == 0
    assert len(content['actions']) == 6


def check_copilot_difficulty():
    """difficulty 是位标志：0/缺失=普通、1=普通、2=突袭、3=通用

    写 MAA 时按标志拆条：通用作业要落「普通 + 突袭」两条，共用同一个作业文件。
    """
    from core import copilot

    cases = [(None, 1), (0, 1), (1, 1), (2, 2), (3, 3), ('2', 2), ('x', 1)]
    for raw, want in cases:
        got = copilot.difficulty_flags({'difficulty': raw})
        assert got == want, 'difficulty=%r 应为 %d，实际 %d' % (raw, want, got)
    assert copilot.difficulty_text(1) == '普通'
    assert copilot.difficulty_text(2) == '突袭'
    assert copilot.difficulty_text(3) == '通用'


def check_copilot_variants():
    """有突袭版的关卡拆两行：普通行只要普通可用的、突袭行只要突袭可用的

    通用作业（difficulty=3）两边都要出现；没有突袭版的关卡只出一行。
    同时确认关卡表里 #f# / #s 这类变体不会被当成独立关卡重复列出。
    """
    from core import copilot, resources

    stage = {'stageId': 'act13side_ex01', 'code': 'NL-EX-1', 'name': '都市怪谈',
             'activity': '长夜临光', 'raid': True}
    rows = [
        {'id': 1, 'difficulty': 1, 'mode': 'ready'},
        {'id': 2, 'difficulty': 2, 'mode': 'ready'},
        {'id': 3, 'difficulty': 3, 'mode': 'ready'},
        {'id': 4, 'difficulty': 3, 'mode': 'borrow'},
    ]
    normal, raid = copilot.stage_variants(stage, rows, limit=10)
    assert normal['stage']['variant'] == 'normal', '第一行应该是普通'
    assert raid['stage']['variant'] == 'raid', '第二行应该是突袭'
    assert [r['id'] for r in normal['ops']] == [1, 3, 4], \
        '普通行应含普通与通用作业，实际 %r' % [r['id'] for r in normal['ops']]
    assert [r['id'] for r in raid['ops']] == [2, 3, 4], \
        '突袭行应含突袭与通用作业，实际 %r' % [r['id'] for r in raid['ops']]
    assert normal['total'] == 3 and raid['total'] == 3
    assert normal['counts']['ready'] == 2 and raid['counts']['ready'] == 2

    # 没有突袭版的关卡只有一行，variant 固定 normal
    plain = copilot.stage_variants(dict(stage, raid=False), rows, limit=10)
    assert len(plain) == 1 and plain[0]['stage']['variant'] == 'normal', '无突袭版的关卡不该拆行'

    # 写 MAA 的条数由「加的时候在哪一行」决定，不是无脑按难度标志展开
    assert copilot.entry_variants(3, 'normal') == [False], '从普通行加通用作业只写普通'
    assert copilot.entry_variants(3, 'raid') == [True], '从突袭行加通用作业只写突袭'
    assert copilot.entry_variants(3, None) == [False, True], '不指定行时按难度展开'
    # 选了哪一行就写哪一行，不再拿作业自己的位标志改主意（2026-11-19 用户报
    # 「选了突袭还是只打普通」：标漏/通用位作业被静默写成 IsRaid=false）
    assert copilot.entry_variants(2, 'normal') == [False], '选了普通行就写普通'
    assert copilot.entry_variants(1, 'raid') == [True], '选了突袭行就写突袭'

    # 资源层：变体关不重复出现，主关带 raid 标记
    nl_ex1 = resources.resolve('NL-EX-1')['stages']
    assert len(nl_ex1) == 1, 'NL-EX-1 只应有一条，实际 %d' % len(nl_ex1)
    assert nl_ex1[0]['stageId'] == 'act13side_ex01' and nl_ex1[0]['raid'] is True
    # 粘 #f# 的 stage id 也要回落到主关
    assert resources.resolve('act13side_ex01#f#')['stages'][0]['stageId'] == 'act13side_ex01'


# ---------------------------------------------------------------- B12 v1 作业默认练度
def check_copilot_v1_default_req():
    """作业站 v1 老作业没有 requirements，练度要求未知。

    规则：没写练度的条目按星级补默认要求——六星精二60 + 专三（skill_level 9）、
    四五星精一60 + 技能7、三星精一55 + 技能7、一二星精零30 无技能。
    配置不够的号要被判成「需补练度」，不能再当成「无特殊要求」放行。
    详情里还要能看到补出来的默认值。
    """
    from core import copilot, resources

    table = resources.char_table() or {}
    by_stars = {}
    for c in table.values():
        if isinstance(c, dict) and c.get('name'):
            by_stars.setdefault(int(c.get('rarity') or 0) + 1, c['name'])

    def own(name, stars, elite, level, mastery=(0, 0, 0), main=7):
        return {'name': name, 'rarity': stars, 'elite': elite, 'level': level,
                'mainSkillLevel': main, 'masteryLevels': list(mastery), 'moduleLevels': {}}

    # 每个星级：默认要求 dict，以及「满足 / 差一点」的两个账号样本
    cases = [
        # stars  默认要求                          满足样本                    不满足样本
        (6, {'elite': 2, 'level': 60, 'skill_level': 9},
            (2, 60, (3, 3, 3)), (2, 60, (0, 0, 0))),      # 专精不够 → 拦下
        (5, {'elite': 1, 'level': 60, 'skill_level': 7},
            (1, 60, (0, 0, 0), 7), (1, 60, (0, 0, 0), 6)),  # 技能 6 级 → 拦下
        (4, {'elite': 1, 'level': 60, 'skill_level': 7},
            (1, 60, (0, 0, 0), 7), (0, 55, (0, 0, 0), 7)),
        (3, {'elite': 1, 'level': 55, 'skill_level': 7},
            (1, 55, (0, 0, 0), 7), (1, 54, (0, 0, 0), 7)),
        (2, {'elite': 0, 'level': 30},
            (0, 30, (0, 0, 0), 0), (0, 29, (0, 0, 0), 0)),
        (1, {'elite': 0, 'level': 30},
            (0, 30, (0, 0, 0), 0), (0, 1, (0, 0, 0), 0)),
    ]
    for stars, want, ok_args, bad_args in cases:
        name = by_stars.get(stars)
        assert name, '资源包里找不到 %d 星干员，无法构造样本' % stars
        op = {'name': name, 'skill': 1}          # v1 条目：没有 requirements
        req = copilot.effective_req(op, stars)
        for k, v in want.items():
            assert req.get(k) == v, '%d 星默认要求 %s 应为 %r，实际 %r' % (stars, k, v, req.get(k))
        assert req.get('implied') is True, '补出来的默认要求要带 implied 标记'
        assert 'requirements' not in op, 'effective_req 不能往原条目里塞字段'
        assert copilot.req_met(op, own(name, stars, *ok_args)), \
            '%s %r 应满足默认要求' % (name, ok_args)
        assert not copilot.req_met(op, own(name, stars, *bad_args)), \
            '%s %r 不该被当成可用（v1 作业不是无要求）' % (name, bad_args)

    # 专精检查认条目里的技能序号：skill=2 看第二技能的专精
    op6 = {'name': by_stars[6], 'skill': 2}
    assert not copilot.req_met(op6, own(by_stars[6], 6, 2, 60, mastery=(3, 0, 0))), \
        '二技能没专精不该放行'
    assert copilot.req_met(op6, own(by_stars[6], 6, 2, 60, mastery=(3, 3, 0)))

    # 写了练度的 v2 条目不受默认值影响
    op2 = {'name': by_stars[6], 'skill': 2, 'requirements': {'elite': 1, 'level': 1}}
    assert copilot.effective_req(op2, 6)['elite'] == 1, '明确写了要求的不能被默认覆盖'
    assert 'implied' not in copilot.effective_req(op2, 6)

    # 压级号 vs v1 作业：六星精一（无专精）+ 五星精零 → mode 应为 train，不再是 ready
    content = {'opers': [
        {'name': by_stars[6], 'skill': 2},
        {'name': by_stars[5], 'skill': 1},
    ], 'groups': []}
    owned_map = {
        copilot.norm(by_stars[6]): own(by_stars[6], 6, 1, 60),
        copilot.norm(by_stars[5]): own(by_stars[5], 5, 0, 40),
    }
    mode, missing, training, total, detail, _six, _w = copilot.match_operation(content, owned_map)
    assert not missing, '两个干员都拥有，不该判缺人'
    assert mode == 'train', '压级号打 v1 作业应判需补练度，实际 %r' % mode
    assert training == [by_stars[6], by_stars[5]], training
    assert all(d['req'].get('implied') for d in detail), 'detail 里应带默认练度要求'

    # 满练号（精二60 + 专三 / 精一60 + 技能7）打同一份 v1 作业：应为 ready
    owned_map[copilot.norm(by_stars[6])] = own(by_stars[6], 6, 2, 60, mastery=(3, 3, 3))
    owned_map[copilot.norm(by_stars[5])] = own(by_stars[5], 5, 1, 60, main=7)
    mode, missing, training, total, detail, _six, _w = copilot.match_operation(content, owned_map)
    assert mode == 'ready' and not training, '满练号打 v1 作业不该被误伤'

    # 详情弹窗用的 detail_opers：合并要求键为 'req'（与搜索侧统一，#63），
    # 也要补上默认
    dop = copilot.detail_opers(content)
    assert dop[0]['req']['elite'] == 2 and dop[0]['req']['implied']
    assert dop[0]['req']['skill_level'] == 9, '六星默认要带专三要求'
    assert 'requirements' not in content['opers'][0], '展示副本不能污染原始 content'


# ---------------------------------------------------------------- B10 悖论模拟
def check_paradox_plan():
    """悖论清单：解锁条件只看对应干员的精英化/等级

    悖论关卡是官方固定阵容，所以「能打不能打」= 这个干员的 elite/level 够不够；
    导入过的要能记住，下次默认不再勾。
    """
    from core import paradox

    t = paradox.table()
    assert t.get('count', 0) > 200, '悖论清单条数异常：%r' % t.get('count')
    for cid, e in (t.get('chars') or {}).items():
        assert e.get('stage', '').startswith('mem_'), '%s 的关卡代号不像悖论：%r' % (cid, e.get('stage'))
        assert e.get('name'), '%s 没有干员名' % cid
        assert e.get('elite') in (0, 1, 2), '%s 解锁精英化异常：%r' % (cid, e.get('elite'))

    sample = [(c, e) for c, e in sorted((t.get('chars') or {}).items()) if e['elite'] == 2]
    (a_cid, a_e), (b_cid, b_e) = sample[0], sample[1]
    snap = {'chars': [
        {'charId': a_cid, 'name': a_e['name'], 'rarity': 6, 'profession': '近卫',
         'elite': 2, 'level': 90},                        # 达标
        {'charId': b_cid, 'name': b_e['name'], 'rarity': 6, 'profession': '近卫',
         'elite': 1, 'level': 70},                        # 精英化不够
    ]}
    p = paradox.plan(snap)
    st = p['stats']
    assert st['owned'] == 2, '只应看到两个干员，实际 %r' % st
    assert st['ready'] == 1 and st['locked'] == 1, '达成判定错：%r' % st
    assert st['todo'] == 1 and st['imported'] == 0

    # 已导入标记：plan 要能读回来，且不再算进 todo
    paradox.mark_imported(snap, [a_e['stage']])
    p2 = paradox.plan(snap)
    assert p2['stats']['imported'] == 1 and p2['stats']['todo'] == 0, p2['stats']
    hit = [i for i in p2['items'] if i['stage'] == a_e['stage']][0]
    assert hit['imported'] is True and hit['importedAt'], hit


def check_maa_paradox_name():
    """写 MAA 时 Name 用可读名字，FilePath 仍按关卡代号命名"""
    src = open(os.path.join(REPO, 'backend', 'core', 'maa.py'), encoding='utf-8').read()
    assert "'Name': it.get('name') or stage_code" in src, 'maa.py 的 Name 取值被改回去了'
    assert "fname = '%s_%s.json' % (_safe(stage_code), cid)" in src, '作业文件名应仍用关卡代号'


# ---------------------------------------------------------------- C. 启动冒烟
def _copy_project(tmp):
    ignore = shutil.ignore_patterns(
        '.git', '.venv', 'venv', 'build', 'dist', 'config', 'skland',
        '__pycache__', '*.pyc', 'release')
    shutil.copytree(REPO, tmp, ignore=ignore, dirs_exist_ok=True)


def _wait_banner(proc, timeout=25):
    """从子进程输出里解析『界面 http://127.0.0.1:PORT/』"""
    import threading
    import time
    lines, ports = [], []

    def pump():
        for raw in proc.stdout:
            lines.append(raw.rstrip('\n'))
            m = re.search(r'127\.0\.0\.1:(\d+)', raw)
            if m:
                ports.append(int(m.group(1)))

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ports or proc.poll() is not None:
            break
        time.sleep(0.2)
    return (ports[0] if ports else None), lines


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def check_boot_smoke():
    tmp = tempfile.mkdtemp(prefix='maaprts-smoke-')
    try:
        _copy_project(tmp)
        # 源码模式数据根默认落 LOCALAPPDATA，冒烟必须指回临时目录：
        # 既隔离真实数据，也顺带验证副本根零新增（数据目录不许再落 cwd）。
        # data/ 先建好再拍快照，别让数据根本身算作「新增」
        data_root = os.path.join(tmp, 'data')
        os.makedirs(data_root, exist_ok=True)
        before = set(os.listdir(tmp))
        env = dict(os.environ, PYTHONIOENCODING='utf-8',
                   MAA_PRTS_DATA_ROOT=data_root)
        proc = subprocess.Popen(
            [sys.executable, '-u', 'backend/main.py', '--no-window'],
            cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace')
        try:
            port, lines = _wait_banner(proc)
            assert port, '启动未输出端口；输出：\n  ' + '\n  '.join(lines[-8:])
            base = 'http://127.0.0.1:%d' % port

            for path in PROBE_API:
                status, _body = _get(base + path)
                assert status == 200, '%s 状态码 %s' % (path, status)

            status, body = _get(base + '/api/bootstrap')
            envelope = json.loads(body.decode('utf-8'))
            assert envelope.get('ok'), 'bootstrap 返回失败：%s' % envelope.get('error')
            paths = (envelope.get('data') or {}).get('paths') or {}
            acc = (paths.get('accounts') or '').rstrip('\\/')
            assert acc.endswith('config'), '账号数据路径不对：%s' % paths.get('accounts')
            assert 'skland' not in paths, 'paths 不应再报告 skland 键'
            assert (paths.get('tokens') or '').endswith('tokens.json'), \
                'token 文件路径不对：%s' % paths.get('tokens')

            applog = os.path.join(data_root, 'debug', 'app.log')
            assert os.path.isfile(applog), '没有生成 <数据根>/debug/app.log'
            assert not os.path.isdir(os.path.join(data_root, 'config', 'skland')), \
                'config/skland 不应被预创建（新布局无此目录）'
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        # 运行前后对比：副本根一个新目录都不许有——数据走 MAA_PRTS_DATA_ROOT
        new = set(os.listdir(tmp)) - before
        assert not new, '运行后仓库根出现新增（数据没走数据根？）：%s' % sorted(new)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- B18 剧情关不进搜索
def check_story_stage_filter():
    """剧情关（stage_table 里 isStoryOnly）只有剧情没有战斗，作业站永远不会有作业。

    以前它们会混进活动展开，每个活动都顶着几行「共 0 份 / 打不了」。
    现在：新资源包靠 stage['story'] 标记；旧包没有这个字段时靠代号/id 兜底，
    两条路径都不能让剧情关出现在搜索结果里，也不能误伤真实战斗关。
    """
    from core import resources

    # 1. 兜底规则本身：三种形态都要认出来，且不影响普通关卡
    cases = [
        ({'id': 'act13side_st01', 'code': 'NL-ST-1'}, True),   # 活动剧情关
        ({'id': 'a003_st01', 'code': 'OF-ST1'}, True),         # 代号无第二个连字符
        ({'id': 'st_08-01', 'code': '10-1'}, True),            # 主线纯剧情节点
        ({'id': 'spst_08-01', 'code': 'EG-3'}, True),          # 主线剧情片段
        ({'id': 'act13side_10', 'code': 'NL-10'}, False),      # 活动战斗关
        ({'id': 'main_08-01', 'code': '8-1'}, False),          # 主线战斗关
        ({'id': 'act13side_tr01', 'code': 'NL-TR-1'}, False),  # 教学关不是剧情关
    ]
    for st, want in cases:
        got = resources._is_story(dict(st))
        assert got is want, '%r 的剧情判定应为 %s，实际 %s' % (st, want, got)

    # 2. 新包以官方字段为准，不再看代号（标了 story=false 就不该被兜底误杀）
    assert resources._is_story({'id': 'act13side_st01', 'code': 'NL-ST-1',
                                'story': False}) is False

    # 3. 实际关卡表里剧情关已经没了
    codes = [s['code'] for s in resources._stages()]
    assert codes, '资源包没加载到关卡'
    left = [c for c in codes if resources._is_story({'id': '', 'code': c})]
    assert not left, '_stages() 里还有剧情关：%s' % left[:5]

    # 4. 搜剧情关代号：给一张「剧情关」卡（mode=story），不能当成战斗关返回。
    #    以前是整条过滤掉、搜了落空；现在能看到这一关存在，但挂不了机
    for kw in ('NL-ST-1', 'EG-3'):
        r = resources.resolve(kw)
        assert r['mode'] == 'story', '搜 %s 应返回剧情卡，实际 %s' % (kw, r['mode'])
        assert r['stages'] and all(s['story'] for s in r['stages']), \
            '%s 返回了非剧情关' % kw
    r = resources.resolve('NL-10')
    assert r['mode'] == 'stage' and len(r['stages']) == 1, \
        'NL-10 是战斗关，必须还能搜到：%r' % r['mode']

    # 4.5 代号既是剧情关、又是章节前缀时（12-1 / 12-10 ~ 12-19），
    #     必须展开成章节系列，不能被剧情卡抢走
    r = resources.resolve('12-1')
    assert r['mode'] == 'activity' and len(r['stages']) > 3, \
        '12-1 应展开成第 12 章系列，实际 %s / %d 关' % (r['mode'], len(r['stages']))

    # 5. 活动展开不带剧情关，但战斗关一个不能少
    r = resources.resolve('act13side')
    assert r['mode'] == 'activity' and r['stages'], 'act13side 应展开成活动'
    assert not [s for s in r['stages'] if s['code'].startswith('NL-ST')], \
        '活动展开里还有 NL-ST：%s' % [s['code'] for s in r['stages']][:6]
    assert any(s['code'] == 'NL-EX-7' for s in r['stages']), '战斗关被误删了'


# ------------------------------------------------- B20 资源包幂等
def check_pack_idempotent():
    """自动同步的前提：同一份上游数据，任何时候生成都得到同一个包。

    以前 version 是「生成日期」、fingerprint 又把 version 算进去，
    隔天重跑就会凭空变出新指纹，全体用户白弹一次「资源已更新」。
    """
    import importlib.util

    path = os.path.join(REPO, 'resources', 'make_pack.py')
    spec = importlib.util.spec_from_file_location('mp_under_test', path)
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)

    # 1. 游戏版本 → 展示版本
    assert mp.norm_version('26-10-29-15-36-11-33649006') == '2026.10.29'
    assert mp.norm_version('2026-10-29') == '2026.10.29'
    assert mp.norm_version('') == ''

    # 2. 指纹只看内容：同内容必同指纹，换内容必变
    args = ({'char_a': {'name': 'A'}}, {'sk': 'S'}, {'eq': {'n': 1}},
            [{'id': 'main_01-07', 'code': '1-7'}], {'z': {'name': 'Z'}},
            {'act1': {'name': '活动'}})
    fp1 = mp.content_fingerprint(*args)
    fp2 = mp.content_fingerprint(*args)
    assert fp1 == fp2, '同内容生成了不同指纹：%s / %s' % (fp1, fp2)
    changed = (args[0], args[1], args[2], args[3], args[4],
               {'act1': {'name': '活动改'}})
    assert mp.content_fingerprint(*changed) != fp1, '内容变了指纹却不变'

    # 3. 仓库里这份包必须带新字段，且 version 是游戏版本而不是生成日期
    with open(os.path.join(REPO, 'resources', 'arknights.json'), encoding='utf-8') as f:
        pack = json.load(f)
    assert re.match(r'^\d{4}\.\d{2}\.\d{2}$', pack.get('version') or ''), \
        '资源包 version 格式不对：%r' % pack.get('version')
    assert re.match(r'^[0-9a-f]{8}$', pack.get('fingerprint') or ''), \
        '资源包 fingerprint 应为 8 位十六进制'
    for key in ('gameVersion', 'sourceCommit'):
        assert key in pack, '资源包缺字段 %s（自动同步依赖它）' % key

    # 4. manifest 与包对得上
    with open(os.path.join(REPO, 'resources', 'pack-manifest.json'), encoding='utf-8') as f:
        man = json.load(f)
    item = (man.get('packs') or {}).get('arknights') or {}
    assert item.get('fingerprint') == pack['fingerprint'], 'manifest 与资源包指纹不一致'
    assert item.get('sha256') and item.get('urls'), 'manifest 缺校验值或下载地址'

    # 5. 版本口径切换不能弹「更新到更旧的版本」
    from core import version as vmod
    assert vmod._is_older('2026.10.29', '2026.11.07'), '口径迁移应判成「更旧」并静默处理'
    assert not vmod._is_older('2026.11.07', '2026.10.29')
    assert not vmod._is_older('v2.3.0', '2026.10.29'), '非点分三段的版本号不该误判'


# ------------------------------------------------- B21 资源更新校验
def check_pack_verify():
    """客户端换包前的三道闸：sha256、大小、条数不许变少。

    任何一条不满足都必须拒绝落盘——半截包覆盖下去就是「资源表全空」。
    """
    import hashlib
    from core import packsync

    path = os.path.join(REPO, 'resources', 'arknights.json')
    raw = open(path, 'rb').read()
    good = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    data = packsync._verify('arknights', raw, good)
    assert data.get('counts', {}).get('chars') == json.loads(raw)['counts']['chars']

    # 校验值不对
    bad = dict(good, sha256='f' * 64)
    try:
        packsync._verify('arknights', raw, bad)
        raise AssertionError('sha256 不对却通过了校验')
    except RuntimeError:
        pass

    # 大小不对
    try:
        packsync._verify('arknights', raw, dict(good, size=len(raw) - 1))
        raise AssertionError('大小不对却通过了校验')
    except RuntimeError:
        pass

    # 条数变少（把干员砍到 1 个，其余字段照抄，sha256 仍是对的）
    shrunk = json.loads(raw)
    shrunk['counts'] = dict(shrunk.get('counts') or {}, chars=1)
    raw2 = json.dumps(shrunk, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    meta2 = {'size': len(raw2), 'sha256': hashlib.sha256(raw2).hexdigest()}
    try:
        packsync._verify('arknights', raw2, meta2)
        raise AssertionError('条数变少却通过了校验')
    except RuntimeError as e:
        assert '比本地少' in str(e), '拒绝原因不对：%s' % e

    # 不是 json
    try:
        packsync._verify('arknights', b'not json', {'size': 8, 'sha256': ''})
        raise AssertionError('坏 json 却通过了校验')
    except RuntimeError:
        pass


# ------------------------------------------------- B22 程序自更新
def check_app_update():
    """版本比较、安装包校验、安装脚本的替换边界。

    这里最容易出事的三处：脏 tag 触发更新、压缩包路径穿越、
    替换时误删 config/。安装脚本（install.ps1）不在冒烟里真跑，
    退一步断言它生成的文本：程序本体全换（含 version.json），用户数据不动，
    并且不许再退回会死锁的进程列表管道。
    """
    import hashlib
    import io
    import zipfile
    from core import appupdate as A

    # 1. 版本比较
    assert A.parse_version('v2.4.0') == (2, 4, 0, 1, 0, 0)
    assert A.parse_version('2.4.0-beta.2') == (2, 4, 0, 0, 1, 2)
    assert A.parse_version('v2.5.0-rc1') == (2, 5, 0, 0, 2, 1)
    assert A.is_newer('v2.10.0', '2.9.0'), '2.10 必须大于 2.9（按数字比，不是字符串比）'
    assert not A.is_newer('v2.4.0-beta.1', '2.4.0'), '预发布不该比同号正式版新'
    assert A.is_newer('v2.4.0', '2.4.0-beta.1')
    assert not A.is_newer('v2.4.0', '2.4.0')
    # 预发布段位 alpha < beta < rc：只比序号会把 rc1 判到 beta.2 之下（#52）
    assert A.is_newer('v2.5.0-rc1', 'v2.5.0-beta.2'), 'rc 必须大于 beta'
    assert A.is_newer('v2.5.0-beta.3', 'v2.5.0-alpha.9'), 'beta 必须大于 alpha'
    assert A.is_newer('v2.5.0-beta.2', 'v2.5.0-beta.1'), '同段位比序号'
    for junk in ('', 'latest', 'v2', '2.4', 'v2.4.0.1', 'release-2.4'):
        assert A.parse_version(junk) is None, '脏 tag 不该解析成功：%r' % junk
        assert not A.is_newer(junk, '2.0.0'), '脏 tag 不该触发更新：%r' % junk

    # 2. 按架构选包 + digest 解析
    asset = A._pick_asset([
        {'name': 'MAA-PRTS-v2.4.0-win-arm64.zip'},
        {'name': 'MAA-PRTS-v2.4.0-win-x64.zip'},
        {'name': 'source.tar.gz'},
    ])
    assert asset and asset['name'].endswith('%s.zip' % A._arch()), asset
    assert A._digest_sha256({'digest': 'sha256:AB12'}) == 'ab12'
    assert A._digest_sha256({}) == ''

    # 3. 安装包完整性
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('MAA-PRTS/MAA-PRTS.exe', b'MZ' + b'\0' * 4096)
        zf.writestr('MAA-PRTS/_internal/x.dll', b'\0' * 2048)
    good = buf.getvalue()
    meta = {'size': len(good), 'sha256': hashlib.sha256(good).hexdigest()}
    A._verify_zip(good, meta)          # 不抛就算过

    for label, m in (
        ('sha256 不对', dict(meta, sha256='0' * 64)),
        ('大小不对', dict(meta, size=len(good) - 1)),
    ):
        try:
            A._verify_zip(good, m)
            raise AssertionError('%s却通过了校验' % label)
        except RuntimeError:
            pass

    # 不含 exe 的包必须拒
    noexe = io.BytesIO()
    with zipfile.ZipFile(noexe, 'w') as zf:
        zf.writestr('MAA-PRTS/readme.txt', b'hi')
    raw = noexe.getvalue()
    try:
        A._verify_zip(raw, {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        raise AssertionError('压缩包里没有 exe 却通过了校验')
    except RuntimeError:
        pass

    # 4. 路径穿越（zip-slip）必须拒
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, 'w') as zf:
        zf.writestr('MAA-PRTS/MAA-PRTS.exe', b'MZ')
        zf.writestr('../../evil.exe', b'x')
    evil.seek(0)
    try:
        with zipfile.ZipFile(evil) as zf:
            A._safe_members(zf)
        raise AssertionError('压缩包里的 ../ 没有拦住')
    except RuntimeError as e:
        assert '可疑路径' in str(e), e

    # 5. 安装脚本：只碰程序本体，用户数据一个都不能出现在替换范围里
    tmp = tempfile.mkdtemp(prefix='maaprts-appupd-')
    backup = A.update_dir
    try:
        A.update_dir = lambda: tmp
        stage = os.path.join(tmp, 'stage', 'MAA-PRTS')
        os.makedirs(stage, exist_ok=True)
        exe = os.path.join(stage, 'MAA-PRTS.exe')
        with open(exe, 'wb') as f:
            f.write(b'MZ')
        script = A._write_installer(exe, '2.4.0')
        assert script and os.path.isfile(script), '安装脚本没生成'
        assert script.endswith('install.ps1'), '安装脚本必须是 .ps1（cmd 版会死锁）'
        text = open(script, encoding='utf-8-sig').read()

        # 2026-11-08 真机事故：cmd 版用 `tasklist | find` 等进程退出，那条管道
        # 在 Windows 上会死锁（cmd 不关写端，find 等不到 EOF），安装卡死在第一步，
        # 用户只看到一个黑窗口。这两个词不许再出现在安装脚本里。
        low = text.lower()
        assert 'tasklist' not in low and 'find ' not in low, \
            '不许再用进程列表管道等待退出（会死锁）'
        assert 'Get-Process -Id $ParentPid' in text, '等待旧进程必须按 PID'
        assert 'stage\\MAA-PRTS' in text, '包内路径拼错了'
        assert '/MIR' in text, '_internal 必须用 /MIR，新版本删掉的旧 DLL 要清掉'
        # 版本号读的是包内 version.json：漏了会「装完还是旧版本 → 反复提示更新」
        assert 'version.json' in text, '安装脚本必须一起替换并校验 version.json'
        # robocopy 0-7 都是成功（1=有复制、3=有额外文件），8 以上才是真失败
        assert '$rc -ge 8' in text, 'robocopy 失败必须中断，不能半新半旧地启动'
        # 用户数据目录只允许出现在 robocopy 的 /XD 排除列表里
        for keep in ('config', 'debug', 'resources'):
            hits = [ln.strip() for ln in text.splitlines() if re.search(r'\b%s\b' % keep, ln)]
            assert len(hits) == 1 and '/XD' in hits[0], \
                '%s 只应出现在 /XD 排除列表里，实际：%s' % (keep, hits)
        # 装失败也得把旧版拉起来，别把用户扔在「程序没了」的状态里
        assert 'install-error.txt' in text and 'Start-Process' in text, \
            '失败路径要留痕并重新拉起旧版本'

        # 2026-11-15 真机：MAA.exe 把 _internal 里的 VCRUNTIME140*.dll 捞去加载，
        # robocopy 镜像 rc=9，装到一半回滚，用户看到「更新完还是旧版本」还不知
        # 道为什么。脚本必须重试，并把 robocopy 点名的文件写进错误提示。
        assert 'install-robocopy.log' in text, '镜像失败要留下 robocopy 明细，别只剩一个 rc'
        assert 'for ($i = 1; $i -le 4; $i++)' in text, '_internal 镜像要重试，不能一次失败就回滚'
        assert 'Start-Sleep -Seconds 2' in text, '重试之间要等占用方释放文件'

        # 7. 安装前预检：MAA 在跑时必须拦下并点名 MAA，别让用户白重启一次
        #（不去逐个试开 _internal：MAA-PRTS 自己就锁着二十多个，那样恒误报）
        import inspect as _inspect
        assert 'precheck()' in _inspect.getsource(A.install), 'install() 必须先跑预检'
        from core import maa as M
        was = M.is_running
        M.is_running = lambda: True
        try:
            why = A.precheck()
            assert 'MAA' in why, 'MAA 在跑时预检必须拦住并点名 MAA，实际：%r' % why
        finally:
            M.is_running = was

        # 拉起方式：PowerShell + 无窗口，且不能再走 cmd
        import inspect
        src = inspect.getsource(A.launch) + inspect.getsource(A._powershell)
        assert 'powershell.exe' in src and 'install.ps1' in src, '应该用 PowerShell 跑安装脚本'
        assert 'cmd.exe' not in src, '不要再回到 cmd 批处理'
        assert 'CREATE_NO_WINDOW' in src, '必须无窗口拉起，否则用户会看到黑窗口'

        # 6. API 不通时的兜底：按命名规则拼地址，照样能下载
        fb = A._fallback_asset('v2.4.0')
        assert fb['degraded'] and fb['sha256'] == '' and fb['size'] == 0, fb
        assert fb['name'] == 'MAA-PRTS-v2.4.0-win-%s.zip' % A._arch(), fb['name']
        assert fb['urls'][0].endswith('/releases/download/v2.4.0/' + fb['name']), fb['urls'][0]
        assert len(fb['urls']) == 3, '直链 + 两个反代都要有：%s' % fb['urls']

        # 没有 size/sha256 时不能把残包当「已下载」，否则解压必失败
        bad = os.path.join(tmp, 'broken.zip')
        with open(bad, 'wb') as f:
            f.write(b'not a zip')
        assert not A._match(bad, {'size': 0, 'sha256': ''}), '残包被当成了已下载'
    finally:
        A.update_dir = backup
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- B19 主线难度变体
def check_mainline_difficulty_variants():
    """主线难度体系（按章梳理，作业站按完整 stageId 索引作业）：

      0-8 章  main_N-M（普通）+ #f#（突袭，不单独成行，raid 标记挂主关）
      9 章    easy_N-M（简单）+ main_N-M（标准，带突袭）
      10-14 章 easy_/main_/tough_ 三套独立 id（简单/标准/磨难），搜代号默认磨难在前
      15-17 章 main_N-M（普通）+ main_N-M#s（险地，独立 stage_name 成行）
    """
    from core import copilot, resources

    # 搜代号返回全部难度变体；默认档（normal）用来出标题
    r = resources.resolve('12-7')
    assert r['mode'] == 'stage' and len(r['stages']) == 3, \
        '12-7 应有简单/标准/磨难三行，实际 %d' % len(r['stages'])
    byid = {s['stageId']: s for s in r['stages']}
    assert set(byid) == {'easy_12-06', 'main_12-06', 'tough_12-06'}, set(byid)
    assert byid['easy_12-06']['difficulty'] == 'easy' and byid['easy_12-06']['difficultyCN'] == '简单'
    assert byid['tough_12-06']['difficulty'] == 'tough' and byid['tough_12-06']['difficultyCN'] == '磨难'
    assert byid['main_12-06']['difficulty'] == 'normal'
    assert byid['main_12-06']['difficultyCN'] == '标准', '有简单档的章节，标准行应标「标准」'
    assert '简单' not in r['title'] and '平议生死' in r['title']

    # 15-17 章：普通 + #s 险地两行；粘 #s id 直接命中险地行（不再回落主关）
    r = resources.resolve('15-3')
    assert len(r['stages']) == 2, '15-3 应有普通/险地两行，实际 %d' % len(r['stages'])
    risky = [s for s in r['stages'] if s['stageId'] == 'main_15-02#s']
    assert risky and risky[0]['difficulty'] == 'risky' and risky[0]['difficultyCN'] == '险地'
    r = resources.resolve('main_15-02#s')
    assert [s['stageId'] for s in r['stages']] == ['main_15-02#s']
    # 险地行不带 raid 标记（15+ 章没有 #f#），普通行也不带
    assert not any(s['raid'] for s in resources.resolve('15-3')['stages'])

    # 9 章：简单 + 标准（标准带突袭 raid 标记，徽标叫「标准」不叫「普通」）
    r = resources.resolve('9-7')
    assert {s['stageId'] for s in r['stages']} == {'easy_09-06', 'main_09-06'}
    m9 = [s for s in r['stages'] if s['stageId'] == 'main_09-06'][0]
    assert m9['raid'] is True and m9['difficultyCN'] == '标准'

    # 0-8 章行为不变：只有主关一行，raid 标记挂主关，徽标仍叫「普通」（difficultyCN 空）
    r = resources.resolve('1-7')
    assert [s['stageId'] for s in r['stages']] == ['main_01-07'] and r['stages'][0]['raid'] is True
    assert r['stages'][0]['difficultyCN'] == ''

    # 系列视图：10-14 章要按简单/标准/磨难分开导入，简单行得看得见；
    # 9 章及以下仍不摆（0-9 章的导入菜单只分普通/突袭），磨难/险地始终保留
    r = resources.resolve('12-1')
    assert r['mode'] == 'activity'
    assert [s for s in r['stages'] if s['difficulty'] == 'easy'], '10-14 章系列视图要有简单行'
    assert any(s['difficulty'] == 'tough' for s in r['stages'])
    assert not [s for s in resources.resolve('9-1')['stages'] if s['difficulty'] == 'easy'], \
        '0-9 章系列视图不该有简单行'

    # 拆行：磨难/简单各一行（variant=normal、不按位标志筛）；险地一行（variant=raid）
    rows = [{'id': 1, 'difficulty': 0, 'mode': 'ready'},   # 位标志没标也照收
            {'id': 2, 'difficulty': 3, 'mode': 'ready'}]   # 标了通用也不筛掉
    tough = copilot.stage_variants(dict(byid['tough_12-06']), rows, limit=10)
    assert len(tough) == 1 and tough[0]['stage']['variant'] == 'normal'
    assert [o['id'] for o in tough[0]['ops']] == [1, 2], '磨难行不该按位标志筛作业'
    risky_row = copilot.stage_variants(dict(risky[0]), rows, limit=10)
    assert len(risky_row) == 1 and risky_row[0]['stage']['variant'] == 'raid'

    # 写 MAA：险地一条 IsRaid=true（MaaCore 15+ 章用它切险地）；磨难/简单一条 IsRaid=false
    assert copilot.entry_variants(0, None, 'main_15-02#s') == [True]
    assert copilot.entry_variants(3, 'normal', 'main_15-02#s') == [True]
    assert copilot.entry_variants(3, None, 'tough_12-06') == [False], '磨难没有突袭，不许按通用位展开两条'
    assert copilot.entry_variants(0, None, 'easy_12-06') == [False]
    assert copilot.entry_variants(3, 'normal') == [False], '不带 stage_name 的旧语义不变'
    assert copilot.difficulty_text_for('main_15-02#s', 0) == '险地'
    assert copilot.difficulty_text_for('tough_12-06', 0) == '磨难'
    assert copilot.difficulty_text_for('easy_12-06', 0) == '简单'
    assert copilot.difficulty_text_for('main_01-07', 3) == '通用'

    # 行排序：同代号内 磨难 → 标准（→ 突袭/险地）→ 简单；代号间按流程位置
    def pack(sid, diff, variant=None):
        return {'stage': {'stageId': sid, 'difficulty': diff, 'variant': variant}}
    keys = [copilot.row_sort_key(pack(*k)) for k in [
        ('easy_12-06', 'easy', 'normal'), ('tough_12-06', 'tough', 'normal'),
        ('main_12-06', 'normal', 'normal'), ('main_12-06', 'normal', 'raid'),
        ('main_15-02#s', 'risky', 'raid'), ('main_15-02', 'normal', 'normal')]]
    assert keys[1] < keys[2] < keys[3] < keys[0], '磨难→标准→突袭→简单 的顺序不对：%r' % keys
    assert keys[4] < keys[5], '险地是 15+ 章的主难度，应排在普通行前面'

    # 流程顺序：关卡按游戏内先后排，TR 教学关插到 stage_table 记的解锁前置关后面
    def codes(kw):
        rows = []
        for s in resources.resolve(kw)['stages']:
            rows.extend(copilot.stage_variants(s, [], limit=1))
        rows.sort(key=copilot.row_sort_key)
        return [r['stage']['code'] for r in rows]

    tl = codes('TL')
    assert tl[:2] == ['TL-TR-1', 'TL-1'], 'TL-TR-1 应排在 TL-1 前面：%r' % tl[:4]
    dh = codes('DH')
    assert dh[0] == 'DH-TR-1', dh[:4]
    assert dh[dh.index('DH-5') + 1] == 'DH-TR-2', 'DH-TR-2 应紧跟 DH-5：%r' % dh[:10]
    tr = codes('TR-')
    assert tr[:3] == ['TR-1', 'TR-2', 'TR-3'], '主线 TR 应按章顺序排：%r' % tr[:8]
    assert tr.index('TR-11') == tr.index('TR-10') + 1, 'TR-11 位置未知，排在本章最前面即可'
    assert tr[-3:] == ['TR-26', 'TR-27', 'TR-28'], '15-17 章的 TR 排最后：%r' % tr[-4:]
    assert tr.index('TR-13') < tr.index('TR-15'), 'TR-13 落在 2 章（S2-7 之后），不该跑到 3 章后面'

    # 主线 S 支线关也是主线关：位置来自 stage_table 的解锁前置关，列表和「下一关」
    # 都按解锁顺序插进主线（2-1 → S2-1 → 2-2 → S2-2 → S2-3 → S2-4 → 2-3）
    ch2 = [s['code'] for s in resources.resolve('2')['stages']]
    assert ch2[:8] == ['TR-11', '2-1', 'S2-1', '2-2', 'S2-2', 'S2-3', 'S2-4', '2-3'], \
        '搜章节号 2 应按推图顺序带上 S2 和 TR：%r' % ch2[:10]
    assert ch2[-1] == '2-10' and 'S2-12' in ch2 and 'TR-S-2' in ch2, ch2[-4:]
    assert len(resources.resolve('S2')['stages']) == 12, 'S2 系列应有 12 关'
    # 纯章节号（2 / 2-）都能搜，且不夹带别的章 / 活动
    assert {s['code'] for s in resources.resolve('2-')['stages']} == set(ch2)
    assert not [s for s in resources.resolve('2')['stages']
                if s['zoneId'].startswith('act')], '章节视图不该混入活动关'
    # 主线 S 关必须全部登记解锁位置，漏一条就会掉到列表末尾
    sub_ids = {s['stageId'] for s in resources._stages()
               if re.match(r'^S\d+-\d+$', s['code'])}
    assert sub_ids and not (sub_ids - set(resources.SUB_AFTER)), \
        '主线 S 关没登记解锁位置：%r' % (sub_ids - set(resources.SUB_AFTER))

    # 下一关：同系列同难度推进；剧情关空缺跳过；章末返回 None；TR 参与推进
    def nxt(sid, diff):
        return resources.next_stage({'stageId': sid, 'difficulty': diff})
    assert nxt('tough_12-06', 'tough') == {
        'code': '12-8', 'stageId': 'tough_12-07', 'difficulty': 'tough',
        'difficultyCN': '磨难'}, '磨难行下一关应是 12-8 磨难'
    assert nxt('tough_12-09', 'tough')['code'] == '12-12', '12-11 是剧情关，下一关应跳到 12-12'
    assert nxt('main_15-02#s', 'risky')['code'] == '15-4', '险地行下一关应是 15-4 险地'
    assert nxt('main_01-07', 'normal')['code'] == '1-8'
    assert nxt('tough_12-18', 'tough') is None, '12 章磨难末关（12-20）没有下一关'
    # 活动关按活动内顺序推进（活动 id 数字没零填充，自然排序不能乱）
    act = resources.resolve('act13side')['stages']
    pair = next((act[i], act[i + 1]) for i in range(len(act) - 1)
                if act[i]['code'] == 'NL-9')
    got = resources.next_stage(pair[0])
    assert got and got['code'] == pair[1]['code'], \
        'NL-9 的下一关应是 %s，实际 %r' % (pair[1]['code'], got)

    # TR 教学关参与推进（位置来自 TR_AFTER）：TL-TR-1 在 TL-1 前面，所以
    # TL-10 仍是末关；主线 0-1 → TR-1 → 0-2；未登记位置的 TR 排系列最前面
    assert nxt('act55side_10', 'normal') is None, 'TR 插在 TL-1 前面，TL-10 仍是末关'
    assert nxt('act13side_tr01', 'normal')['stageId'] == 'act13side_01', \
        'NL-TR-1 的下一关应是 NL-1'
    assert nxt('main_00-01', 'normal')['stageId'] == 'tr_01', '0-1 后面紧跟 TR-1'
    assert nxt('tr_01', 'normal')['stageId'] == 'main_00-02', 'TR-1 后面是 0-2'
    assert nxt('main_02-01', 'normal')['code'] == 'S2-1', '2-1 后面是 S2-1'
    assert nxt('sub_02-01', 'normal')['code'] == '2-2', 'S2-1 后面是 2-2'
    assert nxt('sub_02-04', 'normal')['code'] == '2-3', 'S2-4 打完才开 2-3'
    assert nxt('tr_13', 'normal')['code'] == '2-5', 'TR-13 后面是 2-5'
    assert nxt('main_02-10', 'normal') is None, '2-10 是第 2 章末关'
    # TR 判定覆盖全部 id 形态（tr_01 / _tr01 / -tr01 / lt_tr_01）
    for sid in ('tr_01', 'lt_tr_01', 'act55side_tr01', 'act2vmulti-tr01', 'act18side_tr_02'):
        assert resources._is_tr(sid), '%s 应判为 TR' % sid
    for sid in ('act55side_10', 'main_12-06', 'tough_12-06', 'main_15-02#s', 'hard_15-01'):
        assert not resources._is_tr(sid), '%s 不该判为 TR' % sid
    # TR_AFTER 是人工维护的扩展点：改完清一次缓存（flow 懒计算）就该生效
    old = resources.TR_AFTER.get('act55side_tr01')
    resources.TR_AFTER['act55side_tr01'] = 'act55side_05'
    resources.clear_cache()
    try:
        got = nxt('act55side_05', 'normal')
        assert got and got['stageId'] == 'act55side_tr01', \
            '登记后 TL-TR-1 应插在 TL-5 后面，实际 %r' % got
        got = nxt('act55side_tr01', 'normal')
        assert got and got['code'] == 'TL-6', '登记后的 TR 应能推进到锚点下一关'
    finally:
        if old is None:
            resources.TR_AFTER.pop('act55side_tr01', None)
        else:
            resources.TR_AFTER['act55side_tr01'] = old
        resources.clear_cache()


# ------------------------------------------------- B28 推荐排序与详情缺谁提示
def check_copilot_rank_band():
    """推荐排序：只有「可直接抄且评分 >= 9」才压过借人作业。

    用户反馈（2026-11-16）：最优推荐长期被低分全齐作业占住，旁边 10 分只差一个
    助战的作业反而排在后面。规则改成 mode_band：0 = 可直接抄 9 分以上、
    1 = 能打的（可直接抄但分不够 / 差一人可借）、2 = 需补练度、3 = 打不了，
    档内仍按账号选的算法（评分 / 少人 / 单核）。

    顺带断言详情弹窗的数据源：detail_opers 要给出 owned/ok（缺人与缺练度分得开），
    没传账号练度时两者必须是 None——否则整份作业都会被标成缺卡。
    """
    from core import copilot, resources

    def row(mode, rating, **kw):
        r = {'mode': mode, 'rating': rating, 'views': 0, 'total': 6,
             'weight': 0.0, 'singleCore': False}
        r.update(kw)
        return r

    # 分档：9 分是分水岭
    assert copilot.mode_band(row('ready', 10)) == 0
    assert copilot.mode_band(row('ready', 9)) == 0
    assert copilot.mode_band(row('ready', 8)) == 1, '8 分的全齐作业不该独占第一档'
    assert copilot.mode_band(row('borrow', 10)) == 1
    assert copilot.mode_band(row('train', 10)) == 2
    assert copilot.mode_band(row('blocked', 10)) == 3

    rows = [row('ready', 3, views=9999), row('borrow', 10),
            row('borrow', 9, views=5), row('ready', 10),
            row('train', 10), row('blocked', 10)]
    got = [(r['mode'], r['rating'])
           for r in sorted(rows, key=lambda r: copilot.rank_key(r, 'rating'))]
    assert got == [('ready', 10), ('borrow', 10), ('borrow', 9),
                   ('ready', 3), ('train', 10), ('blocked', 10)], \
        '评分优先的推荐顺序不对：%r' % (got,)

    # 少人 / 单核两种算法同样守分档：低分 ready 不能跳过一条高分 borrow
    for algo in ('fewer', 'single'):
        keys = {m: copilot.rank_key(row(m, 8 if m == 'ready' else 10), algo)[0]
                for m in ('ready', 'borrow', 'train')}
        assert keys['ready'] == keys['borrow'] == 1 and keys['train'] == 2, \
            '%s 算法的分档不对：%r' % (algo, keys)

    # 详情：谁缺练度、谁没有（借人对象），口径与搜索侧一致
    names = [c['name'] for c in (resources.char_table() or {}).values()
             if isinstance(c, dict) and int(c.get('rarity') or 0) + 1 == 6][:2]
    assert len(names) == 2, '资源包里找不到两个六星干员'
    weak, absent = names
    content = {'opers': [
        {'name': weak, 'skill': 1, 'requirements': {'elite': 2, 'level': 90}},
        {'name': absent, 'skill': 1, 'requirements': {'elite': 2, 'level': 60}},
    ]}
    owned = {copilot.norm(weak): {
        'name': weak, 'rarity': 6, 'elite': 1, 'level': 60, 'mainSkillLevel': 7,
        'masteryLevels': [0, 0, 0], 'moduleLevels': {}, 'skillKnown': True}}
    d = copilot.detail_opers(content, owned)
    assert d[0]['owned'] is True and d[0]['ok'] is False, '练度不够的要标 ok=False'
    assert d[1]['owned'] is False and d[1]['ok'] is False, '没有的干员要标 owned=False'
    assert d[0]['req'].get('elite') == 2, '详情里的要求仍按作业原文'
    d2 = copilot.detail_opers(content, None)
    assert all(o['owned'] is None and o['ok'] is None for o in d2), \
        '没传账号练度时不能声称谁缺谁有'


# ---------------------------------------------------------------- B23 作业黑名单
def check_story_stages(_tmp):
    """B29：剧情关双视图 + 森空岛进度解析

    剧情关（资源包里 story=True 的 208 条）以前被 _stages() 整条丢掉，导致
    两件事：搜 9-1 / TL-ST-1 这类「只有剧情关才有的代号」会落空；森空岛
    activity.zones 的 totalStage 含剧情关，两边口径永远对不上。
    """
    from core import progress, resources as R

    plain = R._stages()
    full = R._stages(True)
    assert all(not s['story'] for s in plain), '默认视图里混进了剧情关'
    stories = [s for s in full if s['story']]
    assert len(stories) == 208, '剧情关应为 208 条，实际 %d' % len(stories)
    assert len(full) - len(plain) == len(stories), '两个视图的差应正好是剧情关条数'

    # 剧情关代号精确命中：TL-ST-1 这种前缀不可能被章节抢占；
    # 主线剧情关用 stageId 精确查（9-1 这个代号要让位给「第 9 章系列」展开，
    # 因为 9-10 ~ 9-19 都以它开头，见 B18 的 4.5）
    r = R.resolve('TL-ST-1')
    assert r['mode'] == 'story' and r['stages'][0]['stageId'] == 'act55side_st01'
    r = R.resolve('st_09-01')
    assert r['mode'] == 'story' and r['stages'][0]['code'] == '9-1', \
        'stageId 精确查剧情关失败：%r' % r['mode']

    # 系列搜索不带剧情关，但能算出「这一段里有几个剧情关」
    r = R.resolve('TL-')
    assert r['stages'] and not any(s['story'] for s in r['stages']), 'TL- 系列混进了剧情关'
    tips = R.stories_between(r['stages'])
    assert {s['code'] for s in tips} >= {'TL-ST-1', 'TL-ST-2'}, 'TL 系列的剧情关没找出来'
    assert all(s['story'] for s in tips), 'stories_between 返回了非剧情关'

    # 推图不能跳进剧情关
    nxt = R.next_stage({'stageId': 'main_09-01', 'difficulty': 'normal'})
    assert nxt and not R.story_of(nxt['stageId']), 'next_stage 指到了剧情关'
    after = R.stages_after('st_09-01')
    assert after and after[0]['code'] == '9-2', '剧情关之后应该是 9-2'
    assert not any(s['story'] for s in after), 'stages_after 里混进了剧情关'

    # 森空岛进度解析（结构取自 player/info 的真实返回）
    probe = {
        'status': {'level': 71, 'mainStageProgress': 'st_09-01',
                   'ap': {'current': 561, 'max': 127}},
        'campaign': {'records': [{'campaignId': 'camp_01', 'maxKills': 400},
                                 {'campaignId': 'camp_03', 'maxKills': 271}]},
        'activity': [{'actId': 'act55side', 'actReplicaId': 'act55side',
                      'zones': [{'zoneReplicaId': 'act55side_zone1',
                                 'clearedStage': 3, 'totalStage': 6}]}],
        'routine': {'daily': {'current': 1, 'total': 10},
                    'weekly': {'current': 0, 'total': 13}},
    }
    pr = progress.parse(probe)
    assert pr['main']['story'] is True and pr['main']['remaining'] > 0, '主线进度没解析出来'
    assert pr['main']['next']['code'] == '9-2', '主线下一关应是 9-2'
    assert [c['id'] for c in pr['campaign']] == ['camp_03', 'camp_01'], '没打满的剿灭应排前面'
    assert pr['campaign'][0]['full'] is False and pr['campaign'][1]['full'] is True
    assert pr['campaign'][0]['name'] == '龙门市区', 'camp_03 应叫龙门市区'
    a = pr['activities'][0]
    assert a['left'] == 3 and a['matched'] is True, '活动 zone 余量算错'
    assert a['name'] == '时间啊，向前！', '活动名应取正式名'
    assert pr['ap']['current'] == 561 and pr['routine']['daily']['total'] == 10
    assert progress.summary(pr)['mainRemaining'] == pr['main']['remaining']


# ---------------------------------------------------------------- B30 日志观测
def check_asstlog(_tmp):
    """B30：MAA 日志观测（增量读 / 轮转 / 事件归一 / 停滞判定 / 运行记录）

    这一层完全靠日志，不认识图。所以 MAA 升级改了格式，最坏结果只能少几个
    事件，不能抛异常——下面每条断言都在守这件事。
    """
    import time as _t
    from core import asstlog as A

    maa_dir = os.path.join(_tmp, 'obs')
    dbg = os.path.join(maa_dir, 'debug')
    os.makedirs(dbg, exist_ok=True)
    asst = os.path.join(dbg, 'asst.log')
    gui = os.path.join(dbg, 'gui.log')

    def stamp(ts):
        return _t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(ts)) \
            + '.%03d' % int((ts % 1) * 1000)

    def line(ts, body, tag='INF'):
        return '[%s][%s][Px1][Tx1] %s' % (stamp(ts), tag, body)

    now = _t.time()
    stage_body = ('SubTaskExtraInfo {"class":"asst::MultiCopilotTaskPlugin",'
                  '"details":{"file_name":"config\\\\copilot\\\\TL-1_101.json","id":0,'
                  '"stage_name":"act55side_01"},"subtask":"MultiCopilotTask",'
                  '"taskchain":"Copilot","taskid":1,'
                  '"what":"CopilotListLoadTaskFileSuccess"}')
    node_body = ('{"class":"asst::ProcessTask","cur_task":"Copilot@WaitUntilEndOfAction",'
                 '"details":{"cur_retry":18,"retry_times":20,'
                 '"to_be_recognized":["Copilot@SkipThePreBattlePlot",'
                 '"Copilot@EndOfAction"]},"subtask":"ProcessTask",'
                 '"taskchain":"Copilot","taskid":1}')
    with open(asst, 'w', encoding='utf-8') as f:
        f.write(line(now - 40, stage_body) + '\n')
        f.write(line(now - 30, node_body) + '\n')
    with open(gui, 'w', encoding='utf-8') as f:
        f.write(line(now - 45, 'AsstProxy | <2> Start Task Chain: Copilot, Task ID: 1') + '\n')

    w = A.Watcher(maa_dir, live=False)
    snap = w.poll()
    assert snap['state'] == 'running', '有链起点 + 新日志应判在跑：%r' % snap['state']
    assert snap['stage'] == 'act55side_01', snap['stage']
    assert snap['index'] == 0, snap['index']
    assert snap['node'] == 'Copilot@WaitUntilEndOfAction', snap['node']
    assert (snap['retry'], snap['retryTotal']) == (18, 20), snap['retry']

    # 增量：只读新追加的那一行，旧的不重复计数
    with open(asst, 'a', encoding='utf-8') as f:
        f.write(line(now - 5, stage_body.replace('act55side_01', 'act55side_02')
                     .replace('"id":0', '"id":1')) + '\n')
    snap = w.poll()
    assert snap['newEvents'] == 1, '增量读多算了：%d' % snap['newEvents']
    assert snap['stage'] == 'act55side_02', snap['stage']

    # 轮转：asst.log 被清空重写（变小）后要能从头读，不能卡在旧 offset
    # （用单独的目录，否则前面造的两关数据会被这一步冲掉）
    d3 = os.path.join(_tmp, 'obs3')
    os.makedirs(os.path.join(d3, 'debug'), exist_ok=True)
    f3 = os.path.join(d3, 'debug', 'asst.log')
    with open(f3, 'w', encoding='utf-8') as f:
        f.write(line(now - 40, stage_body) + '\n')
    w3 = A.Watcher(d3, live=False)
    assert w3.poll()['stage'] == 'act55side_01'
    with open(f3, 'w', encoding='utf-8') as f:
        f.write(line(now - 2, stage_body.replace('act55side_01', 'act55side_09')) + '\n')
    assert w3.poll()['stage'] == 'act55side_09', '轮转后没重新读'

    # 停滞：最后一条日志停在 200 秒前、重试已经打满 → stalled（不是 running）
    d2 = os.path.join(_tmp, 'obs2')
    os.makedirs(os.path.join(d2, 'debug'), exist_ok=True)
    with open(os.path.join(d2, 'debug', 'gui.log'), 'w', encoding='utf-8') as f:
        f.write(line(now - 200, 'AsstProxy | <2> Start Task Chain: Copilot, Task ID: 1') + '\n')
    with open(os.path.join(d2, 'debug', 'asst.log'), 'w', encoding='utf-8') as f:
        f.write(line(now - 195, node_body) + '\n')
    snap2 = A.Watcher(d2, live=False).poll()
    assert snap2['state'] == 'stalled', '停了 3 分钟该判停滞：%r' % snap2['state']
    ex = A.explain(snap2)
    assert ex['level'] == 'bad' and '卡' in ex['text'], ex
    assert ex['hint'], '停滞要给下一步提示'

    # clean_done：链跑完就收工，不能一直挂在 running
    with open(os.path.join(d2, 'debug', 'gui.log'), 'a', encoding='utf-8') as f:
        f.write(line(now - 1, 'AsstProxy | <2> Completed Task Chain: Copilot, Task ID: 1') + '\n')
    snap3 = A.Watcher(d2, live=False).poll()
    assert snap3['state'] == 'done', snap3['state']
    assert A.explain(snap3)['level'] == 'ok'

    # 运行记录：每换一关切一段，同一关的重复上报合并
    rows = A.failures(maa_dir, limit=10)
    assert len(rows) >= 2, '应能还原出至少两关：%r' % rows
    assert rows[0]['stageId'] == 'act55side_01', rows[0]
    assert all(r['code'] for r in rows), '运行记录要带关卡代号（查不到就回退 stageId）'

    # 认不出的行一律跳过，绝不抛
    for junk in ('', '乱七八糟一行', '[2026-11-16 10:00:00.000][ERR][Px1][Tx1] 报错',
                 '{"class":"asst::X"', '[]'):
        assert A.norm_asst(junk) is None, '非事件行不该产出事件：%r' % junk
        assert A.norm_gui(junk) is None


# ---------------------------------------------------------------- B31 多账号
def check_multiacct(tmp):
    """B31：多账号编排（切号名归一 / 一账号一档案 / AccountName 写入）"""
    from core import copilot, multiacct as M, settings

    _reset_config(tmp)
    # 切号名：MAA 是子串匹配，官方说明允许「123****4567 / 4567 / 23****45」
    # 三种写法。以前只认 3 位+4 星+4 位，用户照文档只给尾号就被判非法，
    # 助手只能反复追问，最后任务出不来（2026-11-17）。
    from core import acctname
    assert M.norm_account_name('5505') == '5505', '尾号要原样保留（MAA 认）'
    assert M.norm_account_name('1815505') == '181****5505'
    assert M.norm_account_name('18112345505') == '181****5505', '完整手机号也要能补成掩码'
    assert M.norm_account_name('181****5505') == '181****5505'
    assert M.norm_account_name('23****45') == '23****45', '部分掩码别被压星号'
    # 星号数量 / 全角 / 分隔符都不该让用户重填
    for raw, want in (('181＊5505', '181****5505'), ('181*5505', '181****5505'),
                      ('181***5505', '181****5505'), ('181 5505', '181****5505'),
                      ('181-5505', '181****5505'), ('（181）5505', '181****5505'),
                      ('１２３＊５５０５', '123****5505'),
                      ('181****  5505', '181****5505')):
        assert M.norm_account_name(raw) == want, (raw, M.norm_account_name(raw))
    assert M.norm_account_name('小卡拉帕鲁灰') == '小卡拉帕鲁灰'
    assert M.norm_account_name('灰bao#0384') == '灰bao#0384', 'B 服用户名带 # 别被吃掉'
    assert M.norm_account_name('') == ''
    # 校验：官服只拦昵称，格式问题不归用户
    assert acctname.check('5505', '官服') == ('5505', '')
    assert acctname.check('136', '官服') == ('136', ''), '本机档案里就有 3 位片段'
    assert acctname.check('23****45', '官服')[0] == '23****45'
    nm, why = acctname.check('小卡拉帕鲁灰', '官服')
    assert nm is None and '后 4 位' in why, (nm, why)
    assert acctname.check('小卡拉帕鲁灰', 'bilibili服')[0] == '小卡拉帕鲁灰'
    assert acctname.is_official_channel('') is True, '渠道缺失按官服算'
    assert acctname.is_official_channel('B服') is False
    # 档案名撞了就加序号，不覆盖用户原来的档案
    # 一律带 PRTS- 前缀：绝不覆盖用户自己那份档案
    taken = {'PRTS-a', 'PRTS-a_2'}
    assert M._profile_name('a', 'x', taken) == 'PRTS-a_3'
    assert M._profile_name('', '', set()) == 'PRTS-acct'

    # 排不出方案时必须明确说缺什么，不能悄悄返回一个空方案
    p = M.plan({'keyword': '', 'accounts': []})
    assert not p['ok'] and (p.get('missing') or p.get('error')), \
        '排不出方案要说明原因：%r' % p

    # 账号绑的 maaProfile 是**档案名**（moren / 136），不是切号名。拿它填
    # StartUp.AccountName，MAA 切号必然失败，整轮挂机死在「登录」这一步。
    old_c = M.candidates
    M.candidates = lambda: [{'file': 'x.json', 'name': '测试号',
                             'nickName': '测试号', 'channel': '官服',
                             'maaProfile': 'moren', 'accountName': '',
                             'maaDir': '', 'progress': {}}]
    try:
        pl = M.plan({'keyword': '', 'accounts': []})
        whys = {m.get('what') for m in pl['missing']}
        assert 'accountName' in whys, pl
        assert all(not r.get('accountName') for r in pl['accounts']), \
            '档案名不能冒充切号名：%r' % pl['accounts']
        # 用户给尾号就能过，且原样写进 rows
        pl = M.plan({'keyword': '', 'accounts': [], 'names': {'x.json': '5505'}})
        assert pl['accounts'][0]['accountName'] == '5505', pl['accounts'][0]
    finally:
        M.candidates = old_c

    # 假 MAA 目录：一个账号 → 一个档案，AccountName 与开关都写进去
    md = os.path.join(tmp, 'fakemaa')
    os.makedirs(os.path.join(md, 'config'), exist_ok=True)
    cfg = {'Current': 'Default', 'Configurations': {'Default': {
        'TaskQueue': [{'$type': 'StartUpTask', 'AccountName': '',
                       'AccountSwitchEnabled': False, 'TaskType': 'StartUp',
                       'Name': '开始唤醒', 'IsEnable': True}],
        'Copilot': {'TaskList': []},
    }}}
    with open(os.path.join(md, 'config', 'gui.new.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)

    old_fetch = copilot.fetch_prepared
    # 不联网：假装作业已经下好了
    copilot.fetch_prepared = lambda cid, *a, **k: (
        {'stage_name': 'main_01-07', 'opers': []}, 1, 'main_01-07', [], True)
    try:
        plan = {'maaDir': md, 'accounts': [{
            'file': 'x.json', 'name': '测试号', 'accountName': '181****5505',
            'profile': '181****5505', 'reuse': False,
            'ops': [{'copilotId': 1, 'stageCode': '1-7', 'title': 't'}],
        }]}
        res = M.apply(plan)
        assert res['profiles'] == ['PRTS-181****5505'], res
        with open(os.path.join(md, 'config', 'gui.new.json'), encoding='utf-8') as f:
            cfg2 = json.load(f)
        prof = cfg2['Configurations']['PRTS-181****5505']
        su = prof['TaskQueue'][0]
        assert su['AccountName'] == '181****5505', '切号名没写进 StartUpTask'
        assert su['AccountSwitchEnabled'] is True, '没打开切号开关'
        tl = prof['Copilot']['TaskList']
        assert len(tl) == 1 and tl[0]['CopilotId'] == 1, tl
        # 模板档案本身不能被改动
        assert cfg2['Configurations']['Default']['Copilot']['TaskList'] == []

        # set_tasklist / set_current：续跑与切号的底层，索引要重排
        from core import maa
        maa.set_current('PRTS-181****5505', md, close_first=False)
        assert maa.profiles(md)['current'] == 'PRTS-181****5505'
        maa.set_tasklist(tl, 'PRTS-181****5505', md, close_first=False)
        got = maa.tasklist('PRTS-181****5505', md)['items']
        assert len(got) == 1 and got[0]['Index'] == 0
        try:
            maa.set_current('不存在', md, close_first=False)
            raise AssertionError('切到不存在的档案应该报错')
        except ValueError:
            pass
    finally:
        copilot.fetch_prepared = old_fetch


# ---------------------------------------------------------------- B32 助手与 CLI
def check_agent_cli(tmp):
    """B32：内置助手的工具白名单 + 写操作闸门 + CLI 契约"""
    import contextlib
    import io as _io
    import time as _t

    from core import agent, settings
    import cli

    _reset_config(tmp)
    _patch_settings(tmp)      # 造账号/读账号都落在 tmp/config，别碰隔离根
    # 工具表与实现必须一一对应：多一个没实现 = 模型调到就崩
    names = {t['function']['name'] for t in agent.TOOLS}
    handlers = set(agent._HANDLERS) | set(agent._WRITE_HANDLERS)
    assert names == handlers, '工具表与实现不一致：%r' % (names ^ handlers)
    for t in agent.TOOLS:
        fn = t['function']
        assert fn.get('description'), '工具缺描述：' + fn['name']
        assert fn['parameters']['type'] == 'object'

    # 模型给错工具名不能把 loop 炸掉
    r = agent.run_tool('does_not_exist', {})
    assert isinstance(r, dict) and r.get('error'), '未知工具应返回 error'

    # 工具内部异常也要兜住
    r = agent.run_tool('op_detail', {'copilot_id': 'not-a-number'})
    assert isinstance(r, dict) and r.get('error'), '坏参数应兜成 error'
    assert r.get('hint'), '内部异常要带 hint，否则模型会原样重试同一个调用'

    # 账号字段对齐：list_accounts() 给的是 nickName/alias/channelName，
    # progress.summary() 给的是 mainRemaining/activityLeft。助手工具曾直接
    # 下标 'name'（KeyError，一问进度就报错）、又去取不存在的 'mainLeft' /
    # 'activityZones'（全是 null）。同一个坑踩过两次（CLI 一次、助手一次），
    # 这里用真实账号文件钉住字段名。
    acc = {
        'account': {'uid': '123', 'nickName': '测试号#0001',
                    'channelName': '官服', 'game': 'arknights', 'level': 120},
        'progress': {
            'main': {'stageId': 'main_15-17', 'code': '15-19', 'name': '直至，此刻',
                     'remaining': 5,
                     'next': {'code': '15-20', 'stageId': 'main_15-18',
                              'name': '得以重回'}},
            'campaign': [],
            'activities': [{'name': '孤星', 'left': 3, 'matched': True}],
        },
    }
    with open(os.path.join(tmp, 'config', '测试号#0001 官服.json'),
              'w', encoding='utf-8') as f:
        json.dump(acc, f, ensure_ascii=False)
    rows = agent.run_tool('list_accounts', {})['accounts']
    assert len(rows) == 1, '账号列表应读到造的账号：%r' % rows
    row = rows[0]
    assert row['name'] == '测试号#0001', '账号名要取 nickName/alias：%r' % row
    assert row['channel'] == '官服', row
    assert row['mainLeft'] == 5, 'mainLeft 要取 summary 的 mainRemaining：%r' % row
    assert row['mainKnown'] is True and row['activityLeft'] == 1, row
    pg = agent.run_tool('account_progress', {'account': row['file']})
    assert pg['summary']['mainRemaining'] == 5, pg
    assert pg['summary']['mainKnown'] is True, pg
    # 主线缺数据时不能靠 0 推断「已推完」
    acc['progress']['main'] = None
    with open(os.path.join(tmp, 'config', '测试号#0001 官服.json'),
              'w', encoding='utf-8') as f:
        json.dump(acc, f, ensure_ascii=False)
    pg = agent.run_tool('account_progress', {'account': row['file']})
    assert pg['summary']['mainKnown'] is False, 'main=null 要标成没数据：%r' % pg

    # 切号名：助手只问尾号，用户给什么格式都得吃下（原来 3+4 星+4 的死正则
    # 把「照 MAA 文档填的 5505」判成非法，助手反复追问，任务出不来）
    f = row['file']
    got, miss, err = agent._roster([], {f: '5505'}, '')
    assert not err and not miss and got[0]['accountName'] == '5505', (got, miss)
    for raw in ('181 5505', '181＊5505', '18112345505', '1815505'):
        got, miss, _ = agent._roster([], {f: raw}, '')
        assert got and got[0]['accountName'] == '181****5505', (raw, got, miss)
    # 清掉上面几轮写回账号文件的切号名，否则会走「saved」分支蒙过去
    from core import accounts as _accounts
    _accounts.save_maa_meta(f, {'accountName': ''})
    got, miss, _ = agent._roster([], {f: '测试号#0001'}, '')
    assert not got and miss, ('官服填昵称必须拦下（否则切号死循环）', got, miss)
    assert '后 4 位' in miss[0]['question'], miss[0]['question']

    # 写操作在 autoApply 关闭时必须停下来等用户点确认
    settings.save({'agent': {'baseUrl': 'https://api.deepseek.com/v1',
                             'model': 'deepseek-chat', 'autoApply': False}})
    agent._plans['t1'] = {'at': _t.time(), 'plan': {
        'maaDir': '', 'totalStages': 1, 'totalOps': 1,
        'accounts': [{'name': '测试号', 'accountName': '181****5505',
                      'ops': [{'stageCode': '1-7', 'copilotId': 1}]}],
    }}
    s = agent.Session()
    out = agent.run_tool('apply_plan', {'plan_id': 't1'}, s)
    assert out.get('need_confirm') is True, out
    assert s.pending and s.pending['action'] == 'apply_plan', s.pending
    assert '测试号' in (out.get('summary') or ''), '确认文案要写清楚给谁挂什么'

    # 助手没配 Key 时，状态要如实说「不能用」，不能假装可用
    st = agent.status()
    assert st['configured'] is False and st['hasKey'] is False
    # apiKey 绝不回显
    assert 'apiKey' not in st and 'key' not in st, st

    # CLI：输出形状 + 退出码是给外部 harness 的契约
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(['doctor', '--maa-dir', os.path.join(tmp, 'nope')])
    body = json.loads(buf.getvalue())
    assert body['command'] == 'doctor' and 'ok' in body, body
    assert body['data']['ready'] is False, '空目录不该判就绪'
    assert rc in (0, 1, 2, 3), rc

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(['no_such_cmd'])
    assert rc == 2, '未知命令应回 2'
    assert json.loads(buf.getvalue())['ok'] is False

    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(['stages'])
    assert rc == 2, '缺参数应回 2'
    assert 'hint' in json.loads(buf.getvalue())['error'], '错误要带 hint'

    # 关卡命令的出口必须过 resources：模型/脚本给的代号查不到就明确报错
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(['stages', 'TL-'])
    body = json.loads(buf.getvalue())
    assert rc == 0 and body['data']['stages'], body
    assert any(s['code'] == 'TL-1' for s in body['data']['stages'])

    # 位置参数要跳过 --opt value：``ops --account 号.json TL-5`` 曾经把账号名
    # 当成关卡关键词
    assert cli._positionals(['--account', '号.json', 'TL-5']) == ['TL-5']
    assert cli._positionals(['TL-5', '--limit', '2']) == ['TL-5']

    # ``plan --stages TL-5`` 是文档里的写法，但 --stages 同时被当成数量上限，
    # 于是 int('TL-5') 直接炸 ValueError。不联网：把 plan 换成假的抓参数。
    seen = {}
    old_plan = cli.multiacct.plan
    cli.multiacct.plan = lambda req: (seen.update(req)
                                      or {'ok': False, 'warnings': ['测试']})
    try:
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(['plan', '--stages', 'TL-5', '--limit', '2'])
    finally:
        cli.multiacct.plan = old_plan
    assert rc == cli.EXIT_DECIDE, '排不出方案应回 3（该问用户），实际 %r' % rc
    assert seen.get('keyword') == 'TL-5', seen
    assert seen.get('limitStages') == 2, '--limit 要进 limitStages：%r' % seen



# ---------------------------------------------------------------- B33 任务编排
def check_maaplan(tmp):
    """B33：任务编排（GUI 队列 / 直连序列 / 档案隔离）

    两条路线各自的契约：
    - GUI 路线写 ``<档案>.TaskQueue``，**绝不能**放 Copilot —— MAA 的
      ``TaskQueueViewModel.InitializeItems`` 不认识 CopilotTask，会抛
      NotImplementedException，整个任务列表变空白（连别的任务也看不见）。
    - 直连路线 ``core_tasks`` 生成 MaaCore 的任务序列，Copilot 的
      ``filename`` 必须是绝对路径（相对路径不报错，就是静默不入队）。
    """
    from core import copilot, maacore, maaplan

    _reset_config(tmp)
    md = os.path.join(tmp, 'fakemaa3')
    os.makedirs(os.path.join(md, 'config', 'copilot'), exist_ok=True)
    user_prof = {
        'TaskQueue': [
            {'$type': 'StartUpTask', 'TaskType': 'StartUp', 'Name': '开始唤醒',
             'IsEnable': True, 'AccountName': 'mine', 'AccountSwitchEnabled': False},
            {'$type': 'FightTask', 'TaskType': 'Fight', 'Name': '理智作战',
             'IsEnable': True, 'StagePlan': ['1-7']},
            {'$type': 'RecruitTask', 'TaskType': 'Recruit', 'Name': '自动公招',
             'IsEnable': True},
            {'$type': 'InfrastTask', 'TaskType': 'Infrast', 'Name': '基建换班',
             'IsEnable': True},
        ],
        'Copilot': {'TaskList': []},
        'Gui': {'ConnectSettings': {'AdbPath': 'adb.exe', 'Address': 'emulator-5554',
                                    'Config': 'LDPlayer'},
                'StartUpSettings': {'RunDirectly': False}},
    }
    cfg = {'Current': 'mine',
           'Configurations': {'mine': json.loads(json.dumps(user_prof))}}
    with open(os.path.join(md, 'config', 'gui.new.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False)

    # 1) GUI 队列：Copilot 必须被剔出去
    q, skipped = maaplan.build_queue(
        {'tasks': ['startup', 'copilot', 'fight'], 'stages': ['TL-5'],
         'ops': [{'copilotId': 1}]},
        user_prof['TaskQueue'])
    types = [t['TaskType'] for t in q]
    assert 'Copilot' not in types, 'GUI 队列不能有 CopilotTask：%r' % types
    assert 'StartUp' in types and 'Fight' in types, types
    assert any('自动战斗' in s for s in skipped), skipped

    # 2) 关卡写进 FightTask，模板里其它参数原样保留
    fq = next(t for t in q if t['TaskType'] == 'Fight')
    assert fq['StagePlan'] == ['TL-5'], fq
    assert fq['IsEnable'] is True

    # 3) 建档案：只多一份 PRTS-，用户那份一个字段都不变
    info = maaplan.ensure_profile(
        {'profile': '活动', 'accountName': '181****5505',
         'tasks': ['startup', 'fight'], 'stages': ['TL-5'], 'maaDir': md}, md)
    assert info['profile'] == 'PRTS-活动', info
    with open(os.path.join(md, 'config', 'gui.new.json'), encoding='utf-8') as f:
        cfg2 = json.load(f)
    assert 'PRTS-活动' in cfg2['Configurations']
    assert cfg2['Configurations']['mine'] == user_prof, '用户档案被改了'
    ours = cfg2['Configurations']['PRTS-活动']
    assert ours['Gui']['StartUpSettings']['RunDirectly'] is True, '没打开启动即跑'
    su = next(t for t in ours['TaskQueue'] if t['TaskType'] == 'StartUp')
    assert su['AccountName'] == '181****5505', su
    assert su['AccountSwitchEnabled'] is True, '没打开切号开关'

    # 4) 档案名：带前缀、保留 *（官服切号名就靠它）
    assert maaplan._profile_name({'accountName': '181****5505'}) == 'PRTS-181****5505'
    assert maaplan._profile_name({'profile': 'PRTS-x'}) == 'PRTS-x'
    assert maaplan._profile_name({}) == 'PRTS-挂机'

    # 5) 直连序列：StartUp 带切号、Fight 带关卡、渠道映射对
    seq = maaplan.core_tasks({'tasks': ['startup', 'fight'], 'stages': ['TL-5'],
                              'accountName': '181****5505',
                              'channel': 'bilibili服'})
    assert [t for t, _ in seq] == ['StartUp', 'Fight'], seq
    assert seq[0][1]['client_type'] == 'Bilibili', seq[0]
    assert seq[0][1]['account_name'] == '181****5505'
    # MaaCore 的 StartUpTask 只认 account_name/client_type/start_game_enabled
    # 三个键（给了非空 account_name 就切号），不许再发它不认的假参数
    assert 'account_switch_enabled' not in seq[0][1], seq[0][1]
    # 明说了不切号（switchAccount=False）时，切号名整个不发
    seq2 = maaplan.core_tasks({'tasks': ['startup'], 'accountName': '5505',
                               'switchAccount': False})
    assert 'account_name' not in seq2[0][1], seq2[0][1]
    assert seq[1][1]['stage'] == 'TL-5'

    # 6) 抄作业：filename 必须绝对路径（相对路径会被 MaaCore 静默丢掉）
    old = copilot.fetch_prepared
    copilot.fetch_prepared = lambda cid, *a, **k: (
        {'stage_name': 'act55side_05', 'opers': []}, 1, 'act55side_05', [], True)
    try:
        rows = maaplan.prepare_copilot([{'copilotId': 108609, 'stageCode': 'TL-5'}], md)
    finally:
        copilot.fetch_prepared = old
    assert rows, '作业列表是空的'
    assert os.path.isabs(rows[0]['filename']), rows[0]
    assert rows[0]['filename'].endswith('TL-5_108609.json'), rows[0]
    assert rows[0]['stage_name'] == 'TL-5', rows[0]
    # 必须是**关卡代号**不是作业内部 id（act55side_05）：MaaCore 的
    # MultiCopilotTaskPlugin 拿它去拼 `<代号>@Copilot@ClickStageName`，
    # 传内部 id 拼出来的 pipeline 不存在，OCR 永远匹配不上
    assert os.path.isfile(rows[0]['filename']), '作业文件没落盘'
    cq = maaplan.core_tasks({'tasks': ['copilot'], 'copilotList': rows,
                             'copilot': {'formation': True}})
    # core_tasks 会自动补一个 StartUp 在最前面（没有唤醒什么都跑不了）
    assert [t for t, _ in cq] == ['StartUp', 'Copilot'], cq
    cp = cq[-1][1]
    assert cp['copilot_list'][0]['filename'] == rows[0]['filename']
    assert cp['formation'] is True

    # 7) 渠道 → client_type
    for cn, code in [('官服', 'Official'), ('bilibili服', 'Bilibili'),
                     ('B服', 'Bilibili'), ('日服', 'YoStarJP'), ('', 'Official')]:
        assert maaplan.client_type_of(cn) == code, (cn, maaplan.client_type_of(cn))

    # 8) 引擎事件翻译（纯函数，不碰设备）
    eng = maacore.Engine.__new__(maacore.Engine)
    eng.connected = False
    eng._chain_starts = 0
    ev = eng._translate(maacore.MSG_CONNECTION_INFO, {'what': 'Connected'})
    assert ev['kind'] == 'connected' and eng.connected, ev
    ev = eng._translate(maacore.MSG_CONNECTION_INFO, {'what': 'ConnectFailed'})
    assert ev['kind'] == 'error', ev
    assert eng._translate(maacore.MSG_ALL_TASKS_COMPLETED, {})['kind'] == 'done'
    assert '自动战斗' in eng._translate(
        maacore.MSG_TASK_START, {'taskchain': 'Copilot'})['text']
    assert maacore._chain_cn('Fight') == '理智作战'

    # 9) 停挂机：直连跑的时候机器上没有 MAA 进程，只能让引擎自己 AsstStop。
    #    空表返回 0 就够——真停要设备，这里钉住「有这个出口且不会炸」。
    assert maacore.stop_all_live() == 0

    # 10) capabilities 带上核心版本位（GUI 6.20.1 但核心可能单独更新过，
    #     版本对不上时 AsstAppendTask 会静默不收，所以前端要能看到）
    cap = maaplan.capabilities(md)
    assert 'coreVersion' in cap and isinstance(cap['coreVersion'], str), cap
    assert len(cap['tasks']) == 9, len(cap['tasks'])
    assert all('available' in t for t in cap['tasks']), cap['tasks'][:1]

    # 11) build_run(start=false)：只建档案，不启动、不连模拟器
    from core import agent
    res = agent._start_build({
        'rows': [{'name': '测号', 'accountName': '181****5505'}],
        'tasks': ['startup', 'fight', 'copilot'], 'fightStages': ['TL-5'],
        'formation': True, 'start': False, 'maaDir': md, 'profileHint': '只配不跑',
    })
    assert res['started'] is False, res
    assert res['profiles'] == ['PRTS-只配不跑'], res
    assert 'copilotNote' in res, '抄作业配不进档案，必须如实告诉用户'
    with open(os.path.join(md, 'config', 'gui.new.json'), encoding='utf-8') as f:
        cfg3 = json.load(f)
    assert cfg3['Configurations']['mine'] == user_prof, '用户档案被改了'
    assert cfg3['Configurations']['PRTS-只配不跑']['TaskQueue'], '队列是空的'


def check_op_blacklist(tmp):
    """作业黑名单：条目按 MAA 代码存（maa://12345），归一化容错（裸数字补全、
    去重、非法丢弃）；fetch_ops 出口按代码滤，与作者黑名单同一出口"""
    from core import copilot, settings

    _reset_config(tmp)

    saved = settings.save({'opBlacklist': ['maa://1', '456', 456, 'x', 'maa://abc', '']})
    assert saved['opBlacklist'] == ['maa://1', 'maa://456'], saved['opBlacklist']
    settings.save({'opBlacklist': []})      # 归一化段存的条目别带进下面的过滤段

    # fetch_ops 出口过滤：patch 掉网络层，只验证过滤逻辑
    fake = [
        {'meta': {'id': 1, 'uploader_id': '10'}},
        {'meta': {'id': 2, 'uploader_id': '20'}},
        {'meta': {'id': 3, 'uploader_id': '30'}},
    ]
    orig = copilot._fetch_ops_raw
    copilot._fetch_ops_raw = lambda stage, pages=2, min_rating=0, force=False: list(fake)
    try:
        stage = {'stageId': 's', 'code': 'c'}
        got = [i['meta']['id'] for i in copilot.fetch_ops(stage)]
        assert got == [1, 2, 3], got
        settings.save({'opBlacklist': ['maa://2']})
        got = [i['meta']['id'] for i in copilot.fetch_ops(stage)]
        assert got == [1, 3], got                          # 作业黑名单按代码滤
        settings.save({'authorBlacklist': [{'id': '30', 'name': ''}]})
        got = [i['meta']['id'] for i in copilot.fetch_ops(stage)]
        assert got == [1], got                            # 作者黑名单同一出口
    finally:
        copilot._fetch_ops_raw = orig


# ---------------------------------------------------------------- B24 抽卡统计
def _grec(ts, pid, pname, rarity, name='干员'):
    return {'category': 'cat', 'poolId': pid, 'poolName': pname, 'charId': 'ch_' + name,
            'charName': name, 'rarity': rarity, 'isNew': False,
            'gachaTs': str(ts), 'pos': 0}


def check_gacha_stats(tmp):
    """抽卡统计（纯本地逻辑，不联网）：

    - 星级归一：明日方舟 0 基、终末地口径未知，统一到 1~6
    - 保底按 **池子 id** 连续计数、出六星清零；展示按池名合并（同名不同期不刷屏）
    - 缓存视图：有/无 token 都能看已有缓存，抽卡数/六星数与记录一致
    - 两个游戏账号即便 uid 相同也不能互相认领对方的文件
    """
    from core import api, gacha, updater

    _reset_config(tmp)

    # 1) 星级归一
    assert gacha._norm_rarity(5) == 6, '0 基 5 应该是六星'
    assert gacha._norm_rarity(0) == 0
    assert gacha._norm_rarity(6) == 6, '已是 1 基的 6 不能再 +1'
    assert gacha._norm_rarity(None) == 0 and gacha._norm_rarity('x') == 0

    # 2) 汇总：P1 抽 4 次出 2 个六星、P2 抽 1 次出 1 个六星，交替顺序不能串池
    base = 1700000000000
    recs = [
        _grec(base + 60000, 'P1', '标准寻访', 3),
        _grec(base + 120000, 'P1', '标准寻访', 6, '六星甲'),
        _grec(base + 180000, 'P1', '标准寻访', 5),
        _grec(base + 240000, 'P2', '限定寻访', 6, '六星乙'),
        _grec(base + 300000, 'P1', '标准寻访', 6, '六星丙'),
    ]
    s = gacha.summarize(list(reversed(recs)))     # 故意打乱输入顺序
    assert s['total'] == 5, s['total']
    assert s['sixCount'] == 3, s['sixCount']
    assert s['byRarity']['6'] == 3 and s['byRarity']['5'] == 1 and s['byRarity']['3'] == 1, s['byRarity']
    assert [x['charName'] for x in s['sixStars']] == ['六星丙', '六星乙', '六星甲'], s['sixStars']
    assert [x['pity'] for x in s['sixStars']] == [2, 1, 2], s['sixStars']
    assert s['avgPity'] == 1.7, s['avgPity']
    assert s['lastSixGap'] == 0, s['lastSixGap']       # 最新一条就是六星
    assert s['rate6'] == 0.6, s['rate6']
    assert [(p['poolName'], p['count'], p['six']) for p in s['pools']] == [
        ('标准寻访', 4, 2), ('限定寻访', 1, 1)], s['pools']

    # 2b) 卡池按最近抽卡时间倒序：抽得少但更近的池子排最上面（不按抽数排）
    late = [
        _grec(base + 10000, 'PA', '老池', 3),
        _grec(base + 20000, 'PA', '老池', 3),
        _grec(base + 30000, 'PA', '老池', 3),
        _grec(base + 40000, 'PB', '新池', 4),
    ]
    order = [p['poolName'] for p in gacha.summarize(late)['pools']]
    assert order == ['新池', '老池'], order           # 新池只 1 抽也排前面
    assert 'lastMs' not in gacha.summarize(late)['pools'][0], '排序字段不该外泄'
    assert s['records'][0]['charName'] == '六星丙', s['records'][0]
    assert s['firstAt'] and s['lastAt'] and s['firstAt'] < s['lastAt'], (s['firstAt'], s['lastAt'])

    # 3) 缓存视图：缓存与 token 相互独立（没 token 也能看历史）
    path = gacha.cache_path('测试 官服.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'updatedAt': '2026-11-10T18:00:00+08:00',
                   'game': 'arknights', 'uid': '1', 'records': recs}, f)
    meta = {'game': 'arknights', 'uid': '1', 'nickName': '测试'}
    v = gacha.view('测试 官服.json', 'TOKEN', meta)
    assert v['hasToken'] is True and v['count'] == 5 and v['summary']['total'] == 5, v
    v2 = gacha.view('测试 官服.json', '', meta)
    assert v2['hasToken'] is False and v2['summary']['sixCount'] == 3, v2

    # 4) 终末地账号快照：game/roleId 落盘，不写练度段
    snap = updater._ef_snapshot(
        {'uid': '697277796', 'roleId': '1447867726', 'nickName': '红颜断清梦',
         'channelName': '官服', 'serverName': 'China', 'level': 57}, 'TOKEN')
    assert snap['account']['game'] == 'endfield', snap['account']
    assert snap['account']['roleId'] == '1447867726'
    assert snap['stats'] == {} and snap['chars'] == [] and snap['missing'] == []

    # 5) 同 uid 跨游戏不串号：scan_accounts 必须按 game 隔离
    cfg = os.path.join(tmp, 'config')
    with open(os.path.join(cfg, 'A 官服.json'), 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '999', 'game': 'arknights', 'nickName': 'A'}}, f)
    with open(os.path.join(cfg, 'A 终末地.json'), 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '999', 'game': 'endfield', 'nickName': 'A'}}, f)
    ark = updater.scan_accounts(cfg)
    ef = updater.scan_accounts(cfg, 'endfield')
    assert list(ark) == ['999'] and ark['999'].endswith('A 官服.json'), ark
    assert list(ef) == ['999'] and ef['999'].endswith('A 终末地.json'), ef

    # 6) 路由已注册（前端抽卡统计的入口）
    assert '/api/gacha' in api.ROUTES and '/api/gacha/sync' in api.ROUTES

    # 7) 终末地星级是 1 基（4/5/6），不能套明日方舟那套 +1；非抽卡记录要丢掉
    assert gacha._norm_ef_rarity(4) == 4 and gacha._norm_ef_rarity(6) == 6
    assert gacha._norm_ef_rarity(None) == 0 and gacha._norm_ef_rarity(9) == 0
    assert gacha._ef_norm({'kind': 'gift_intel_book', 'seqId': '9'}, 'C') == {}
    w = gacha._ef_norm({'kind': 'draw', 'poolId': 'weaponbox_1_6_1', 'poolName': '篆玉申领',
                        'weaponId': 'wpn_1', 'weaponName': '呼啸守卫', 'rarity': 6,
                        'gachaTs': '1792038254646', 'seqId': '740'},
                       gacha.EF_WEAPON_CATEGORY)
    assert w['kind'] == 'weapon' and w['charName'] == '呼啸守卫' and w['charId'] == 'wpn_1', w
    assert w['rarity'] == 6 and w['pos'] == 740, w

    # 8) 终末地翻页：服务端只认 seq_id 游标（size/limit 全被忽略、每页固定 5 条），
    #    不传游标就永远只拿到最新一页；stop_below 接上本地缓存要立刻停
    calls = []

    def _fake_get(url, headers=None, timeout=25):
        q = dict(urllib.parse.parse_qsl(url.split('?', 1)[1]))
        calls.append(q)
        end = int(q.get('seq_id') or 21)
        rows = [{'kind': 'draw', 'seqId': str(i), 'rarity': 4, 'poolId': 'p',
                 'poolName': '池', 'gachaTs': str(1700000000000 + i)}
                for i in range(end - 1, max(0, end - 6), -1)]
        return {'code': 0, 'data': {'list': rows, 'hasMore': len(rows) >= 5}}

    _orig_get = gacha._get_json
    gacha._get_json = _fake_get
    try:
        full = gacha._ef_drain('http://x', {'token': 't'}, 'C', '测试')
        assert len(full) == 20, len(full)              # seq 21..1 一路翻到底
        assert calls[0].get('seq_id') is None, calls[0]
        assert 'size' not in calls[0] and 'page' not in calls[0], calls[0]
        assert len(calls) == 5, len(calls)
        calls.clear()
        part = gacha._ef_drain('http://x', {'token': 't'}, 'C', '测试', stop_below=17)
        assert len(part) == 5 and len(calls) == 1, (len(part), len(calls))
    finally:
        gacha._get_json = _orig_get

    # 9) 终末地两套口径：角色寻访与武器申领分开算（保底/出率不能混），总数是两者之和
    ef_recs = [
        {'category': 'c', 'kind': 'char', 'poolId': 'c1', 'poolName': '限定寻访',
         'charName': '角色甲', 'rarity': 6, 'gachaTs': str(base + 1000), 'pos': 11},
        {'category': 'c', 'kind': 'char', 'poolId': 'c1', 'poolName': '限定寻访',
         'charName': '角色乙', 'rarity': 4, 'gachaTs': str(base + 2000), 'pos': 12},
        {'category': 'w', 'kind': 'weapon', 'poolId': 'w1', 'poolName': '篆玉申领',
         'charName': '武器甲', 'rarity': 6, 'gachaTs': str(base + 3000), 'pos': 21},
    ]
    s2 = gacha.summarize(ef_recs)
    assert s2['total'] == 3 and s2['sixCount'] == 2, s2
    # 两套口径各给一份完整汇总（前端按 tab 分别渲染整套面板）
    assert s2['char']['total'] == 2 and s2['char']['sixCount'] == 1, s2['char']
    assert s2['weapon']['total'] == 1 and s2['weapon']['sixCount'] == 1, s2['weapon']
    assert s2['char']['rate6'] == 0.5 and s2['weapon']['rate6'] == 1.0, s2
    assert s2['char']['avgPity'] == 1 and s2['weapon']['avgPity'] == 1, s2
    # 每个 tab 的「按卡池」只含自己那一套，池子下面挂着自己的六星记录
    assert [p['poolName'] for p in s2['char']['pools']] == ['限定寻访'], s2['char']['pools']
    assert [p['poolName'] for p in s2['weapon']['pools']] == ['篆玉申领'], s2['weapon']['pools']
    wpool = s2['weapon']['pools'][0]
    assert wpool['kind'] == 'weapon' and wpool['six'] == 1, wpool
    assert [(x['charName'], x['pity']) for x in wpool['sixItems']] == [('武器甲', 1)], wpool
    assert [x['charName'] for x in s2['weapon']['sixStars']] == ['武器甲'], s2['weapon']['sixStars']
    assert [x['charName'] for x in s2['weapon']['records']] == ['武器甲'], s2['weapon']['records']
    assert s2['char']['firstAt'] and s2['weapon']['firstAt'] == s2['weapon']['lastAt'], s2
    # 全是角色记录时 weapon 为 None（前端据此决定要不要给武器池 tab）
    assert gacha.summarize(ef_recs[:2])['weapon'] is None
    assert {p['poolName']: p['kind'] for p in s2['pools']} == {
        '限定寻访': 'char', '篆玉申领': 'weapon'}, s2['pools']

    # 10) 旧版终末地缓存（version 1，只取了每池第一页且星级错位）直接作废重建
    ef_path = gacha.cache_path('旧 终末地.json')
    os.makedirs(os.path.dirname(ef_path), exist_ok=True)
    with open(ef_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'game': 'endfield', 'uid': '9',
                   'records': [_grec(base, 'P', '限定寻访', 5)]}, f)
    assert gacha.load_cache('旧 终末地.json') == {}, 'v1 终末地缓存必须丢弃'
    with open(ef_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 2, 'game': 'endfield', 'uid': '9',
                   'records': [_grec(base, 'P', '限定寻访', 5)]}, f)
    assert len(gacha.load_cache('旧 终末地.json').get('records') or []) == 1


# ---------------------------------------------------------------- B25 森空岛签到
def check_signin(_tmp):
    """森空岛签到（不联网，全用假客户端）：

    - 两套口径：方舟 POST {uid, gameId:1}；终末地 POST + header
      sk-game-role: 3_<roleId>_<serverId>（拿 uid 打方舟接口会 403）
    - 今天签过（方舟 records 里有今天的 ts / 终末地 hasToday）就不发签到请求
    - 签到撞 HTTP 403 = 今天已签到，算 already 不算失败
    - 没 token / 解不开 token / 终末地缺 roleId 一律跳过；坏 token 只登录一次
    - 同一角色被两个账号文件指向时只签一次
    """
    import types
    import urllib.error

    from core import accounts, api, signin

    now = time.time()
    rows = [
        {'file': 'a 官服.json', 'nickName': 'A', 'game': 'arknights', 'uid': '1', 'hasToken': True},
        {'file': 'b 官服.json', 'nickName': 'B', 'game': 'arknights', 'uid': '2', 'hasToken': True},
        {'file': 'c 终末地.json', 'nickName': 'C', 'game': 'endfield', 'uid': '9',
         'roleId': '1715413040', 'serverId': '1', 'hasToken': True},
        {'file': 'a-dup 官服.json', 'nickName': 'A2', 'game': 'arknights', 'uid': '1', 'hasToken': True},
        {'file': 'no-token.json', 'nickName': 'N', 'game': 'arknights', 'uid': '3', 'hasToken': False},
        {'file': 'bad 官服.json', 'nickName': 'X', 'game': 'arknights', 'uid': '4', 'hasToken': True},
        {'file': 'bad2 官服.json', 'nickName': 'Y', 'game': 'arknights', 'uid': '5', 'hasToken': True},
        {'file': 'd 官服.json', 'nickName': 'D', 'game': 'arknights', 'uid': '6', 'hasToken': True},
        {'file': 'e 终末地.json', 'nickName': 'E', 'game': 'endfield', 'uid': '10', 'hasToken': True},
        {'file': 'f 终末地.json', 'nickName': 'F', 'game': 'endfield', 'uid': '11',
         'roleId': '222', 'serverId': '2', 'hasToken': True},
    ]
    tokens = {'a 官服.json': 'T1', 'b 官服.json': 'T2', 'c 终末地.json': 'T3',
              'a-dup 官服.json': 'T1', 'no-token.json': '', 'bad 官服.json': 'BAD',
              'bad2 官服.json': 'BAD', 'd 官服.json': 'T403', 'e 终末地.json': 'T4',
              'f 终末地.json': 'T5'}

    class FakeSk:
        """假的森空岛客户端：记录请求，按 token 决定「今天签没签」"""

        made = []
        inst = {}

        def __init__(self, token, log=None):
            if token == 'BAD':
                raise RuntimeError('token 无效')
            FakeSk.made.append(token)
            FakeSk.inst[token] = self
            self.token = token
            self.posts = []

        def api_get(self, path, **kw):
            if path.startswith(signin.EF_PATH):
                assert kw.get('headers', {}).get('sk-game-role'), '终末地必须带 sk-game-role'
                return {'code': 0, 'data': {'hasToday': self.token != 'T5'}}
            return {'code': 0, 'data': {
                'records': ([{'ts': str(int(now))}] if self.token == 'T2' else [])}}

        def api_post(self, path, body=None, headers=None, **kw):
            self.posts.append((path, body, headers))
            if self.token == 'T403':
                raise urllib.error.HTTPError('u', 403, 'Forbidden', None, None)
            if path == signin.EF_PATH:
                return {'code': 0, 'data': {
                    'awardIds': [{'id': 'x'}],
                    'resourceInfoMap': {'x': {'name': '折金票', 'count': 2000}}}}
            return {'code': 0, 'data': {
                'awards': [{'resource': {'name': '合成玉'}, 'count': 300}]}}

    # 0) 纯函数：日期判定与终末地身份头
    assert signin._is_today(now) is True, '今天的时间戳应该判成今天'
    assert signin._is_today(now - 3 * 86400) is False, '三天前不是今天'
    assert signin._is_today('') is False and signin._is_today('x') is False
    h = signin._ef_headers({'roleId': '1', 'serverId': '2'})
    assert h['sk-game-role'] == '3_1_2', h          # gameId_roleId_serverId
    assert '/api/signin/run' in api.ROUTES, '签到接口没挂上 ROUTES'

    orig_rows, orig_tok = accounts.list_accounts, accounts.token_for
    orig_sk, orig_time = signin.skland.Skland, signin.time
    try:
        accounts.list_accounts = lambda: [dict(r) for r in rows]
        accounts.token_for = lambda f: tokens.get(f, '')
        signin.skland.Skland = FakeSk
        signin.time = types.SimpleNamespace(sleep=lambda s: None)   # 别真睡 0.4 秒 × 10

        res = signin.run('', log=lambda s: None)
        c = res['counts']
        assert c == {'ok': 2, 'already': 3, 'fail': 2, 'skip': 3}, c
        assert len(res['items']) == len(rows), len(res['items'])

        # 每个 token 只登录一次（a-dup 直接跳过，没有多登录一次 T1）
        assert FakeSk.made == ['T1', 'T2', 'T3', 'T403', 'T4', 'T5'], FakeSk.made

        # 方舟：POST {uid, gameId:1}
        path, body, _h = FakeSk.inst['T1'].posts[-1]
        assert path == signin.ARK_PATH and body == {'uid': '1', 'gameId': 1}, (path, body)

        # 终末地：body 为空，身份在头上
        path, body, hdr = FakeSk.inst['T5'].posts[-1]
        assert path == signin.EF_PATH and body is None, (path, body)
        assert hdr['sk-game-role'] == '3_222_2', hdr
        assert hdr['Origin'] == 'https://game.skland.com/', hdr

        # 今天签过的号不发签到请求
        assert FakeSk.inst['T2'].posts == [], FakeSk.inst['T2'].posts

        # 403 记成「已签到」而不是失败
        states = {i['file']: i['state'] for i in res['items']}
        assert states['d 官服.json'] == 'already', states
        assert states['e 终末地.json'] == 'skip', states     # 缺 roleId
        assert states['no-token.json'] == 'skip' and states['a-dup 官服.json'] == 'skip', states
        assert states['bad 官服.json'] == 'fail', states

        # 单号签到：只打这一个号
        one = signin.run('a 官服.json', log=lambda s: None)
        assert one['counts'] == {'ok': 1, 'already': 0, 'fail': 0, 'skip': 0}, one['counts']

        # 不存在的账号要报错，而不是悄悄当全局签
        try:
            signin.run('不存在.json', log=lambda s: None)
        except RuntimeError:
            pass
        else:
            raise AssertionError('不存在的账号应该报错')
    finally:
        accounts.list_accounts, accounts.token_for = orig_rows, orig_tok
        signin.skland.Skland, signin.time = orig_sk, orig_time

def check_skland_http_body(_tmp):
    """skland HTTP 错误响应体进任务日志（不联网，假 urlopen）：

    - 可重签名请求的 401：每轮重试行带响应体，重试耗尽抛出前再单记一行
    - 登录链（dict headers）的 401：不重试，抛出前只记一行响应体
    - 空响应体：不加 [HTTP] 行，重试行不带尾巴
    """
    import io
    import types
    import urllib.error
    from core import skland

    calls, logs = [], []
    body = b'{"code":10001,"message":"invalid sign"}'

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {},
                                     io.BytesIO(body))

    orig_urlopen, orig_time = urllib.request.urlopen, skland.time
    try:
        urllib.request.urlopen = fake_urlopen
        skland.time = types.SimpleNamespace(sleep=lambda s: None)

        # 可重签名请求：重签两轮后抛出，每轮日志都带响应体
        try:
            skland._http('GET', 'https://x/api', lambda: {}, note=logs.append)
        except urllib.error.HTTPError:
            pass
        else:
            raise AssertionError('401 重试耗尽应抛 HTTPError')
        assert len(calls) == 3, len(calls)
        assert logs == [
            '    [签名重试 1/2] HTTP 401 ' + body.decode(),
            '    [签名重试 2/2] HTTP 401 ' + body.decode(),
            '    [HTTP 401] ' + body.decode(),
        ], logs

        # 登录链：dict headers 的 401 直接抛出，只记一行响应体、不重试
        calls.clear(); logs.clear()
        try:
            skland._http('POST', 'https://x/auth', {}, body={'t': 1}, note=logs.append)
        except urllib.error.HTTPError:
            pass
        else:
            raise AssertionError('登录链 401 应直接抛出')
        assert len(calls) == 1, len(calls)
        assert logs == ['    [HTTP 401] ' + body.decode()], logs

        # 空响应体：不加 [HTTP] 行，重试行也不带尾巴
        def no_body(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {},
                                         io.BytesIO(b''))
        urllib.request.urlopen = no_body
        logs.clear()
        try:
            skland._http('GET', 'https://x/api', lambda: {}, retries=2, note=logs.append)
        except urllib.error.HTTPError:
            pass
        assert logs == ['    [签名重试 1/1] HTTP 401'], logs
    finally:
        urllib.request.urlopen = orig_urlopen
        skland.time = orig_time


def check_applog_capture(_tmp):
    """B27：失败现场落 debug/app.log（core 侧写入端 applog）

    - applog.log 写 settings.LOG_PATH（隔离根下，不碰真实用户数据）
    - skland HTTP 401：界面任务日志维持 200 截断口径不变，app.log 记
      完整响应体，且只记 host+path（query 带 uid，不落盘）
    - 后台任务失败：finish 时摘要 + traceback 落 app.log
    """
    import io
    import types
    import urllib.error
    from core import applog, settings, skland, tasks

    def read_log():
        try:
            with open(settings.LOG_PATH, encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return ''

    applog.log('B27 自检标记 xyz123')
    assert 'B27 自检标记 xyz123' in read_log(), 'applog 没写进 settings.LOG_PATH'

    # skland 401：任务日志 200 截断、app.log 全量
    long_tail = 'T' * 300
    body = '{"code":10001,"message":"invalid sign","detail":"' + long_tail + '"}'

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {},
                                     io.BytesIO(body.encode('utf-8')))

    logs = []
    orig_urlopen, orig_time = urllib.request.urlopen, skland.time
    try:
        urllib.request.urlopen = fake_urlopen
        skland.time = types.SimpleNamespace(sleep=lambda s: None)
        try:
            skland._http('GET', 'https://x/api/v1/game/player/info?uid=1',
                         lambda: {}, note=logs.append)
        except urllib.error.HTTPError:
            pass
    finally:
        urllib.request.urlopen = orig_urlopen
        skland.time = orig_time

    assert logs and logs[-1].endswith(body[:200]), logs[-1]
    assert 'T' * 220 not in ''.join(logs), '任务日志不该带完整响应体'
    filed = read_log()
    assert '[森空岛] HTTP 401 GET x/api/v1/game/player/info' in filed, 'app.log 缺 401 行'
    assert body in filed, 'app.log 应记完整响应体（不截 200）'
    assert 'uid=1' not in filed, 'app.log 不该带 query（账号参数不落盘）'

    # 后台任务失败：摘要 + traceback 落 app.log
    def boom():
        raise RuntimeError('炸给 B27 看')

    tid = tasks.create('B27 失败任务', boom)
    for _ in range(50):
        st = tasks.get(tid)
        if st and st['status'] != 'running':
            break
        time.sleep(0.1)
    assert st and st['status'] == 'error', st
    filed = read_log()
    assert '[任务失败] B27 失败任务：RuntimeError: 炸给 B27 看' in filed, '缺任务失败摘要'
    assert 'boom' in filed, '缺 traceback 现场'


# ---------------------------------------------------------------- B34 活动关导航
def check_stagenav(tmp):
    """B34：活动关导航（区域行号推算 / 页面判别 / 商店保护）

    这块全靠固定坐标 + 截图判断，最容易被"顺手改一下"改坏，所以把两条稳定
    契约钉住：代号→区域行号、当前页面的分类。
    """
    from core import stagenav

    # 1) 代号 → 区域行号（0=残阳终尽 1=长冬初青 2=世纪之春）
    cases = {
        'TL-S-1': 2,        # 当期新关，本地资源库还没收录 → 走后缀规则
        'TL-S-3': 2,
        'TL-EX-1': 1,       # EX 挑战关在第二个区域
        'TL-5': 0,          # 本地查得到 zoneId → 走精确路径
        '1-7': None,        # 主线关不归这段导航管
        '15-3': None,
        'S4-1': None,       # 「1 个字母 + 章节号」的主线特殊关
        'R8-2': None,
        'act55side_s01': None,   # 作业内部 id，调用方本来就该传代号
    }
    for code, want in cases.items():
        got = stagenav.zone_rows_of(code)
        assert got == want, (code, got, want)

    # 2) 导航点齐全（少一个就是 KeyError 直接炸导航）
    p = stagenav.points()
    for k in ('tabTerminal', 'activityCard', 'themeZone', 'zoneRows',
              'backCorner'):
        assert k in p, '缺导航点：%s' % k
    assert len(p['zoneRows']) == 3, p['zoneRows']

    # 3) 页面判别：造四张图钉阈值（底部白占比 / 中部纯白 + 亮度）
    import io

    from PIL import Image, ImageDraw

    class Shot:
        def __init__(self, data):
            self._d = data

        def shot(self):
            return self._d

    def png(tab_white, mid_pure=0.0, mid_fill=(60, 60, 60)):
        im = Image.new('RGB', (1280, 720), (60, 60, 60))
        d = ImageDraw.Draw(im)
        x1, y1 = int(0.01 * 1280), int(0.86 * 720)
        x2, y2 = int(0.35 * 1280), int(0.94 * 720)
        if tab_white > 0:
            d.rectangle([x1, y1, x1 + int((x2 - x1) * tab_white), y2],
                        fill=(255, 255, 255))
        mx1, my1 = int(0.30 * 1280), int(0.35 * 720)
        mw, mh = int(0.40 * 1280), int(0.20 * 720)
        d.rectangle([mx1, my1, mx1 + mw, my1 + mh], fill=mid_fill)
        if mid_pure > 0:
            d.rectangle([mx1, my1, mx1 + int(mw * mid_pure), my1 + mh],
                        fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        return Shot(buf.getvalue())

    assert stagenav.probe(png(0.0)) == 'map', '底部全暗应判成活动地图'
    assert stagenav.probe(png(0.30)) == 'home', '底部半白应判成主界面'
    assert stagenav.probe(png(0.85, 0.0, (225, 225, 225))) == 'theme', \
        '底部大半白 + 中部亮应判成活动主题页'
    assert stagenav.probe(png(0.50, 0.40)) == 'store', \
        '中部成片纯白（商品卡片）应判成活动商店'

    # 4) 主线关不点：直接返回 False，绝不能对主线关乱点活动入口
    class NullTouch:
        def __init__(self):
            self.hits = []

        def tap(self, x, y):
            self.hits.append((x, y))
            return True

        def shot(self):
            return b''

    nt = NullTouch()
    ok, why = stagenav.goto_stage_list(nt, '1-7')
    assert ok is False and '不是活动关' in why, (ok, why)
    assert nt.hits == [], '主线关不该产生任何点击：%s' % (nt.hits,)


# ---------------------------------------------------------------- B35 老故事集导航
def check_story_nav(tmp):
    """B35：老故事集导航（曲谱入口 / 卡片模板匹配）

    MN 这类老活动 MAA 资源里没有导航配置，``Fight`` 直接拒收（实测
    ``AsstAppendTask`` 返回 0），只能自己从「曲谱 → 乐章收录」的网格里按图
    找活动卡片。这里钉住三件事：模板找得到、匹配定位准、曲谱页判据。
    """
    import io as _io

    from PIL import Image, ImageDraw

    from core import stagenav

    # 1) 模板查找：MN 有卡片模板，当期活动（TL）不该有
    assert stagenav.tpl_path_of('MN-7'), 'MN-7 应该能找到卡片模板'
    assert stagenav.tpl_path_of('MN-EX-1'), 'MN-EX-1 也该命中同一个模板'
    assert stagenav.tpl_path_of('TL-9') == '', 'TL-9 不该有卡片模板'
    assert stagenav.tpl_path_of('1-7') == '', '主线关不该有卡片模板'

    tpl_path = stagenav.tpl_path_of('MN-7')
    tpl = Image.open(tpl_path).convert('L')

    class Shot:
        def __init__(self, data):
            self._d = data

        def shot(self):
            return self._d

    # 2) 模板匹配：贴到黑底大图的固定位置，得能找回那个位置
    canvas = Image.new('L', (1920, 1080), 8)
    px, py = 520, 380
    canvas.paste(tpl, (px, py))
    buf = _io.BytesIO()
    canvas.save(buf, 'PNG')
    x, y, s = stagenav.find_card(Shot(buf.getvalue()), tpl_path)
    want = (px + tpl.width // 2, py + tpl.height // 2)
    assert x and abs(x - want[0]) <= 12 and abs(y - want[1]) <= 12, \
        ('模板定位偏了', (x, y), want, s)
    assert s <= stagenav._CARD_MAX_SCORE, ('分数应该很低才对', s)

    # 3) 曲谱页判据：暗底 True / 亮底 False（实测 曲谱 42 / 网格 53 / 主界面 99~130）
    def gray(v):
        im = Image.new('L', (1280, 720), v)
        b = _io.BytesIO()
        im.save(b, 'PNG')
        return Shot(b.getvalue())

    assert stagenav.in_story_page(gray(45)) is True, '暗底应判成曲谱页'
    assert stagenav.in_story_page(gray(128)) is False, '亮底不该判成曲谱页'

    # 只靠「左上角够暗」会把主界面判成曲谱页：皮肤立绘压在左边，实测灰度
    # 98.5，正好卡在旧阈值 100 下面。后果是登录后一步都没走就开始翻三十几屏，
    # 最后报「翻了几十屏都没找到」——2026-11-17 用户报的跳转失败就是这个。
    # 这里造一张「左上暗 + 左下角那块礼包白卡」的主界面：必须判 False。
    im = Image.new('RGB', (1280, 720), (45, 45, 45))
    dr = ImageDraw.Draw(im)
    dr.rectangle([int(0.02 * 1280), int(0.86 * 720),
                  int(0.35 * 1280), int(0.98 * 720)],
                 fill=(255, 255, 255))
    b = _io.BytesIO()
    im.save(b, 'PNG')
    assert stagenav.in_story_page(Shot(b.getvalue())) is False, \
        '左下角有白色功能卡的是主界面，不能判成曲谱页'

    # 3.5) 「乐章收录」网格判据 + 进网格必点「默认进度」。
    #      这一页会记住上次选的标签：停在「完成进度」时每张卡左上角都盖着
    #      进度徽章（38/39 那种蓝色菱形），卡片图被改掉一块，模板匹配从 5 分
    #      涨到 20 分以上——整轮扫描白跑（2026-11-18 用户报 RI 导航失败，
    #      现场截图正停在「完成进度」）。判据就是顶部三个标签必有一个蓝字。
    def rgb_box(blue):
        im = Image.new('RGB', (1920, 1080), (18, 18, 22))
        dr = ImageDraw.Draw(im)
        if blue:
            dr.rectangle([int(0.57 * 1920), int(0.04 * 1080),
                          int(0.62 * 1920), int(0.085 * 1080)],
                         fill=(60, 170, 255))
        b = _io.BytesIO()
        im.save(b, 'PNG')
        return Shot(b.getvalue())

    assert stagenav.in_score_grid(rgb_box(True)) is True, \
        '有蓝色标签的应该是乐章收录网格'
    assert stagenav.in_score_grid(rgb_box(False)) is False, \
        '没有蓝色标签的（曲谱时间轴）不该判成网格'

    p = stagenav.points()
    assert 'tabProgress' in p and len(p['tabProgress']) == 2, \
        ('要有一个「默认进度」标签的点击点', p.get('tabProgress'))
    src = inspect.getsource(stagenav.goto_story_activity)
    assert "p['tabProgress']" in src, '进网格后必须点一下「默认进度」'
    assert 'in_score_grid' in src, '点完「乐章收录」要确认真的进了网格'

    # 3.6) 扫描要「等画面停稳再截图」：滑动惯性能吃好几秒，中途截到的帧整屏
    #      带拖影（卡片被拉成两半），匹配分数虚高 → 明明在屏幕上却报没找到。
    assert hasattr(stagenav, '_settled_shot'), '截图前要先等画面停稳'
    assert '_settled_shot' in inspect.getsource(stagenav.goto_story_activity), \
        '扫描循环必须用 _settled_shot 取帧'

    # 3.7) 回顶要甩够（旧写法从列表底部到不了顶，扫描会从中间开始漏上半张表），
    #      卡片位置缓存读不到时要安静返回 0（不能因为少个 json 就导航不了）。
    assert hasattr(stagenav, '_scroll_top'), '要有「甩到顶」的辅助函数'
    assert '_scroll_top' in src, '整页扫之前必须先回顶'
    assert stagenav._load_card_offset('ZZZ') == 0, '缓存读不到要返回 0'

    # 4) 九张老活动的卡片模板都在（少一张就意味着那个活动导航不了）
    for code, name in (('MN-1', 'MN'), ('WD-1', 'WD'), ('DM-1', 'DM'),
                       ('MB-1', 'MB'), ('BI-1', 'BI'), ('TW-1', 'TW'),
                       ('RI-1', 'RI'), ('SV-1', 'SV'), ('WR-1', 'WR')):
        assert stagenav.tpl_path_of(code), '缺卡片模板：%s' % name

    # 5) EX / S / MO 关识别与「分区图标」坐标
    #    活动地图右下角不是「一个 EX 开关」，是 2~4 个分区图标（基础 / EX /
    #    S / MO），点哪个切哪个；EX 也不总在最右边（DM/BB/MT/PV 在中间）。
    assert stagenav.is_extra('MN-EX-1') is True
    assert stagenav.is_extra('MN-EX-8') is True
    assert stagenav.is_extra('MN-7') is False
    assert stagenav.is_extra('TL-S-1') is False
    assert stagenav.ex_point('MN-EX-1') == (1199, 620), stagenav.ex_point('MN-EX-1')
    assert stagenav.ex_point('WD-EX-3') == (1199, 620)

    # 分区类型：EX / S / MO / ST / 基础
    assert stagenav.zone_kind('DM-EX-1') == 'ex', stagenav.zone_kind('DM-EX-1')
    assert stagenav.zone_kind('OF-EX1') == 'ex', '火蓝之心是 EX1 写法'
    assert stagenav.zone_kind('BB-S-1') == 's', stagenav.zone_kind('BB-S-1')
    assert stagenav.zone_kind('MT-S-2') == 's'
    assert stagenav.zone_kind('DM-MO-1') == 'mo', stagenav.zone_kind('DM-MO-1')
    assert stagenav.zone_kind('MN-ST-1') == 'st', stagenav.zone_kind('MN-ST-1')
    assert stagenav.zone_kind('DM-1') == 'base'

    # 分区序号 = 资源库 zoneId 排序（基础 0 / EX 1 / S、MO 2），和界面上
    # 那排图标的左右顺序一致——这是「不用 OCR 也能选对格子」的根据。
    for code, want in (('DM-1', 0), ('DM-EX-1', 1), ('DM-MO-1', 2),
                       ('BB-S-1', 2), ('PV-EX-3', 1), ('GA-EX-1', 1),
                       ('BB-ST-1', 0), ('MT-S-1', 2)):
        got = stagenav.zone_rows_of(code)
        assert got == want, '分区序号错了：%s → %s（应为 %d）' % (code, got, want)

    # 已经标定过的活动：图标数与分区数一致，坐标都在 1280×720 内
    for code, n in (('DM-EX-1', 3), ('BB-S-1', 3), ('MT-S-1', 3), ('PV-EX-1', 3),
                    ('WD-EX-1', 2), ('GA-EX-1', 2), ('HE-EX-1', 2),
                    ('BI-EX-1', 2), ('RS-EX-1', 2), ('GO-EX-1', 2),
                    ('MN-EX-1', 2),
                    # 2026-11-18 第二批（用户逐活动截图标的 EX / S 入口）
                    ('NL-EX-1', 3), ('NL-S-1', 3),
                    ('HS-EX-1', 3), ('HS-S-1', 3), ('HS-MO-1', 3),
                    ('OR-EX-1', 3), ('OR-S-1', 3), ('OR-MO-1', 3),
                    ('CW-EX-1', 2), ('DV-EX-1', 2), ('MB-EX-1', 2),
                    # 2026-11-18 第三批（老故事集剩下这批，用户逐活动截图标的）
                    ('SV-EX-1', 2), ('BP-EX-1', 2), ('EP-EX-1', 2),
                    ('TW-EX-1', 3), ('TW-MO-1', 3), ('LE-EX-1', 2),
                    ('ZT-EX-1', 3), ('ZT-S-1', 3), ('FC-EX-1', 2),
                    ('EA-EX-1', 2), ('OF-EX1', 3), ('OF-F1', 3),
                    ('RI-EX-1', 2), ('DH-EX-1', 3), ('DH-S-1', 3),
                    ('IC-EX-1', 3), ('IC-S-1', 3), ('SL-EX-1', 3),
                    ('SL-MO-1', 3), ('AS-EX-1', 3), ('AS-S-1', 3),
                    ('AT-EX-1', 3), ('AT-S-1', 3), ('CB-EX1', 2),
                    ('CV-EX-1', 2), ('AD-EX-1', 2)):
        pts = stagenav.zone_icons_of(code)
        assert len(pts) == n, '%s 应该标定了 %d 个分区图标，实际 %d' % (code, n, len(pts))
        for x, y in pts:
            assert 0 < x < 1280 and 0 < y < 720, ('分区图标坐标越界', code, x, y)
        # 同一个活动的图标必须左→右有序、且互不重叠（MT 的「谒见神祇」离
        # 「瞻望圣堂」只有 87px，所以门槛按图标宽度的下限定 70）
        for a, b in zip(pts, pts[1:]):
            assert b[0] - a[0] >= 70, ('分区图标间距过小', code, a, b)

    # 分区栏式活动：序号要落在资源库算出来的那一格上（错了就点到隔壁分区）
    for code, want in (('NL-EX-1', 1), ('NL-S-1', 2), ('HS-EX-1', 1),
                       ('HS-MO-1', 2), ('OR-EX-1', 1), ('OR-MO-1', 2),
                       ('CW-EX-1', 1), ('DV-EX-1', 1), ('MB-EX-1', 1),
                       # 第三批
                       ('SV-EX-1', 1), ('BP-EX-1', 1), ('EP-EX-1', 1),
                       ('TW-EX-1', 1), ('TW-MO-1', 2), ('LE-EX-1', 1),
                       ('ZT-EX-1', 1), ('ZT-S-1', 2), ('FC-EX-1', 1),
                       ('EA-EX-1', 1), ('OF-EX1', 2), ('OF-F1', 1),
                       ('RI-EX-1', 1), ('DH-EX-1', 1), ('DH-S-1', 2),
                       ('IC-EX-1', 1), ('IC-S-1', 2), ('SL-EX-1', 1),
                       ('SL-MO-1', 2), ('AS-EX-1', 1), ('AS-S-1', 2),
                       ('AT-EX-1', 1), ('AT-S-1', 2), ('AT-MO-1', 2),
                       ('CB-EX1', 1), ('CV-EX-1', 1), ('AD-EX-1', 1)):
        assert stagenav.zone_rows_of(code) == want, \
            ('分区序号错了：%s → %s' % (code, stagenav.zone_rows_of(code)))

    # OF 是「地区切换」面板：①基础 / ②OF-F / EX，三格都是按钮
    assert stagenav.zone_kind('OF-F1') == 'f', stagenav.zone_kind('OF-F1')
    assert stagenav.zone_kind('OF-EX1') == 'ex'
    # SV 复刻过，资源库里两套基础区都收着，EX 排出来会是第 3 格；界面上只有
    # 两格，靠 zoneRowByCode 硬指。IC-P-1/P-2 同理（自成一区，界面挂在基础区）。
    assert stagenav.zone_rows_of('SV-EX-1') == 1, stagenav.zone_rows_of('SV-EX-1')
    assert stagenav.zone_rows_of('SV-1') == 0
    assert stagenav.zone_rows_of('IC-P-1') == 0, stagenav.zone_rows_of('IC-P-1')
    # 前缀要按整段匹配：'TW-S' 不能把 TW-ST-1 也算进去，'IC-P' 不能认 IC-PX
    assert stagenav._row_override('TW-S-1') is None
    assert stagenav._row_override('IC-P-1') == 0
    assert stagenav._row_override('IC-PX-1') is None

    # 只有「基础区 + 一个 EX 按钮」的活动：不进 zoneIcons，按活动覆盖单点。
    # 这类界面上右边那块是活动名/副标题，不是格子，硬按分区栏下标点会点空。
    for code, pt in (('WR-EX-1', (1073, 605)), ('WB-EX-1', (1005, 628)),
                     ('IW-EX-1', (1175, 620)), ('GT-EX-1', (1171, 605))):
        assert stagenav.zone_icons_of(code) == [], \
            ('%s 不是分区栏，不该进 zoneIcons' % code)
        assert stagenav.ex_point(code) == pt, (code, stagenav.ex_point(code))
        assert stagenav.zone_rows_of(code) == 1, code

    # GT-HX 与 GT-EX 同属 act13d2_zone2（界面上共用一个 EX 图标）：代号归一成 ex
    assert stagenav.zone_kind('GT-HX-1') == 'ex', stagenav.zone_kind('GT-HX-1')
    assert stagenav.zone_rows_of('GT-HX-1') == 1
    assert stagenav.is_extra('GT-HX-1') is True

    # CW 的第 3 格（S 分区）故意不标：那套 UI 不是常规战斗列表，MAA 跑不了，
    # 标了会把它点开。留给 goto_subzone 报「只标了 2 个」。
    assert len(stagenav.zone_icons_of('CW-S-1')) == 2

    # 切入必须走独立函数，而且要在「进入活动」之后
    src = inspect.getsource(stagenav.goto_story_activity)
    assert 'goto_subzone(touch, stage_code' in src, '进地图后要调 goto_subzone 切分区'
    assert hasattr(stagenav, 'goto_subzone') and callable(stagenav.goto_subzone)
    sub = inspect.getsource(stagenav.goto_subzone)
    assert '先归位' in sub or '归位到第 1 个分区' in sub, '要先点第 1 格归位再切目标格'
    assert '_snap_icon' in sub, '点之前要用图像识别校正图标位置'
    # 图像识别本身：真图上要能认出图标列（用 2026-11-18 用户截图标定的坐标自检）
    assert hasattr(stagenav, '_icon_peaks') and hasattr(stagenav, '_snap_icon')

    # 6) 扫描覆盖与步长：整张表（主线 + 各 Story Line 分组 + 未归组新活动）
    #    实测 15 屏、约 12000 像素高（1080 口径）。覆盖要留几倍余量，但
    #    **步长不能贪大**：560 那版会漏屏（玛莉娅·临光正好落在两张截图之间）；
    #    360 仍会截断卡片（截断卡匹配分从 4 涨到 19，掉出阈值），
    #    240（1080 口径 360px）保证每行至少有一屏基本完整（2026-11-17）。
    assert stagenav._MAX_SCREENS * stagenav._SWIPE_STEP >= 10000, \
        ('扫描覆盖要够整个曲谱网格', stagenav._MAX_SCREENS, stagenav._SWIPE_STEP)
    assert 200 <= stagenav._SWIPE_STEP <= 460, \
        ('步长太大漏屏、太小翻不完', stagenav._SWIPE_STEP)
    # 加载竞态必须有兜底：卡片图是网络异步加载的，滚到那屏时可能还是黑色占位
    # （2026-11-17 实测 DH/GA/GO/HE/MT 稳定 999）。2026-11-18 把「预滚一遍」
    # 换成了逐屏「整屏无候选就补拍一帧」，代码里必须还留着这道补拍。
    assert 'wait=0.9' in inspect.getsource(stagenav.goto_story_activity), \
        ('整屏没有候选时要补拍一帧，兜住缩略图加载竞态',)

    # 7) adb 通道自检：地址写错时要能自己换一个，连不上要明确说「连不上」，
    #    而不是让上层一路点空、最后误报「翻了几十屏没找到卡片」。
    assert hasattr(stagenav.Touch, 'ready'), 'Touch 要有 ready() 自检'

    # 8) 活动总表（EVENTS）与分发：乐章收录网格里全部有关卡的活动都在表里。
    #    2026-11-17 打包版踩过：PYZ 里模板目录解析不到 → tpl_path_of 返回空 →
    #    MN-7 被当成当期活动去点区域列表。所以分发必须「模板 or 总表」双闸。
    #    代号全部经真机落图核对（2026-11-17，46 个），易错代号在此钉死。
    assert len(stagenav.EVENTS) >= 45, ('活动总表不该缩水', len(stagenav.EVENTS))
    assert stagenav.event_of('SV-EX-1') == ('SV', '覆潮之下')
    assert stagenav.event_of('DM-2') == ('DM', '生于黑夜')
    assert stagenav.event_of('15-3') == ('', ''), '主线关不该进活动表'
    assert stagenav.event_of('H15-1') == ('', ''), '突袭关不该进活动表'
    assert stagenav._navtpl_dirs(), '模板目录一个都没找到'
    # 真机落图核出的易错代号（2026-11-17）：别再回退成按英文缩写猜的旧表
    assert stagenav.event_of('BP-1') == ('BP', '生路'), '生路=BP（RS 是银心湖列车）'
    assert stagenav.event_of('RS-1') == ('RS', '银心湖列车')
    assert stagenav.event_of('WR-1') == ('WR', '画中人')
    assert stagenav.event_of('WB-2') == ('WB', '登临意')
    assert stagenav.event_of('CV-1') == ('CV', '不义之财')
    assert stagenav.event_of('ZT-3') == ('ZT', '崔林特尔梅之金')
    assert stagenav.event_of('EP-1') == ('EP', '出苍白海')
    assert stagenav.event_of('MT-1') == ('MT', '众生行记')
    assert stagenav.event_of('EA-1') == ('EA', '挽歌燃烧殆尽')
    assert stagenav.event_of('ME-1') == ('ME', '雅赛努斯复仇记')
    assert stagenav.event_of('AW-1') == ('AW', '日暮寻路')
    assert stagenav.event_of('TG-1') == ('TG', '去咧嘴谷')
    assert stagenav.event_of('KR-1')[1] == '熔炉"还魂"记'
    assert stagenav.event_of('OS-1') == ('OS', '雪山降临1101')
    assert stagenav.event_of('UR-1') == ('UR', '未许之地')
    assert stagenav.event_of('AT-1') == ('AT', '墟')
    # 46 张模板都随包带上
    tpldir = stagenav._navtpl_dirs()[0]
    missing = [c for c in stagenav.EVENTS
               if not any(os.path.isfile(os.path.join(d, c + '.png'))
                          for d in stagenav._navtpl_dirs())]
    assert not missing, ('这些活动缺模板图', missing)
    assert len([f for f in os.listdir(tpldir) if f.endswith('.png')]) >= 40


# ---------------------------------------------------------------- B36 打关预检
def check_stage_precheck(tmp):
    """B36：开打前预检（前置关 / 练度 / 连锁 / 卡在哪一关）

    2026-11-18 用户实测：练度不够还硬挂 BB-S-1~4，MAA 跑 8 分钟 0/1，
    第一关就打不过，后面三关根本没解锁。预检必须做到：
      1) 按游戏流程排，目标关的前置关自动补进计划；
      2) 练度不够 / 缺干员的关不排队；
      3) 前面的关打不了，后面的关标「前置未通」（不是「能打」）；
      4) 一关都打不了时，skill_fight 直接报错，不碰设备；
      5) 真开打了要知道卡在哪一关（作业文件名 → 代号）。
    """
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from core import stageplan, skills, maaplan

    # 1) 顺序与分区：代号后缀决定类型，同活动同类型按序号排
    assert stageplan.kind_of('BB-S-3') == 's'
    assert stageplan.kind_of('MN-EX-1') == 'ex'
    assert stageplan.kind_of('OF-EX1') == 'ex', '火蓝之心没有横杠也要认'
    assert stageplan.kind_of('BB-MO-1') == 'mo'
    assert stageplan.kind_of('BB-1') == 'base'
    assert stageplan.chain('BB-S-3') == ['BB-S-1', 'BB-S-2', 'BB-S-3']
    assert stageplan.chain('BB-EX-3') == ['BB-EX-1', 'BB-EX-2', 'BB-EX-3']
    # 主线不展开（一章十几关，用户没这个预期）
    assert stageplan.chain('1-7') == ['1-7']

    # 2) 要打 S3 时 S1、S2 自动补进计划，并标成「前置」而不是用户点名
    plan = stageplan.expand(['BB-S-3'])
    assert [(p['code'], p['prereq']) for p in plan] == [
        ('BB-S-1', True), ('BB-S-2', True), ('BB-S-3', False)], plan
    # 跨分区按游戏顺序：EX 在 S 前面
    codes = [p['code'] for p in stageplan.expand(['BB-S-1', 'BB-EX-2'])]
    assert codes == ['BB-EX-1', 'BB-EX-2', 'BB-S-1'], codes
    # 基础关默认**不**补前置（刷 BB-7 的材料不该顺手排 BB-1~BB-6），
    # 但计划里必须出现「没补前置」的提示；显式 fill_base=True 才补。
    assert [p['code'] for p in stageplan.expand(['BB-7'])] == ['BB-7']
    assert len(stageplan.expand(['BB-7'], fill_base=True)) == 7
    assert any('没补前置' in n for n in stageplan.review(['BB-7'], mode='fight')['notes'])

    # 3) 练度判定 + 前置连锁（不联网，直接喂 lookup 结果）
    orig = stageplan._lookup
    try:
        stageplan._lookup = lambda code, snap, **kw: {
            'state': 'train' if code == 'BB-S-2' else 'ready',
            'reason': '练度不足' if code == 'BB-S-2' else '',
            'notes': ['缺 精二 60'] if code == 'BB-S-2' else [],
            'op': {'copilotId': 1, 'stageCode': code, 'variant': 'normal'},
            'rating': 9, 'title': 'x'}
        rep = stageplan.review(['BB-S-4'], '', use_cache=False)
        states = [(i['code'], i['state']) for i in rep['plan']]
        assert states == [('BB-S-1', 'ready'), ('BB-S-2', 'train'),
                          ('BB-S-3', 'locked'), ('BB-S-4', 'locked')], states
        assert rep['playable'] == ['BB-S-1'], rep['playable']
        assert [b['code'] for b in rep['blocked']] == ['BB-S-2', 'BB-S-3', 'BB-S-4']
        assert 'BB-S-2' in rep['blocked'][0]['reason'] or \
            rep['blocked'][0]['reason'] == '练度不足'
    finally:
        stageplan._lookup = orig

    # 4) 一关都打不了：直接报错，绝不能把空队列交给 MAA
    orig_rev = stageplan.review
    orig_acc = skills._account_file
    try:
        stageplan.review = lambda stages, *a, **k: {
            'plan': [], 'playable': [], 'blocked': [{'code': 'BB-S-1',
                                                     'reason': '练度不足',
                                                     'notes': []}],
            'ops': {}, 'notes': ['练度不够'], 'summary': '能打 0 关'}
        skills._account_file = lambda n: 'x.json'
        try:
            skills.skill_fight({'stages': ['BB-S-1']}, lambda *_: None)
        except ValueError as e:
            assert '一关都打不了' in str(e), e
        else:
            raise AssertionError('一关都打不了还继续排队了')

        # 4.1) 纯刷关（fight）默认不补前置：刷 S-3 的材料不该顺手打 S-1、S-2
        cap = {}

        def fake_review(stages, *a, **k):
            cap.update(k)
            return {'plan': [], 'playable': [], 'blocked': [],
                    'ops': {}, 'notes': [], 'summary': 'x'}
        stageplan.review = fake_review
        try:
            skills.skill_fight({'stages': ['BB-S-3'], 'mode': 'fight',
                                'account': 'x.json'}, lambda *_: None)
        except ValueError:
            pass
        assert cap.get('with_prereq') is False, cap
        cap.clear()
        try:
            skills.skill_fight({'stages': ['BB-S-3'], 'mode': 'copy',
                                'account': 'x.json'}, lambda *_: None)
        except ValueError:
            pass
        assert cap.get('with_prereq') is True, cap
    finally:
        stageplan.review = orig_rev
        skills._account_file = orig_acc

    # 4.2) 没账号快照时不能把「缺干员」当结论：那会把所有关都拦住
    from core import copilot as cp
    orig_search = cp.search
    try:
        cp.search = lambda *a, **k: {
            'mode': 'stage',
            'stages': [{'stage': {'code': 'BB-S-1', 'variant': 'normal'},
                        'ops': [{'copilotId': 1, 'mode': 'blocked',
                                 'missing': ['甲', '乙', '丙'], 'training': [],
                                 'total': 11, 'rating': 9, 'title': 'x'}],
                        'best': {'copilotId': 1, 'mode': 'blocked',
                                 'missing': ['甲', '乙', '丙'], 'training': [],
                                 'total': 11, 'rating': 9, 'title': 'x'}}]}
        rep = stageplan.review(['BB-S-1'], '', use_cache=False)
        assert rep['playable'] == ['BB-S-1'], rep
        assert not rep['blocked'], rep['blocked']
        assert rep['plan'][0]['state'] == 'unknown'
    finally:
        cp.search = orig_search

    # 4.3) 同样能打时优先突袭行：通用位（difficulty=3）作业以前永远落
    #      variant=normal，写进 MAA 一律 IsRaid=false，挂 IW-EX 打的全是
    #      普通难度（2026-11-18 用户实测「只推荐普通难度，说突袭打不了」，
    #      其实作业就叫「IW-EX-1突袭五人」）。可打性仍优先于变体：突袭行
    #      全打不了、普通行能打时，照旧选普通。
    try:
        good = {'copilotId': 9, 'mode': 'ready', 'missing': [], 'training': [],
                'total': 11, 'rating': 9, 'title': 'x', 'difficulty': 3}
        cp.search = lambda *a, **k: {'mode': 'stage', 'stages': [
            {'stage': {'code': 'IW-EX-1', 'variant': 'normal'},
             'ops': [dict(good)], 'best': dict(good)},
            {'stage': {'code': 'IW-EX-1', 'variant': 'raid'},
             'ops': [dict(good)], 'best': dict(good)}]}
        v = stageplan._lookup('IW-EX-1', {'chars': []}, use_cache=False)
        assert (v.get('op') or {}).get('variant') == 'raid', v
        bad = dict(good, copilotId=2, mode='blocked', missing=['甲', '乙'])
        cp.search = lambda *a, **k: {'mode': 'stage', 'stages': [
            {'stage': {'code': 'IW-EX-1', 'variant': 'normal'},
             'ops': [dict(good)], 'best': dict(good)},
            {'stage': {'code': 'IW-EX-1', 'variant': 'raid'},
             'ops': [dict(bad)], 'best': dict(bad)}]}
        v = stageplan._lookup('IW-EX-1', {'chars': []}, use_cache=False)
        assert (v.get('op') or {}).get('variant') == 'normal', v
    finally:
        cp.search = orig_search

    # 5) 卡在哪一关：作业文件名 → 代号（MAA 只给文件路径）
    assert maaplan._stage_of_file({'file_name': r'C:\x\BB-S-3_10531.json'}) == 'BB-S-3'
    assert maaplan._stage_of_file(
        {'file_name': 'BB-S-3_10531.json'},
        {'by_file': {'BB-S-3_10531.json': 'BB-S-3'}}) == 'BB-S-3'
    assert 'failed_stage' in inspect.getsource(maaplan.run_core), \
        'run_core 要回报卡在哪一关'

    # 6) 失败原因要翻译成人话（认不出来就原样返回，不许瞎猜）
    assert '解锁' in stageplan.explain_failure('关卡未解锁')
    assert '练度' in stageplan.explain_failure('未达到三星')
    assert stageplan.explain_failure('') == ''

    # 7) 助手侧：确认卡里必须先写清「哪几关打不了」，一关都打不了就别摆卡
    from core import agent
    orig_rev2 = stageplan.review
    try:
        stageplan.review = lambda *a, **k: {
            'plan': [{'code': 'BB-S-1', 'state': 'train', 'reason': '练度不足',
                      'notes': ['星熊 需 精二 60 级'], 'prereq': False,
                      'kind': 's', 'story': False}],
            'playable': [], 'blocked': [{'code': 'BB-S-1', 'reason': '练度不足',
                                         'notes': []}],
            'ops': {}, 'notes': [], 'summary': '能打 0 关'}
        out = agent._skill_via_guard('fight', {'stages': ['BB-S-1']}, None)
        assert out.get('error') and 'precheck' in out, out
        assert '练度不足' in out['precheck'], out['precheck']
    finally:
        stageplan.review = orig_rev2


# ---------------------------------------------------------------- B37 环境检测
def check_envcheck(_tmp):
    """B37：开跑前环境检测（配置来源 / 静默 / 替用户启动 / 闸门）

    2026-11-18 用户要求：每次跑任务前先看模拟器在不在，路径优先从 MAA 的
    配置里读；检测本身「不显示出来」，只有真没启动、真替他启动了才说话。
    所以这组断言的语气很明确——**正常路径必须一行日志都没有**。
    """
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from unittest import mock
    from core import envcheck, skills, tasks

    cfg = {'adb': 'adb', 'address': 'emulator-5554', 'kind': 'LDPlayer',
           'emu_dir': 'D:\\LDPlayer\\', 'index': 0, 'name': '雷电模拟器',
           'source': 'MAA 配置'}
    empty = {'adb': '', 'address': '', 'kind': '', 'emu_dir': '', 'index': 0,
             'name': '', 'source': ''}

    # 1) 读不到 MAA 配置（还没引导过）：放行，别把任务堵死
    with mock.patch.object(envcheck, 'read_config', lambda: empty):
        logs = []
        ok, why = envcheck.ensure(log=logs.append)
        assert ok and not logs, ('没配置时应静默放行', logs)

    # 2) 已经连上：零日志（「不显示出来」就是这条）
    with mock.patch.multiple(
            envcheck, read_config=lambda: cfg,
            adb_now=lambda cfg=None: (True, 'emulator-5554', 'adb 正常')):
        logs = []
        ok, why = envcheck.ensure(log=logs.append)
        assert ok and why == 'adb 正常', (ok, why)
        assert not logs, ('已连上时不该刷日志', logs)

    # 3) 用户关了自动启动：只提示，绝不发启动命令
    started = []
    with mock.patch.multiple(
            envcheck, read_config=lambda: cfg,
            adb_now=lambda cfg=None: (False, '', '没有在线设备'),
            instance_up=lambda cfg: False,
            launch=lambda c: started.append(c) or (True, 'x')):
        with mock.patch.object(envcheck, 'settings',
                               mock.Mock(load=lambda: {'emulatorAutoStart': False})):
            ok, why = envcheck.ensure(log=lambda *_: None)
        assert not ok and '自动启动' in why, why
        assert not started, '关了自动启动还去点启动器'

    # 4) 没开就替用户启动；起了才放行，日志里要说清「帮你了」
    seq = []

    def fake_adb(cfg=None):
        seq.append(1)
        return (len(seq) > 1, 'emulator-5554', 'adb 正常（emulator-5554）')
    logs = []
    with mock.patch.multiple(
            envcheck, read_config=lambda: cfg, adb_now=fake_adb,
            instance_up=lambda cfg: False,
            launch=lambda c: (True, 'ldconsole.exe launch --index 0')):
        with mock.patch.object(envcheck, 'settings',
                               mock.Mock(load=lambda: {'emulatorAutoStart': True})):
            ok, why = envcheck.ensure(log=logs.append, wait=3)
    assert ok, why
    assert any('帮你启动' in l for l in logs), logs
    assert any('adb 就绪' in l for l in logs), logs

    # 5) 启动器找不到 / 类型不认识：说清原因，别静默往下走
    with mock.patch.multiple(
            envcheck, read_config=lambda: dict(cfg, emu_dir='D:\\nope\\'),
            adb_now=lambda cfg=None: (False, '', '没有在线设备'),
            instance_up=lambda cfg: False):
        with mock.patch.object(envcheck, 'settings',
                               mock.Mock(load=lambda: {'emulatorAutoStart': True})):
            ok, why = envcheck.ensure(log=lambda *_: None)
        assert not ok and '目录不存在' in why, why
    with mock.patch.multiple(
            envcheck, read_config=lambda: dict(cfg, kind='Weird'),
            adb_now=lambda cfg=None: (False, '', '没有在线设备'),
            instance_up=lambda cfg: False):
        with mock.patch.object(envcheck, 'settings',
                               mock.Mock(load=lambda: {'emulatorAutoStart': True})):
            ok, why = envcheck.ensure(log=lambda *_: None)
        assert not ok and '不认识' in why, why

    # 6) 进程在、adb 连不上（模拟器还在开机 / adb 端口被占）：等一小会再给话术
    with mock.patch.multiple(
            envcheck, read_config=lambda: cfg,
            adb_now=lambda cfg=None: (False, 'emulator-5554', '设备 offline'),
            instance_up=lambda cfg: True):
        ok, why = envcheck.ensure(log=lambda *_: None, wait=1)
        assert not ok and 'adb' in why and 'MAA' in why, why

    # 7) 闸门挂对了：除签到（森空岛网页）外都要先查环境
    assert set(skills._NEEDS_GAME) == set(skills.SKILLS) - {'signin'}, \
        skills._NEEDS_GAME
    # /api/skill/list 和助手工具表都按 (desc, fn) 二元组取值，形状不能变
    for name, entry in skills.SKILLS.items():
        assert len(entry) == 2 and callable(entry[1]), name

    # 8) 真挡一次：环境没过，任务直接 error，skill 本体一次都不该被调
    boom = []
    entry = skills.SKILLS['wakeup']
    skills.SKILLS['wakeup'] = (entry[0], lambda *a, **k: boom.append(1))
    try:
        with mock.patch.object(skills.envcheck, 'ensure',
                               lambda log=None: (False, '模拟器没起来')):
            tid = skills.run_skill('wakeup', {})
            t = None
            for _ in range(300):
                t = tasks.get(tid)
                if t and t['status'] != 'running':
                    break
                time.sleep(0.02)
    finally:
        skills.SKILLS['wakeup'] = entry
    assert not boom, '环境没过还是把 skill 本体跑了'
    assert t and t['status'] == 'error', (t or {}).get('status')
    assert '环境没准备好' in (t.get('error') or ''), t.get('error')


def check_settings_panel(tmp):
    """B38：设置页改版后的接口口径（账号 token / 账号级 MAA 绑定）

    2026-11-18 设置从「一列到底」改成左 tab 右内容，账号页开始展示 token、
    MAA 页开始按账号写切号名。这两处都直接改账号文件，口径错一点就是
    「把别的号的凭证写进来」或「把昵称当切号名写进去，MAA 在账号列表里
    死找 4 分钟」，所以单独一组建言。
    """
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from core import accounts, api

    _reset_config(tmp)
    cfg = os.path.join(tmp, 'config')
    path = os.path.join(cfg, 'smoke-acc.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '9', 'nickName': '冒烟', 'channelName': '官服',
                               'game': 'arknights'}}, f, ensure_ascii=False)

    # 1) 没存过 token：要判「没有」，不能跟「坏了」混成一句
    st = accounts.token_state('smoke-acc.json')
    assert st['has'] is False and st['broken'] is False and st['token'] == '', st
    d = api.api_account_token({}, {'file': 'smoke-acc.json'})
    assert d['has'] is False and d['mask'] == '' and d['token'] == '', d

    # 2) 写 token：密文落盘、能读回、掩码只露尾 4 位
    accounts.set_token('smoke-acc.json', 'SMOKE_TOKEN_ABCD')
    st = accounts.token_state('smoke-acc.json')
    assert st['has'] and not st['broken'] and st['token'] == 'SMOKE_TOKEN_ABCD', st
    raw = open(path, encoding='utf-8').read()
    assert 'SMOKE_TOKEN_ABCD' not in raw, 'token 明文落盘'
    d = api.api_account_token({}, {'file': 'smoke-acc.json'})
    assert d['mask'].endswith('ABCD') and 'SMOKE_TOKEN' not in d['mask'], d
    # 换 token 只动 skd 段，账号本体（uid/昵称）与备注必须原样
    snap = accounts.get_account('smoke-acc.json')
    assert snap['account']['uid'] == '9' and snap['account']['nickName'] == '冒烟'
    accounts.update('smoke-acc.json', lambda s2: s2['skd'].__setitem__('alias', '大号'))
    accounts.set_token('smoke-acc.json', 'SMOKE_TOKEN_WXYZ')
    snap = accounts.get_account('smoke-acc.json')
    assert snap['skd']['alias'] == '大号', '换 token 不该把备注冲掉'
    assert accounts.token_state('smoke-acc.json')['token'] == 'SMOKE_TOKEN_WXYZ'

    # 3) 密文解不开：点名「本机解不开」，而不是「没有 token」
    snap['skd'] = {'data': base64.b64encode(b'not-dpapi').decode('ascii')}
    accounts.save_account('smoke-acc.json', snap)
    st = accounts.token_state('smoke-acc.json')
    assert st['has'] and st['broken'] and st['token'] == '', st
    assert '解不开' in st['error'], st['error']

    # 4) 切号名：归一化、清空、官服填昵称要拦、B 服用户名不许动
    m = api.api_account_maa({}, {'file': 'smoke-acc.json',
                                 'patch': {'accountName': '181 5505'}})['maa']
    assert m['accountName'] == '181****5505', m
    m = api.api_account_maa({}, {'file': 'smoke-acc.json',
                                 'patch': {'accountName': ''}})['maa']
    assert m['accountName'] == '', '清空切号名是合法操作，不能拦'
    try:
        api.api_account_maa({}, {'file': 'smoke-acc.json',
                                 'patch': {'accountName': '冒烟'}})
        raise AssertionError('官服填昵称没被拦下')
    except ValueError as e:
        assert '切号名' in str(e), e
    snap = accounts.get_account('smoke-acc.json')
    snap['account']['channelName'] = 'B服'
    accounts.save_account('smoke-acc.json', snap)
    m = api.api_account_maa({}, {'file': 'smoke-acc.json',
                                 'patch': {'accountName': '灰bao#7986'}})['maa']
    assert m['accountName'] == '灰bao#7986', m

    # 5) bootstrap 要把日志目录一起给界面（「数据与日志」页要用）
    boot = api.bootstrap({}, {})
    assert {'root', 'accounts', 'config', 'logs', 'appLog'} <= set(boot['paths']), boot['paths']
    assert boot['paths']['logs'].replace('/', '\\').endswith('debug')


# ---------------------------------------------------------------- B39 培养干员
def check_oper_train(tmp):
    """B39：培养干员（练度导入 / 材料不足暂停 / 续跑 / 战斗日志补全）

    2026-11-18 用户要求「新增培养干员 skill：导入需要培养的练度，材料不足
    会暂停」，同时报「自动战斗没有日志」。这里钉住四件最容易错的：

    ① 专精目标是**绝对等级**（``[0,3,0]`` = 二技能专三），不是「再升三级」；
    ② 已经达标的项要从计划里剔掉，否则 MAA 白跑一趟还报一串「失败」；
    ③ 材料不足 = 暂停而不是失败：剩下的计划要落盘，``resume`` 能接着来；
    ④ 战斗过程（编队 / 部署 / 开技能）必须出日志，重复动作不能刷屏。
    """
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from core import accounts, maalog, maaplan, skills

    _reset_config(tmp)
    cfg = os.path.join(tmp, 'config')
    path = os.path.join(cfg, 'smoke-acc.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'account': {'uid': '1', 'nickName': '冒烟',
                               'channelName': '官服'},
                   'chars': [
                       {'name': '泥岩', 'profession': '重装', 'elite': 1,
                        'skillLevel': 4,
                        'skills': [{'mastery': 0}, {'mastery': 1}, {'mastery': 0}]},
                       {'name': '银灰', 'profession': '近卫', 'elite': 2,
                        'skillLevel': 7,
                        'skills': [{'mastery': 0}, {'mastery': 3}, {'mastery': 3}]},
                   ]}, f, ensure_ascii=False)

    # 1) 专精写法统一吃下（列表 / 字典 / 冒号 / 中文）
    assert skills._norm_mastery([0, 3, 0]) == [0, 3, 0]
    assert skills._norm_mastery({'2': 3}) == [0, 3, 0]
    assert skills._norm_mastery('2:3') == [0, 3, 0]
    assert skills._norm_mastery('技能2专精3') == [0, 3, 0]
    assert skills._norm_mastery('二技能专三') == [0, 3, 0]
    assert skills._norm_mastery(None) is None
    for bad in ('看不懂', [0, 9, 0], {'9': 1}):
        try:
            skills._norm_mastery(bad)
            raise AssertionError('非法专精目标没被拦：%r' % (bad,))
        except ValueError:
            pass

    # 2) 入参三种写法 → 同一条计划；已达标项剔掉；未知干员当场拦
    rows = skills._train_rows({'operator': '泥岩', 'elite': 2,
                               'skillLevel': 7, 'mastery': [0, 3, 0]})
    assert rows == [{'name': '泥岩', 'elite': 2, 'skillLevel': 7,
                     'mastery': [0, 3, 0]}], rows
    logs = []
    plans = skills._train_plans(rows, 'smoke-acc.json', log=logs.append)
    assert plans == [{'name': '泥岩', 'role': 'Tank', 'elite': 2,
                      'skill_level': 7, 'skill_mastery': [0, 3, 0]}], plans

    done_rows = skills._train_rows({'operator': '银灰', 'elite': 2,
                                    'skillLevel': 7, 'mastery': [0, 3, 0]})
    assert skills._train_plans(done_rows, 'smoke-acc.json',
                               log=logs.append) == []
    assert any('不用培养' in x for x in logs), logs
    try:
        skills._train_plans([{'name': '不存在的干员'}], 'smoke-acc.json',
                            log=lambda *_: None)
        raise AssertionError('未知干员没被拦')
    except ValueError as e:
        assert '没有' in str(e), e

    # 3) core_tasks：plans 原样进 OperProgress；noStartUp 时不补唤醒
    seq = maaplan.core_tasks({'tasks': ['operprogress'], 'plans': plans,
                              'noStartUp': True})
    assert seq == [('OperProgress', {'plans': plans})], seq
    seq = maaplan.core_tasks({'tasks': ['operprogress'], 'plans': plans,
                              'accountName': '5505'})
    assert [t for t, _ in seq] == ['StartUp', 'OperProgress'], seq

    # 4) 材料不足 → 暂停（不是失败），剩下计划落盘、resume 读得回来
    calls = []
    st = {'plans': plans, 'done': {}, 'stop': lambda: calls.append('stop')}
    maaplan._on_core_event({'kind': 'extra', 'what': 'OperProgressDetail',
                            'data': {'name': '泥岩', 'result': 'Success',
                                     'elite': 2}}, lambda *_: None, 't', st)
    assert st['done']['泥岩']['elite'] == 2, st['done']
    maaplan._on_core_event({'kind': 'extra', 'what': 'OperProgressDetail',
                            'data': {'name': '泥岩', 'result': 'Failed',
                                     'result_detail': 'ResourceInsufficient'}},
                           lambda *_: None, 't', st)
    assert '材料不足' in st['pause'], st
    assert calls == ['stop'], calls
    left = skills._remain_plans(plans, st['done'])
    # 精英化已经做完 → 只剩技能与专精（绝对等级，不扣减）
    assert left == [{'name': '泥岩', 'role': 'Tank', 'skill_level': 7,
                     'skill_mastery': [0, 3, 0]}], left
    skills._save_train_plan('smoke-acc.json', left)
    got = skills._load_train_plan()
    assert got and got['account'] == 'smoke-acc.json' and got['plans'] == left, got
    skills._save_train_plan('smoke-acc.json', [])
    assert skills._load_train_plan() is None

    # 4b) 缺芯片 = 「该去补芯片」，不是崩。MaaCore 6.20.1 在 E0→E1 缺普通
    #     芯片时直接返回 ChipNotCraftable、不去加工站合成（上游 issue #18612，
    #     MAA 自己的界面跑也一样）。2026-11-19 用户看到的「卡在升级界面报错」
    #     就是这条 + 下面 start 的误判。日志要写清「缺什么、怎么补」，
    #     精英化没成之后的连环 PrerequisiteNotMet 不许刷屏。
    calls2 = []
    st3 = {'plans': plans, 'done': {}, 'stop': lambda: calls2.append('stop')}
    out3 = []
    maaplan._on_core_event({'kind': 'extra', 'what': 'OperProgressDetail',
                            'data': {'name': '泥岩', 'result': 'Failed',
                                     'result_detail': 'ChipNotCraftable'}},
                           out3.append, 't', st3)
    assert '芯片' in st3['pause'], st3
    assert any('加工站' in x for x in out3), out3
    assert calls2 == ['stop'], calls2
    out4 = []
    maaplan._on_core_event({'kind': 'extra', 'what': 'OperProgressDetail',
                            'data': {'name': '泥岩', 'result': 'Failed',
                                     'result_detail': 'PrerequisiteNotMet'}},
                           out4.append, 't', st3)
    assert out4 == [], out4

    # 4c) 暂停后 _wait_run 不许「重排一次」把刚停下的任务又拉起来
    class _StubEng:
        starts = 0

        def wait(self, timeout=None, poll=1.0):
            return False

        def running(self):
            return False

        def start(self, settle=15.0):
            type(self).starts += 1
            return True

    assert maaplan._wait_run(_StubEng(), 5, lambda *_: None,
                             {'pause': '泥岩 缺精英化芯片'}) is False
    assert _StubEng.starts == 0, '暂停还被重排了'

    # 5) MAA 日志桥：只翻有信息量的行，噪音（识别框为空 / 缺 pipeline）全丢
    tap = maalog.Tap(None)
    msg = tap.translate('[2026-11-18 14:41:00.533][ERR][Px1][Tx2] '
                        'asst::MultiCopilotTaskPlugin::confirm_stage_name '
                        'confirm stage name failed after retrying 3 times, '
                        'stage name: MN-EX-6')
    assert msg and 'MN-EX-6' in msg[1], msg
    assert tap.translate('[2026-11-18 14:41:00.533][WRN][Px1][Tx2] '
                         'asst::VisionHelper::correct_rect roi is empty') is None
    assert tap.translate('[2026-11-18 14:41:00.533][ERR][Px1][Tx2] '
                         'Unknown task: FightSeries-OldMethodFlag') is None
    assert tap.translate('[2026-11-18 14:41:00.533][TRC][Px1][Tx2] x') is None

    # 6) 战斗过程要有日志：编队 / 部署 / 开技能各一条，重复的部署不刷屏
    out = []
    st2 = {'actions': {}, 'deployed': []}
    for ev in ({'kind': 'extra', 'what': 'BattleFormation',
                'data': {'formation': ['银灰', '泥岩']}},
               {'kind': 'extra', 'what': 'CopilotAction',
                'data': {'action': 'Deploy', 'target': '银灰'}},
               {'kind': 'extra', 'what': 'CopilotAction',
                'data': {'action': 'Deploy', 'target': '银灰'}},
               {'kind': 'extra', 'what': 'CopilotAction',
                'data': {'action': 'UseSkill'}}):
        maaplan._on_core_event(ev, out.append, 't', st2)
    assert out == ['编队：银灰、泥岩', '部署 银灰', '开技能'], out
    # Output / SkillUsage 这类内部记账不该把英文原文写进面板
    out = []
    maaplan._on_core_event({'kind': 'extra', 'what': 'CopilotAction',
                            'data': {'action': 'Output'}},
                           out.append, 't', {'actions': {}})
    assert out == [], out

    # 7) SubTaskStart 的 pipeline 名必须跟真实 asst.log 对得上。
    #    2026-11-18 踩过：映射表的键是照源码猜的（Copilot@BattleStartPreFlag），
    #    而 asst.log 里 first[0] 实际是 BattleStartAll / BattleQuickFormation，
    #    结果战斗一整段没日志。这里把「真实 JSON → maacore._translate →
    #    中文进度」整条链走一遍，键再错位会直接红。
    from core import maacore
    eng = object.__new__(maacore.Engine)   # 不 open，只借 _translate（不碰 DLL）
    samples = [
        ({'first': ['Copilot@WaitUntilEndOfAction'], 'taskchain': 'Copilot'}, '战斗中'),
        ({'first': ['Copilot@FullStageNavigation'], 'taskchain': 'Copilot'}, '找关卡'),
        ({'first': ['Copilot@StageNavigationSlowlySwipeLeft'],
          'taskchain': 'Copilot'}, '滑动找关卡'),
        ({'first': ['BattleStartAll'], 'taskchain': 'Copilot'}, '开始行动'),
        ({'first': ['BattleQuickFormation'], 'taskchain': 'Copilot'}, '快速编队'),
        ({'first': ['UseMedicine'], 'taskchain': 'Fight'}, '吃理智药'),
        ({'first': ['OperProgress@EliteUp'], 'taskchain': 'OperProgress'}, '精英化'),
    ]
    for d, want in samples:
        ev = eng._translate(maacore.MSG_SUBTASK_START, d)
        assert ev and ev.get('kind') == 'node', (d, ev)
        out = []
        maaplan._on_core_event(ev, out.append, 't', {'nodes': {}})
        assert out == ['· ' + want], (d, out)
    # 插件类名节点（不是用户能看懂的阶段）静默
    ev = eng._translate(maacore.MSG_SUBTASK_START,
                        {'first': ['BattleProcessTask'], 'taskchain': 'Copilot'})
    out = []
    maaplan._on_core_event(ev, out.append, 't', {'nodes': {}})
    assert out == [], out
    # 按职业筛选的十几种后缀统一归一类
    assert (maaplan._subtask_label('Copilot@BattleQuickFormationRole-Caster')
            == '编队·按职业筛选')

    # 8) 切号名唯一来源 = 账号绑定的 maa.accountName；**绝不读用户的 MAA
    #    配置档案**（2026-11-18 用户口径：agent 模式跑临时配置=StartUp 任务
    #    参数，不碰用户的 gui.new.json）。以前这里会退回读 MAA 当前档案的
    #    AccountName / 纯数字档案名来猜——档案和账号对不上号，猜错切号名
    #    MAA 会在账号列表里死找 4 分钟。昵称也绝不能顶——官服切号认手机号。
    from core import maa as maa_mod

    def _forbidden(*a, **k):
        raise AssertionError('不许读用户的 MAA 配置档案')

    orig_load = maa_mod._load_cfg
    maa_mod._load_cfg = _forbidden
    try:
        assert skills._account_name_tail('smoke-acc.json') == '', \
            '没绑定切号名 → 空串（且全程没读 MAA 配置）'
        accounts.save_maa_meta('smoke-acc.json', {'accountName': '5505'})
        assert skills._account_name_tail('smoke-acc.json') == '5505', \
            '账号绑定的切号名要能读到'
    finally:
        maa_mod._load_cfg = orig_load
        accounts.save_maa_meta('smoke-acc.json', {'accountName': ''})

    # 9) Engine.wait 的启动宽限：AsstStart 异步生效，刚 start 完 AsstRunning
    #    还是 False——以前这里直接 return，把「还没开始跑」当「跑完」，
    #    run_core 收尾 close，培养任务排进队列的同一毫秒就被析构（17:02
    #    用户实测：OperProgress 一步没跑，任务报错）。
    from types import SimpleNamespace
    from core import maacore
    eng = object.__new__(maacore.Engine)
    eng._done = threading.Event()
    eng._handle = 1
    calls = {'n': 0}

    def _running():
        calls['n'] += 1
        if calls['n'] > 4:             # running 生效后任务正常跑完
            eng._done.set()
            return True
        return False                   # 前 4 次 False（AsstStart 还没生效）

    eng._lib = SimpleNamespace(AsstRunning=lambda h: _running())
    assert eng.wait(timeout=10, poll=0.01) is True, '启动宽限内等到 running'
    eng2 = object.__new__(maacore.Engine)
    eng2._done = threading.Event()
    eng2._handle = 1
    flip = {'n': 0}

    def _flip():
        flip['n'] += 1
        return flip['n'] <= 3          # 先跑起来，然后被停

    eng2._lib = SimpleNamespace(AsstRunning=lambda h: _flip())
    assert eng2.wait(timeout=10, poll=0.01) is False, \
        '跑过之后 running 变 False → 停（_done 未 set）'

    # 10) Engine.start 的收尾重试：上一次任务刚跑完时 AsstStart 会返回 false
    #     （AllTasksCompleted 早于工作线程收尾，AsstRunning 仍为 true）——
    #     run_core 第二段「唤醒完了再排培养」正好撞这个窗口，用户 2026-11-18
    #     看到的就是 StartUp 完成 → 排队培养 → AsstStart 返回失败。
    eng3 = object.__new__(maacore.Engine)
    eng3._done = threading.Event()
    eng3._handle = 1
    eng3._chain_starts = 0
    st = {'start_calls': 0, 'running': 3}

    def _asst_start(h):
        st['start_calls'] += 1
        return st['running'] <= 0        # 还没退干净就拒

    def _asst_running(h):
        if st['running'] > 0:
            st['running'] -= 1
            return True
        return False

    eng3._lib = SimpleNamespace(AsstStart=_asst_start, AsstRunning=_asst_running)
    assert eng3.start(settle=5) is True, '退干净后要能重试成功'
    assert st['start_calls'] == 2, st

    # AsstStart 被拒、但实例一直在跑：这是「上一批还在收尾、队列继续被消费」，
    # **不是**失败。2026-11-19 用户报「培养卡在升级界面报错」，三次全是这条：
    # asst.log 里 00:25:09.674 Start | block（返回 false）→ 00:25:09.776
    # TaskChainStart（任务照跑），我们在 15 秒后把它判成失败抛错。
    eng4 = object.__new__(maacore.Engine)
    eng4._done = threading.Event()
    eng4._handle = 1
    eng4._chain_starts = 0
    eng4._lib = SimpleNamespace(AsstStart=lambda h: False,
                                AsstRunning=lambda h: True)
    assert eng4.start(settle=0.2) is True, \
        '一直 running = 队列还在被消费，不许判成「AsstStart 返回失败」'

    # 更准的判据：新任务链开始（_chain_starts 增加）就立刻返回，不干等 settle
    eng5 = object.__new__(maacore.Engine)
    eng5._done = threading.Event()
    eng5._handle = 1
    eng5._chain_starts = 0

    def _deny_then_chain(h):
        threading.Timer(0.1, lambda: setattr(eng5, '_chain_starts', 1)).start()
        return False

    eng5._lib = SimpleNamespace(AsstStart=_deny_then_chain,
                                AsstRunning=lambda h: True)
    t0 = time.time()
    assert eng5.start(settle=5) is True
    assert time.time() - t0 < 2, '看到新链开始就该立刻返回'

    # 11) 账号列表要带出已绑定的切号名：用户设置里配过尾号，助手还追问过一次
    #     （2026-11-18）——列表里有值，模型才不会重复问
    from core import agent as agent_mod
    lc = agent_mod._tool_list_accounts({})
    row = next((a for a in lc['accounts'] if a['file'] == 'smoke-acc.json'), None)
    assert row is not None, lc
    assert 'accountName' in row, '账号列表少了 accountName 字段'
    assert 'accountName' in (lc.get('hint') or ''), 'hint 没提示已有尾号就别问'


def check_config_globals(tmp):
    """B40：config/ 里的全局 json 不许把账号列表炸掉

    2026-11-19 01:24 用户实测「突然无法自动战斗」：培养暂停时 skills 把
    ``train-plan.json`` 落进 config/（里面 ``account`` 是**文件名字符串**），
    ``accounts.list_accounts`` 的 ``(snap.get('account') or {}).get('uid')``
    正好撞上字符串 → 整个账号列表抛 AttributeError → 唤醒 / 战斗 / 培养
    全部报「'str' object has no attribute 'get'」。

    这里把「我们自己的全局文件」和「畸形 account 段」都摆进目录，断言只有
    真账号被列出、且谁都不抛。
    """
    _reset_config(tmp)
    from core import accounts, skills, updater
    cfg = os.path.join(tmp, 'config')

    def dump(name, obj):
        with open(os.path.join(cfg, name), 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)

    dump('good.json', {'account': {'uid': '42', 'nickName': '冒烟',
                                   'channelName': '官服'},
                       'maa': {'accountName': '181****5505'},
                       'stats': {'charCount': 3}})
    # 真正踩雷的那个：account 是文件名字符串
    dump('train-plan.json', {'account': 'good.json', 'savedAt': '',
                             'plans': [{'name': '山'}]})
    dump('str-account.json', {'account': 'oops'})
    dump('list-account.json', {'account': ['oops']})
    dump('bad-segs.json', {'account': {'uid': '43'},
                           'stats': 'oops', 'maa': 'oops', 'skd': 'oops'})
    dump('arr.json', [1, 2, 3])

    assert 'train-plan.json' in updater.NON_ACCOUNT_JSON, \
        '培养计划文件必须登记成全局文件，不是账号'

    rows = accounts.list_accounts()
    files = sorted(r['file'] for r in rows)
    assert files == ['bad-segs.json', 'good.json'], files

    bad = next(r for r in rows if r['file'] == 'bad-segs.json')
    assert bad['stats']['charCount'] is None, 'stats 是字符串时应当当空'
    assert bad['maaAccountName'] == '', 'maa 是字符串时应当当空'
    assert bad['hasToken'] is False

    good = next(r for r in rows if r['file'] == 'good.json')
    assert good['maaAccountName'] == '181****5505'

    # 每个 skill 开跑第一句都是 _account_file('')
    assert skills._account_file('') == 'good.json'
    assert skills._account_file('冒烟') == 'good.json'

    # 同步侧扫描 / 同名改名也不能被这些文件带崩
    assert sorted(updater.scan_accounts(cfg)) == ['42', '43']


def check_raid_order(tmp):
    """B41：每关只打真正要打的那一档（默认突袭、可逐关指定）

    用户 2026-11-19 03:13 实测反馈：「说好只排突袭，结果又从头挂了一遍普通
    EX-1」。asst.log 01:42 证明 MaaCore 拿到 is_raid=true 会自己点
    ChangeToRaidDifficulty 切难度，不需要前面垫一条普通；上一版排
    「普通 → 突袭」两条，用户看到的就是白打一次普通。

    这里钉六件事：
    ① 默认 raid='auto'：有突袭作业的关**只排突袭**一条；
    ② raid='both'：普通 → 突袭两条，顺序不能反；
    ③ raid=False / 'normal'：只排普通；
    ④ stages 支持 @raid / @both / @normal 逐关覆盖（agent 承诺的
      「EX-1~5 只打突袭、EX-6/7 普通+突袭」要能落地）；
    ⑤ prepare_copilot 不再自动给突袭垫普通；
    ⑥ 主线险地（#s）仍是单条 is_raid=true。
    """
    _reset_config(tmp)
    from core import copilot, maaplan, skills

    stage_name = {}

    def fake_search(st, snap, pages=1, limit=6):
        st = st.upper()
        base = sum(ord(c) for c in st) * 100
        stage_name[base] = st
        stage_name[base + 1] = st
        return {'stages': [
            {'stage': {'code': st, 'variant': 'normal'},
             'ops': [{'copilotId': base, 'mode': 'ready', 'rating': 9,
                      'title': '普通作业', 'difficulty': copilot.DIFF_NORMAL}]},
            {'stage': {'code': st, 'variant': 'raid'},
             'ops': [{'copilotId': base + 1, 'mode': 'ready', 'rating': 8,
                      'title': '突袭作业', 'difficulty': copilot.DIFF_RAID}]},
        ]}

    def fake_fetch(cid):
        cid = int(cid)
        if cid == 301:      # 通用位作业：既能打普通也能打突袭
            return ({'stage_name': 'SV-EX-3'},
                    copilot.DIFF_NORMAL | copilot.DIFF_RAID, 'SV-EX-3', [], True)
        raid = cid % 100 == 1
        flags = copilot.DIFF_RAID if raid else copilot.DIFF_NORMAL
        return ({'stage_name': stage_name.get(cid, 'X')}, flags,
                stage_name.get(cid, 'X'), [], True)

    old = (copilot.search, copilot.fetch_prepared, skills._default_maa_dir)
    copilot.search, copilot.fetch_prepared = fake_search, fake_fetch
    skills._default_maa_dir = lambda: tmp
    try:
        logs = []
        rows = skills._prepare_ops(['SV-EX-1', 'SV-EX-2'], '', logs.append)
        seq = [(r['stage_name'], r['is_raid']) for r in rows]
        # ① 默认只打突袭——不再先刷一遍普通
        assert seq == [('SV-EX-1', True), ('SV-EX-2', True)], seq

        # ② both：普通 → 突袭两条，顺序不能反
        rows_b = skills._prepare_ops(['SV-EX-1'], '', logs.append, raid='both')
        seq_b = [(r['stage_name'], r['is_raid']) for r in rows_b]
        assert seq_b == [('SV-EX-1', False), ('SV-EX-1', True)], seq_b

        # ③ raid=False / 'normal'：只排普通
        rows2 = skills._prepare_ops(['SV-EX-1'], '', logs.append, raid=False)
        assert [(r['stage_name'], r['is_raid']) for r in rows2] == [('SV-EX-1', False)]
        rows2b = skills._prepare_ops(['SV-EX-1'], '', logs.append, raid='normal')
        assert [(r['stage_name'], r['is_raid']) for r in rows2b] == [('SV-EX-1', False)]

        # ④ 逐关覆盖：EX-1 只突袭、EX-2 普通+突袭（agent 承诺的混合计划）
        want = {'SV-EX-1': 'raid', 'SV-EX-2': 'both'}
        rows_w = skills._prepare_ops(['SV-EX-1', 'SV-EX-2'], '', logs.append,
                                     raid='auto', wants=want)
        seq_w = [(r['stage_name'], r['is_raid']) for r in rows_w]
        assert seq_w == [('SV-EX-1', True), ('SV-EX-2', False),
                         ('SV-EX-2', True)], seq_w

        # 解析 @ 后缀与 dict 写法
        codes, wants = skills._parse_stages(
            ['SV-EX-1@raid', 'SV-EX-2@both', 'SV-EX-3@normal',
             {'stage': 'SV-EX-4', 'raid': True}, 'SV-EX-5'])
        assert codes == ['SV-EX-1', 'SV-EX-2', 'SV-EX-3', 'SV-EX-4', 'SV-EX-5'], codes
        assert wants == {'SV-EX-1': 'raid', 'SV-EX-2': 'both',
                         'SV-EX-3': 'normal', 'SV-EX-4': 'both'}, wants

        # 某一关没作业 / 搜挂了：跳过它继续，别拖垮整批（预检标 unknown
        # 的关卡、作业站抽风时靠这条，否则一发请求失败整次挂机都不跑）
        logs_skip = []

        def fake_search_mixed(st, snap, pages=1, limit=6):
            if st.upper() == 'XX-1':
                return {'stages': []}
            return fake_search(st, snap, pages=pages, limit=limit)

        copilot.search = fake_search_mixed
        rows_skip = skills._prepare_ops(['SV-EX-1', 'XX-1'], '', logs_skip.append)
        assert [r['stage_name'] for r in rows_skip] == ['SV-EX-1'], rows_skip
        assert any('跳过 XX-1' in x for x in logs_skip), logs_skip
        copilot.search = fake_search

        # ⑤ 突袭条目不再自动垫一条普通（上一版就是它让用户白打一遍普通）
        logs3 = []
        rows3 = maaplan.prepare_copilot(
            [{'copilotId': 301, 'stageCode': 'SV-EX-3', 'variant': 'raid'}],
            tmp, log=logs3.append)
        assert [(r['stage_name'], r['is_raid']) for r in rows3] == \
            [('SV-EX-3', True)], rows3
        assert not any('补一条普通' in x for x in logs3), logs3

        # 主线险地（#s）不是突袭：不补普通条目
        logs5 = []
        copilot.fetch_prepared = lambda cid: ({'stage_name': 'main_15-3#s'},
                                              copilot.DIFF_NORMAL | copilot.DIFF_RAID,
                                              'main_15-3#s', [], True)
        rows5 = maaplan.prepare_copilot(
            [{'copilotId': 501, 'stageCode': '15-3', 'variant': 'raid'}],
            tmp, log=logs5.append)
        assert [(r['stage_name'], r['is_raid']) for r in rows5] == [('15-3', True)], rows5
    finally:
        copilot.search, copilot.fetch_prepared, skills._default_maa_dir = old


# ---------------------------------------------------------------- 入口


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    _isolate_data_root()   # 必须先于一切 core.* 导入（A 组后面 B/C 都会 import）
    print('MAA-PRTS 冒烟测试  %s' % REPO)
    print('-' * 60)
    r = Report()

    r.check('A1 .bat 编码（UTF-8 无 BOM + CRLF）', check_bat_encoding)
    r.check('A2 全部 .py 可编译', check_py_compile)

    tmp = tempfile.mkdtemp(prefix='maaprts-smoke-unit-')
    try:
        settings_mod = _patch_settings(tmp)
        r.check('B1 DPAPI 加解密往返', check_vault_roundtrip)
        r.check('B2 DPAPI 篡改必报错', check_vault_tamper)
        r.check('B3 tokens 随账号 skd 段（去重/备注/不外泄）', lambda: check_tokens_in_accounts(tmp))
        r.check('B4 旧 tokens.txt 自动迁移', lambda: check_tokens_migration(tmp))
        r.check('B5 坏 skd 逐条跳过并点名', lambda: check_tokens_bad_skd(tmp))
        r.check('B6 畸形旧 txt 留证并警告', lambda: check_tokens_bad_txt(tmp))
        r.check('B7 作业 json 修正后 MaaCore 可解析', check_copilot_normalize)
        r.check('B8 难度位标志（普通/突袭/通用）', check_copilot_difficulty)
        r.check('B9 突袭关拆普通/突袭两行', check_copilot_variants)
        r.check('B10 悖论清单与解锁推算', check_paradox_plan)
        r.check('B11 悖论写 MAA 的命名', check_maa_paradox_name)
        r.check('B12 v1 作业默认练度（压级号不再误推）', check_copilot_v1_default_req)
        r.check('B13 旧布局 config/skland 压平迁移', lambda: check_layout_migration(settings_mod, tmp))
        r.check('B14 MAA 干员 JSON 解析与练度映射', lambda: check_operbox_parse(tmp))
        r.check('B15 从 MAA 直接建号（无 token）', lambda: check_create_from_maa(tmp))
        r.check('B16 账号手动排序（accountOrder）', lambda: check_account_order(tmp))
        r.check('B17 MAA 导入账号只比等级', check_maa_import_level_only)
        r.check('B18 剧情关不进搜索', check_story_stage_filter)
        r.check('B19 主线难度变体（简单/标准/磨难/险地）', check_mainline_difficulty_variants)
        r.check('B20 资源包生成幂等（版本/指纹）', check_pack_idempotent)
        r.check('B21 资源更新校验（sha256/条数）', check_pack_verify)
        r.check('B22 程序自更新（版本比较/安装包/替换边界）', check_app_update)
        r.check('B23 作业黑名单（maa:// 代码归一化 + 出口过滤）', lambda: check_op_blacklist(tmp))
        r.check('B24 抽卡统计（星级归一/保底/池合并/终末地翻页/武器口径/跨游戏隔离）', lambda: check_gacha_stats(tmp))
        r.check('B25 森空岛签到（两套口径/已签到判定/403/跳过/去重）', lambda: check_signin(tmp))
        r.check('B26 skland HTTP 错误响应体进任务日志（401 鉴别信息）', lambda: check_skland_http_body(tmp))
        r.check('B27 app.log 失败现场（森空岛响应体全量 + 任务失败摘要/traceback）', lambda: check_applog_capture(tmp))
        r.check('B28 推荐排序分档 + 详情缺练度/缺人提示', check_copilot_rank_band)
        r.check('B29 剧情关双视图 + 森空岛进度解析', lambda: check_story_stages(tmp))
        r.check('B30 日志观测（增量/轮转/停滞判定/运行记录）', lambda: check_asstlog(tmp))
        r.check('B31 多账号编排（切号名/一账号一档案/作业列表）', lambda: check_multiacct(tmp))
        r.check('B32 助手工具白名单 + 写操作闸门 + CLI 契约', lambda: check_agent_cli(tmp))
        r.check('B33 任务编排（GUI 队列剔除 Copilot/直连序列/档案隔离）', lambda: check_maaplan(tmp))
        r.check('B34 活动关导航（区域行号/页面判别/主线关不点）', lambda: check_stagenav(tmp))
        r.check('B35 老故事集导航（曲谱入口/卡片模板/EX 切换）', lambda: check_story_nav(tmp))
        r.check('B36 打关预检（前置关/练度/连锁/卡在哪关）', lambda: check_stage_precheck(tmp))
        r.check('B37 环境检测（配置来源/静默/替用户启动/任务前闸门）', lambda: check_envcheck(tmp))
        r.check('B38 设置页接口（token 读写/掩码/切号名归一）', lambda: check_settings_panel(tmp))
        r.check('B39 培养干员（练度导入/材料暂停/续跑/战斗日志）', lambda: check_oper_train(tmp))
        r.check('B40 全局 json 不炸账号列表（train-plan/畸形 account 段）', lambda: check_config_globals(tmp))
        r.check('B41 每关只打要打的档（默认突袭/@后缀逐关/不再垫普通）', lambda: check_raid_order(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    r.check('C1 启动 + 只读接口 + 目录零污染', check_boot_smoke)

    print('-' * 60)
    if r.fails:
        print('结果：FAIL（%d 项未通过）' % r.fails)
        return 1
    print('结果：全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
