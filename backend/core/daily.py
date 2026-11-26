# -*- coding: utf-8 -*-
"""多账号「一键长草」：读每号自己配的 MAA 档案，用 MAA 的理智作战挂

为什么不自己导航（2026-11-26 用户报障）
--------------------------------------
老的日常是「我们的 stagenav 导航 + 作业站 Copilot」打的。问题是 MAA GUI 这时
也在跑自己的日常队列：两条腿同时戳同一台模拟器，截图/点击互相抢，最后 MAA 报错
退出。而且我们只认作业站，用户档案里明明配好了「理智作战刷 OS-8」，我们却从头
再问一遍、再导航一遍——费时且容易错。

现在这条路的形状（用户原话）：

1. 每个号去读**他自己配的那份 MAA 档案**：一键长草里勾了哪些任务、理智作战
   正在刷哪一关。关卡报给用户确认，确认完直接开跑。
2. 拷贝的只是「一键长草这一套」——任务开关与参数（公招几次、基建无人机用途、
   信用商店黑名单…），模拟器地址、切号名、结束脚本、主题这些一概不带。
3. 没给账号分配档案（都用默认）就用白本 ``resources/daily-plan.json``
   （从 181 抄的一份，地址/切号名/关卡留空），关卡问用户，agent 填进去。
4. 单号直接跑；多号同模拟器轮替（一个跑完切下一个）；不同模拟器并行（各起
   一个独立进程，见 ``maaplan.run_plan`` 的分组）。
5. **切号交给 MAA**：``StartUp.account_name`` 一给，MAA 自己退出登录 → 账号
   管理器 → 选中那个号。我们不再按返回键、不再点导航。
6. 理智作战由 MAA 的 ``Fight`` 任务自己找关卡。**我们一次 adb 点击都不发**。

所以这里的产出只是「每个号一条 MaaCore 任务链」，真正执行复用
``maaplan.run_plan``（它已经按「MAA 目录 + 模拟器地址」分好组了）。
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import re

from . import maa, maaplan, multiacct, resources, settings, tasks

#: 白本模板（跟着 ``resources`` 一起进包；读不到就退回 maaplan 的内置默认值）
DEFAULT_PATH = os.path.join(settings.RES, 'resources', 'daily-plan.json')

#: 「一键长草」的任务顺序。MAA GUI 那个一键长草按钮加的就是这一套
ORDER = ('startup', 'recruit', 'infrast', 'fight', 'mall', 'award')

#: TaskType → 我们的 key（TASK_MAP 是反着的）
KEY_OF = {v['type']: k for k, v in maaplan.TASK_MAP.items()}

#: 不进日常队列的任务类型：
#: - Copilot 根本进不了 MaaCore 的任务队列（见 maaplan 顶部）
#: - OperProgress / UserDataUpdate 不是「长草」，是别的入口在管
SKIP_TYPES = frozenset(('Copilot', 'OperProgress', 'UserDataUpdate'))


# ---------------------------------------------------------------- 小工具
def _split_list(v):
    """MAA 的信用商店配置是 ``碳;家具;加急许可`` 这种分号串，也容忍数组"""
    if isinstance(v, (list, tuple)):
        items = list(v)
    else:
        items = re.split(r'[;；,，]', str(v or ''))
    return [str(x).strip() for x in items if str(x).strip()]


def _stars(*flags):
    """四个勾选 → MaaCore 的星级数组（``select`` / ``confirm`` 共用一套口径）"""
    return [lv for lv, f in zip((3, 4, 5, 6), flags) if f]


def _pct(v, default=0.3):
    """``DormThreshold`` 是百分数（30），MaaCore 的 ``threshold`` 是 0~1"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return round(n / 100.0, 2) if n > 1 else n


# ---------------------------------------------------------------- 档案 → 任务链
def _fight_params(t):
    """``理智作战``：用药 / 碎石 / 次数

    次数**只在档案真的勾了「限制次数」时才给**：MaaCore 的 ``times`` 写 0 是
    「一次都不打」，不是「不限」；不给这个键就按 maaplan 的口径算（单关刷到
    理智不够、多关每关一次）。
    """
    p = {'medicine': int(t.get('MedicineCount') or 0) if t.get('UseMedicine') else 0,
         'stone': int(t.get('StoneCount') or 0) if t.get('UseStone') else 0}
    try:
        n = int(t.get('TimesLimit') or 0)
    except (TypeError, ValueError):
        n = 0
    if t.get('EnableTimesLimit') and n > 0:
        p['times'] = n
    return p


def _recruit_params(t):
    sel = _stars(t.get('Level3Choose'), t.get('Level4Choose'),
                 t.get('Level5Choose'), t.get('Level6Choose')) or [3, 4]
    return {
        'refresh': bool(t.get('ForceRefresh', True)),
        'select': sel,
        'confirm': list(sel),
        'first_tags': list(t.get('PreserveTagList') or []) if t.get('PreserveTagEnabled') else [],
        'extra_tags_mode': int(t.get('ExtraTagMode') or 0),
        'times': int(t.get('MaxTimes') or 0),
        'expedite': False,
    }


def _infrast_params(t):
    rooms = [str(r.get('Room')) for r in (t.get('RoomList') or []) if r.get('Room')]
    return {
        'mode': 0,
        'facility': rooms or ['Mfg', 'Trade', 'Control', 'Power', 'Reception',
                              'Office', 'Dorm', 'Processing', 'Training'],
        # UsesOfDrones 的取值（Money / SyntheticJade / CombatRecord / PureGold /
        # OriginStone / NotUse）和 MaaCore 的 drones 是同一套字符串，直接用
        'drones': str(t.get('UsesOfDrones') or 'NotUse'),
        'threshold': _pct(t.get('DormThreshold')),
        'replenish': bool(t.get('OriginiumShardAutoReplenishment')),
        'dorm_notstationed_enabled': bool(t.get('DormFilterNotStationed')),
        'dorm_trust_enabled': bool(t.get('DormTrustEnabled')),
        'reception_message_board': bool(t.get('ReceptionMessageBoard', True)),
        'reception_clue_exchange': bool(t.get('ReceptionClueExchange', True)),
        'reception_send_clue': bool(t.get('SendClue', True)),
        # GUI 的 ``plan_index`` 从 -1 开始（-1 = 用默认排班），照抄，别归零
        'filename': str(t.get('Filename') or ''),
        'plan_index': int(t.get('PlanSelect') if t.get('PlanSelect') is not None else -1),
    }


def _mall_params(t):
    return {
        'shopping': bool(t.get('Shopping', True)),
        'buy_first': _split_list(t.get('FirstList')),
        'blacklist': _split_list(t.get('BlackList')),
        'visit_friends': bool(t.get('VisitFriends', True)),
        'credit_fight': bool(t.get('CreditFight', True)),
        # MAA 的 FormationIndex 从 0 起，GUI 里显示的是 1 起
        'formation_index': max(0, int(t.get('CreditFightFormation') or 1) - 1),
        'force_shopping_if_credit_full': bool(t.get('ShoppingIgnoreBlackListWhenFull')),
        'only_buy_discount': bool(t.get('OnlyBuyDiscount')),
        'reserve_max_credit': bool(t.get('ReserveMaxCredit')),
    }


def _award_params(t):
    return {
        'award': bool(t.get('Award', True)),
        'mail': bool(t.get('Mail', True)),
        # 免费抽卡在 MaaCore 里的键叫 recruit（是「抽」不是「招募」）
        'recruit': bool(t.get('FreeGacha')),
        'orundum': bool(t.get('Orundum', True)),
        'mining': bool(t.get('Mining', True)),
        'specialaccess': bool(t.get('SpecialAccess', True)),
    }


_MAPPERS = {
    'Fight': _fight_params,
    'Recruit': _recruit_params,
    'Infrast': _infrast_params,
    'Mall': _mall_params,
    'Award': _award_params,
}


def chain_of_queue(queue):
    """档案的 ``TaskQueue`` → ``(tasks, stages, chain)``

    - ``tasks``：启用了的日常任务 key，保持用户档案里的顺序（他在 MAA 里怎么
      排的，我们就怎么排；没排到的任务不替他补）
    - ``stages``：理智作战的关卡列表（``StagePlan`` 原样，可能是 1-7 这种多关
      轮换，也可能是 Annihilation 剿灭）
    - ``chain``：各类任务的 MaaCore 参数（键名见 MAA 的集成文档）
    """
    keys, stages, chain = [], [], {}
    for t in (queue or []):
        tt = str(t.get('TaskType') or '')
        if not tt or tt in SKIP_TYPES or not t.get('IsEnable'):
            continue
        key = KEY_OF.get(tt)
        if not key:
            continue
        if key not in keys:
            keys.append(key)
        fn = _MAPPERS.get(tt)
        if fn:
            chain[key] = fn(t)
        if tt == 'Fight':
            for s in (t.get('StagePlan') or []):
                s = str(s or '').strip()
                if s and s not in stages:
                    stages.append(s)
    return keys, stages, chain


def load_default():
    """读白本模板；坏了/没打包进去就退回 maaplan 的内置默认值"""
    try:
        with open(DEFAULT_PATH, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get('tasks'):
            d.setdefault('chain', {})
            return d
    except (OSError, ValueError):
        pass
    chain = {}
    for key in ORDER:
        ttype = maaplan.TASK_MAP[key]['type']
        proto = copy.deepcopy(maaplan._DEFAULTS.get(ttype) or {})
        fn = _MAPPERS.get(ttype)
        if fn:
            chain[key] = fn(proto)
    return {'tasks': list(ORDER), 'chain': chain, 'stage': '', 'source': '内置默认'}


# ---------------------------------------------------------------- 账号 → 档案
def _load_cfg(maa_dir):
    try:
        return maa._load_cfg(maa_dir) or {}
    except Exception:
        return {}


def _match_profile(c, cfg):
    """按切号名反查档案（用户没显式绑也能对上）

    只在 ``StartUp.AccountName`` 真的等于这个号的切号名时才认——不然八份档案
    里随便挑一份，关卡就可能是别人的。
    """
    from . import acctname

    nm = acctname.normalize(c.get('accountName') or '')
    if not nm:
        return '', None
    for name, prof in (cfg.get('Configurations') or {}).items():
        if str(name).startswith(maaplan.PROFILE_PREFIX):
            continue                      # 我们上一轮建的档案，不当模板
        su = next((t for t in (prof.get('TaskQueue') or [])
                   if t.get('TaskType') == 'StartUp'), None)
        if acctname.normalize((su or {}).get('AccountName') or '') == nm:
            return name, prof
    return '', None


def _conn_of(prof):
    """档案里的连接设置（只用来判断「这个号在哪台模拟器」）"""
    cs = ((prof or {}).get('Gui') or {}).get('ConnectSettings') or {}
    return {'address': str(cs.get('Address') or '').strip(),
            'emu': str(cs.get('Config') or '').strip()}


# ---------------------------------------------------------------- 推荐关卡
def recommended():
    """当前还开着的活动，推荐刷它的材料关（默认 -8）

    用户口径（2026-11-26）：「活动给他推荐正在开启的材料关卡，一般是
    XX-6 / 7 / 8，默认 8」。只在 ``eventnav.ENTRIES`` 里没结束的活动里挑，
    挑完还要让资源包认得出这个关卡代号，认不出就往下退一档。
    """
    from . import eventnav

    today = datetime.date.today().isoformat()
    out = []
    for pre, ent in (eventnav.ENTRIES or {}).items():
        if ent.get('closed'):
            continue
        until = str(ent.get('until') or '')
        if until and until < today:
            continue
        for n in (8, 7, 6):
            code = '%s-%d' % (pre, n)
            try:
                res = resources.resolve(code)
                ok = any((s.get('code') or '') == code for s in (res.get('stages') or []))
            except Exception:
                ok = False
            if ok:
                out.append({'code': code, 'activity': ent.get('name') or pre,
                            'until': until})
                break
    return out


# ---------------------------------------------------------------- 探测
def detect(req=None):
    """读每个号「现在打算怎么长草」，**只读不写、不碰模拟器**

    返回 ``accounts[]``：``source='profile'`` 说明读到了他自己的 MAA 档案
    （带关卡和任务开关）；``'default'`` 说明没有档案、用的是白本。
    """
    req = req or {}
    md = multiacct._maa_dir(req)
    cands = multiacct.candidates()
    if not cands:
        return {'ok': False, 'error': '左侧没有可挂的明日方舟账号',
                'accounts': [], 'needStage': [], 'missing': [], 'recommended': []}

    want = [str(x) for x in (req.get('accounts') or []) if str(x).strip()]
    want = [w for w in want if w not in ('all', '左侧全部', '全部')]
    if want:
        by = {c['file']: c for c in cands}
        unknown = [f for f in want if f not in by]
        picked = [by[f] for f in want if f in by]
        if not picked:
            return {'ok': False, 'error': '点名的账号都不在左侧列表里',
                    'unknown': unknown, 'accounts': [], 'needStage': [],
                    'missing': [], 'recommended': recommended()}
    else:
        unknown, picked = [], cands

    tpl = load_default()
    rows, need_stage, missing, warnings = [], [], [], []
    for c in picked:
        d = c.get('maaDir') or md
        cfg = _load_cfg(d)
        name = c.get('maaProfile') or ''
        prof = (cfg.get('Configurations') or {}).get(name) if name else None
        if not prof:
            name, prof = _match_profile(c, cfg)
            if prof and c.get('maaProfile'):
                warnings.append('账号「%s」绑的档案「%s」在这台 MAA 里找不到，'
                                '按切号名对到了「%s」' % (c['name'], c['maaProfile'], name))
        if prof:
            keys, stages, chain = chain_of_queue(prof.get('TaskQueue'))
            # 只剩「开始唤醒」等于这个号根本没配日常（moren 那种只挂干员培养的
            # 档案）。这种照抄过来就是「唤醒了什么都不干」，退回白本更合理。
            if set(keys) <= {'startup'}:
                keys = list(tpl['tasks'])
                chain = copy.deepcopy(tpl.get('chain') or {})
                warnings.append('账号「%s」的档案「%s」里日常任务一个都没勾，'
                                '按白本的一键长草排' % (c['name'], name))
                prof = None
        if prof:
            source = 'profile'
            address = _conn_of(prof).get('address') or ''
        else:
            keys = list(tpl['tasks'])
            stages = [tpl['stage']] if tpl.get('stage') else []
            chain = copy.deepcopy(tpl.get('chain') or {})
            source = 'default'
            address = ''
            name = ''
        # 账号上钉过的模拟器优先：档案里的地址八份常常全指向同一台，靠它分不开
        address = c.get('adbAddress') or address
        # 档案读到了但理智作战留空（或压根没这个任务）→ 关卡得问用户
        need = not stages
        if need:
            need_stage.append({'file': c['file'], 'name': c['name'],
                               'profile': name, 'source': source,
                               'question': '账号「%s」（%s）要刷哪一关？' % (
                                   c['name'], c.get('channel') or '渠道未知')})
        if not (c.get('accountName') or ''):
            missing.append({'what': 'accountName', 'file': c['file'],
                            'name': c['name'],
                            'question': '账号「%s」的切号名是什么？（官服给手机号后 4 位，'
                                        'B 服给游戏内用户名）' % c['name']})
        rows.append({
            'file': c['file'], 'name': c['name'],
            'channel': c.get('channel') or '',
            'accountName': c.get('accountName') or '',
            'profile': name, 'source': source,
            'maaDir': d, 'address': address,
            'tasks': keys, 'stages': list(stages), 'chain': chain,
            'stage': '',        # 等 plan 定
            'needStage': need,
        })

    return {
        'ok': True, 'maaDir': md, 'accounts': rows,
        'needStage': need_stage, 'missing': missing, 'unknown': unknown,
        'warnings': warnings, 'recommended': recommended(),
        'summary': _summary(rows),
    }


def _summary(rows):
    prof = [r for r in rows if r['source'] == 'profile']
    own = [r for r in rows if r['source'] == 'default']
    bits = []
    if prof:
        bits.append('%d 个号读到了自己的 MAA 档案' % len(prof))
    if own:
        bits.append('%d 个号没有档案（用白本一键长草）' % len(own))
    staged = [r for r in rows if r['stages']]
    if staged:
        codes = sorted({s for r in staged for s in r['stages']})
        bits.append('理智作战已在刷：%s' % '、'.join(codes[:8]))
    return '；'.join(bits) or '没有账号'


# ---------------------------------------------------------------- 排方案
def plan(req=None):
    """把「怎么长草」定下来：每个号一条任务链，不再碰作业站

    ``req`` 里 ``stage`` 给了就**所有号都用它**（用户确认/改口时以他为准），
    ``stages`` 是 ``{账号文件: 关卡}`` 的逐号覆盖；``fallbackStage`` 只补那些
    「自己配置里没写关卡」的号（推荐设置就是走这条，别把别人配好的关卡改掉）。
    """
    req = req or {}
    info = detect(req)
    if not info.get('ok'):
        return info

    fixed = str(req.get('stage') or '').strip()
    per = {str(k): str(v or '').strip()
           for k, v in (req.get('stages') or {}).items()}
    fallback = str(req.get('fallbackStage') or '').strip()
    tpl_stage = str((load_default() or {}).get('stage') or '').strip()

    rows, need = [], []
    for a in info['accounts']:
        own = a['stages'][0] if a['stages'] else ''
        st = (per.get(a['file']) or fixed or own or fallback or tpl_stage)
        if not st:
            need.append({'file': a['file'], 'name': a['name'],
                         'question': '账号「%s」要刷哪一关？' % a['name']})
            continue
        row = dict(a)
        override = bool(fixed or per.get(a['file']) or not a['stages'])
        row['stages'] = [st] if override else list(a['stages'])
        row['stage'] = st
        rows.append(row)

    if not rows:
        return {'ok': False, 'error': '每个号都还不知道刷哪一关',
                'needStage': need, 'recommended': info.get('recommended') or [],
                'accounts': info['accounts'], 'missing': info.get('missing') or []}

    return {'ok': True, 'rows': rows, 'maaDir': info['maaDir'],
            'needStage': need, 'missing': info.get('missing') or [],
            'warnings': info.get('warnings') or [],
            'recommended': info.get('recommended') or [],
            'summary': _plan_text(rows)}


def _plan_text(rows):
    lines = ['%d 个账号，用 MAA 的一键长草 + 理智作战' % len(rows)]
    for r in rows:
        src = '自己的档案 %s' % r['profile'] if r['profile'] else '白本配置'
        lines.append('· %s（切号名 %s，%s）→ %s' % (
            r['name'], r['accountName'] or '未填', src,
            '%s · %s' % ('、'.join(r['stages']),
                         ' → '.join(maaplan.TASK_MAP[k]['label']
                                    for k in r['tasks'] if k in maaplan.TASK_MAP))))
    return '\n'.join(lines)


# ---------------------------------------------------------------- 执行
def run(plan_data, tid=None):
    """按模拟器分组跑：同台轮替、不同台并行（逻辑全在 maaplan.run_plan）

    这里只把「每个号自己的任务清单 / 关卡 / 档案」塞进 row，让 ``_run_group``
    逐号生成 spec —— 一个号一条任务链，切号靠 StartUp 的 account_name。
    """
    rows = plan_data.get('rows') or []
    spec_base = {'tasks': list(ORDER), 'maaDir': plan_data.get('maaDir') or ''}
    return maaplan.run_plan(rows, spec_base, tid)


def run_async(plan_data):
    """后台跑（助手工具用）：返回 taskId，日志进跑机面板"""
    rows = plan_data.get('rows') or []
    tid = tasks.create('一键长草（%d 个账号）' % len(rows),
                       owners=[r.get('file') for r in rows if r.get('file')])

    def go():
        import traceback
        try:
            tasks.finish(tid, result=run(plan_data, tid))
        except Exception as e:                      # noqa: BLE001
            tasks.finish(tid, error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    import threading
    threading.Thread(target=go, daemon=True, name='daily-run').start()
    return tid
