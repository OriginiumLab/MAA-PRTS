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
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
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
    assert copilot.entry_variants(2, 'normal') == [True], '普通行里没有普通作业的兜底'
    assert copilot.entry_variants(1, 'raid') == [False]

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

    # 4. 搜剧情关代号拿不到结果；战斗关照常能搜到
    for kw in ('NL-ST-1', 'EG-3'):
        r = resources.resolve(kw)
        assert not r['stages'], '搜 %s 不该有结果，实际 %d 关' % (kw, len(r['stages']))
    r = resources.resolve('NL-10')
    assert r['mode'] == 'stage' and len(r['stages']) == 1, \
        'NL-10 是战斗关，必须还能搜到：%r' % r['mode']

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

    # 系列/活动视图不摆「简单」行，但磨难/险地保留
    r = resources.resolve('12-1')
    assert r['mode'] == 'activity'
    assert not [s for s in r['stages'] if s['difficulty'] == 'easy'], '系列视图不该有简单行'
    assert any(s['difficulty'] == 'tough' for s in r['stages'])

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

    # 行排序：同代号内 磨难 → 标准（→ 突袭/险地）→ 简单；代号间按主关 id
    def pack(sid, diff, variant=None):
        return {'stage': {'stageId': sid, 'difficulty': diff, 'variant': variant}}
    keys = [copilot.row_sort_key(pack(*k)) for k in [
        ('easy_12-06', 'easy', 'normal'), ('tough_12-06', 'tough', 'normal'),
        ('main_12-06', 'normal', 'normal'), ('main_12-06', 'normal', 'raid'),
        ('main_15-02#s', 'risky', 'raid'), ('main_15-02', 'normal', 'normal'),
        ('act13side_ex01', 'normal', 'normal')]]
    assert keys[1] < keys[2] < keys[3] < keys[0], '磨难→标准→突袭→简单 的顺序不对：%r' % keys
    assert keys[4] < keys[5], '险地是 15+ 章的主难度，应排在普通行前面'
    assert keys[6][0] == 'act13side_ex01', '活动关的归并 id 不该被改写'

    # 下一关：同系列同难度推进；剧情关空缺跳过；章末返回 None
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

    # TR 教学关默认不参与推进：TL-10（活动主线末关）后面是 TL-TR-1 也不能给，
    # 序列末尾就是 None；未登记的 TR 自身也推不出下一关
    assert nxt('act55side_10', 'normal') is None, \
        'TL-10 的下一关不该是 TL-TR-1（TR 位置未知，默认全跳过）'
    assert nxt('act13side_tr01', 'normal') is None, '未登记位置的 TR 自身不该有下一关'
    assert nxt('tr_01', 'normal') is None
    assert nxt('lt_tr_01', 'normal') is None, '塔的 TR（lt_tr_01）也不该有下一关'
    # TR 判定覆盖全部 id 形态（tr_01 / _tr01 / -tr01 / lt_tr_01）
    for sid in ('tr_01', 'lt_tr_01', 'act55side_tr01', 'act2vmulti-tr01', 'act18side_tr_02'):
        assert resources._is_tr(sid), '%s 应判为 TR' % sid
    for sid in ('act55side_10', 'main_12-06', 'tough_12-06', 'main_15-02#s', 'hard_15-01'):
        assert not resources._is_tr(sid), '%s 不该判为 TR' % sid
    # TR_AFTER 登记过的 TR 插回锚点后面参与推进（人工维护位置的扩展点）
    resources.TR_AFTER['act55side_tr01'] = 'act55side_05'
    try:
        got = nxt('act55side_05', 'normal')
        assert got and got['stageId'] == 'act55side_tr01', \
            '登记后 TL-TR-1 应插在 TL-5 后面，实际 %r' % got
        got = nxt('act55side_tr01', 'normal')
        assert got and got['code'] == 'TL-6', '登记后的 TR 应能推进到锚点下一关'
    finally:
        resources.TR_AFTER.pop('act55side_tr01', None)


# ---------------------------------------------------------------- B23 作业黑名单
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
