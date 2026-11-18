# -*- coding: utf-8 -*-
"""M4 内置助手：一个只认工具的 agent loop

设计上刻意做窄
--------------
模型能做的每一件事，都是这里预先允许的 10 个工具之一。**不给文件系统、
不给 shell、不给任意 HTTP**。它拿不到的东西就干不了，这比「提示词里写
不许干」可靠一万倍。

三道防线
--------
1. **关卡必须过表**：模型给的 stageCode 一律先进 ``resources.resolve``，
   查不到就把候选回给它重选，绝不直接透传给 MAA。
2. **写前必须确认**：``apply_plan`` / ``add_stages`` 在 ``autoApply`` 关闭时
   只返回 ``need_confirm``，等用户点确认再动。
3. **工具白名单**：见 ``TOOLS``。

为什么不用 MCP
--------------
我们只有一个宿主（自己的窗口），MCP 每个工具的 schema 是常驻上下文的，
工具一多反而更费；CLI + 手写 schema 更省也更可控。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import traceback
import urllib.request
import uuid

from . import (accounts, asstlog, copilot, maa, multiacct, progress,
               resources, settings, skills, tasks, vault, watch)

MAX_TURNS = 12
KEEP_MESSAGES = 24
KEY_PATH = os.path.join(settings.CONFIG_DIR, 'agent.key')

# ---------------------------------------------------------------- 密钥
def _load_key() -> str:
    """读 DPAPI 加密的 apiKey；没有 / 解不开返回空串"""
    try:
        with open(KEY_PATH, 'rb') as f:
            return vault.unprotect(f.read()).decode('utf-8')
    except Exception:
        return ''


def _save_key(key: str) -> None:
    if not key:
        try:
            os.remove(KEY_PATH)
        except OSError:
            pass
        return
    os.makedirs(settings.CONFIG_DIR, exist_ok=True)
    blob = vault.protect(key.encode('utf-8'))
    tmp = KEY_PATH + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, KEY_PATH)


def config():
    """前端要的配置视图：有 key 只说有没有，绝不回传明文"""
    st = settings.load().get('agent') or {}
    return {
        'baseUrl': st.get('baseUrl') or '',
        'model': st.get('model') or '',
        'autoApply': bool(st.get('autoApply')),
        'hasKey': bool(_load_key()),
    }


def save_config(patch):
    cur = dict(settings.load().get('agent') or {})
    for k in ('baseUrl', 'model', 'autoApply'):
        if k in (patch or {}):
            cur[k] = patch[k]
    settings.save({'agent': cur})
    if 'apiKey' in (patch or {}):
        _save_key(str(patch.get('apiKey') or '').strip())
    return config()


# ---------------------------------------------------------------- LLM
class LLMError(RuntimeError):
    pass


def _payload(messages, tools=None, stream=True):
    st = settings.load().get('agent') or {}
    model = st.get('model') or ''
    if not model:
        raise LLMError('还没设置模型名（设置 → 内置助手）')
    body = {'model': model, 'messages': messages, 'temperature': 0.2,
            'stream': stream}
    # 不给 max_tokens 时用中转站默认值（常见 512/1024）：回复长一点就被
    # 硬截断在句中，前端看着像「话没说完」（2026-11-17 用户反馈）。
    # 设置里配了就用配的，没配给 4096 兜底。
    try:
        mt = int(st.get('maxTokens') or 0)
    except (TypeError, ValueError):
        mt = 0
    body['max_tokens'] = mt if mt > 0 else 4096
    if tools:
        body['tools'] = tools
        body['tool_choice'] = 'auto'
    return body


def _post(body):
    st = settings.load().get('agent') or {}
    url = (st.get('baseUrl') or '').rstrip('/') + '/chat/completions'
    key = _load_key()
    if not key:
        raise LLMError('还没填 API Key（设置 → 内置助手）')
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
        'Accept': 'text/event-stream' if body.get('stream') else 'application/json',
        'User-Agent': 'MAA-PRTS-agent',
    })
    try:
        from . import net
        return net.open(req, timeout=180)
    except Exception as e:
        from . import net as _n
        body_txt = ''
        try:
            body_txt = _n.err_body(e)
        except Exception:
            body_txt = str(e)
        raise LLMError(_explain_http(e, body_txt))


def _explain_http(e, text):
    """把各家的报错翻成人话——用户看到的应该是一句能照着改的提示"""
    code = getattr(e, 'code', None)
    head = {401: 'API Key 不对或过期了', 403: '这把 Key 没有调用该模型的权限',
            404: 'Base URL 或模型名不对（找不到这个接口/模型）',
            429: '触发了限流或余额不足', 400: '请求被拒绝（模型名可能写错，'
                                          '或该模型不支持工具调用）'}
    msg = head.get(code) or ('HTTP %s' % code if code else '连不上服务')
    snippet = (text or '').strip().replace('\n', ' ')[:200]
    return '%s%s' % (msg, ('：' + snippet) if snippet else '')


def _stream(messages, tools, on_delta):
    """流式调用；返回 (content, tool_calls, finish_reason)

    tool_calls 按 index 累积——OpenAI 兼容协议里名字和参数都是分片下发的，
    要拼完才能解析。finish_reason 用来区分「说完了」和「撞上限被截断」。
    """
    resp = _post(_payload(messages, tools, stream=True))
    content, calls, finish = '', {}, ''
    buf = ''
    for raw in resp:
        try:
            line = raw.decode('utf-8', 'ignore')
        except Exception:
            continue
        buf += line
        while '\n' in buf:
            cur, buf = buf.split('\n', 1)
            cur = cur.strip()
            if not cur.startswith('data:'):
                continue
            data = cur[5:].strip()
            if not data or data == '[DONE]':
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            for ch in (obj.get('choices') or []):
                if ch.get('finish_reason'):
                    finish = ch['finish_reason']
                delta = ch.get('delta') or {}
                piece = delta.get('content')
                if piece:
                    content += piece
                    if on_delta:
                        on_delta(piece)
                for tc in (delta.get('tool_calls') or []):
                    idx = tc.get('index', 0)
                    slot = calls.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                    if tc.get('id'):
                        slot['id'] = tc['id']
                    fn = tc.get('function') or {}
                    if fn.get('name'):
                        slot['name'] += fn['name']
                    if fn.get('arguments'):
                        slot['arguments'] += fn['arguments']
    out = [{'id': v['id'] or ('call_%d' % i), 'type': 'function',
            'function': {'name': v['name'], 'arguments': v['arguments'] or '{}'}}
           for i, v in sorted(calls.items()) if v['name']]
    return content, out, finish


def _plain(messages, tools):
    """非流式兜底：有些中转站不支持 stream + tools 同时用"""
    resp = _post(_payload(messages, tools, stream=False))
    try:
        obj = json.loads(resp.read().decode('utf-8', 'ignore'))
    except Exception as e:
        raise LLMError('返回的不是合法 JSON：%s' % e)
    ch = (obj.get('choices') or [{}])[0]
    msg = ch.get('message') or {}
    return ((msg.get('content') or ''), (msg.get('tool_calls') or []),
            ch.get('finish_reason') or '')


# ---------------------------------------------------------------- 工具
def _tool(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': desc,
        'parameters': {'type': 'object', 'properties': props or {},
                       'required': required or []},
    }}


_ACC = {'type': 'string', 'description': '账号文件名（list_accounts 返回的 file）；留空表示默认账号'}
_STAGE = {'type': 'string', 'description': '关卡关键词：TL-5 / 15-3 / act55side / 活动中文名 / 章号'}

TOOLS = [
    _tool('list_accounts',
          '列出左侧所有账号（含等级、主线进度摘要、是否绑定 MAA 档案）。'
          '任何涉及"所有账号""这些账号"的操作都先调它拿 file。'),
    _tool('list_activities',
          '列出最近开启的活动（活动正式名 + 代号前缀 + 关卡数）。'
          '用户说"这周的活动""新活动"这类模糊说法时用它确认指哪个。',
          {'limit': {'type': 'integer', 'description': '返回几个，默认 12'}}),
    _tool('search_stages',
          '把关键词翻成具体关卡，返回关卡代号/内部 id/难度/是否剧情关。'
          '**任何关卡号都必须先过这一步**，不要凭印象写 stageCode。',
          {'keyword': _STAGE}, ['keyword']),
    _tool('find_ops',
          '查这些关卡在作业站上有哪些作业，按该账号练度分档。'
          '返回每关最优几份（copilotId / 星级 / 能否直接抄）。',
          {'keyword': _STAGE, 'account': _ACC,
           'limit': {'type': 'integer', 'description': '每关返回几份，默认 3'}},
          ['keyword']),
    _tool('op_detail',
          '看一份作业缺什么：哪些干员练度不够（橙）、要借谁（紫）。',
          {'copilot_id': {'type': 'integer', 'description': '作业 id'},
           'account': _ACC}, ['copilot_id']),
    _tool('account_progress',
          '看一个账号的游戏进度：主线推到哪、还差几关，剿灭几张没打满，'
          '活动还有几个区没清。回答"我还差哪些关"必须用它，不要猜。',
          {'account': _ACC}),
    _tool('run_status',
          'MAA 现在的运行状态：在跑第几关、卡在哪个环节、刚才失败在哪。'
          '回答"现在跑到哪了""为什么失败"用它。',
          {'maa_dir': {'type': 'string', 'description': '留空用默认 MAA 目录'}}),
    _tool('plan_multi_account',
          '给多个账号排一套挂机方案（只生成，不写盘、不启动）。'
          '会按每个账号各自的练度挑能打的作业。'
          '缺切号名（AccountName）时返回 missing，需要向用户要。'
          '**只排「抄作业」的方案**；如果用户要配日常任务（公招/基建/领奖），'
          '用 build_run 更直接。',
          {'accounts': {'type': 'array', 'items': {'type': 'string'},
                        'description': '账号文件名列表；空数组=左侧全部'},
           'keyword': _STAGE,
           'kind': {'type': 'string', 'enum': ['activity', 'permanent'],
                    'description': '关卡类型，只影响提示文案'},
           'names': {'type': 'object',
                     'description': '可选。{账号文件名: 手机号后4位或账号名}。'
                                    '用户已经给了的填这里，省得重复问'},
           'limitStages': {'type': 'integer', 'description': '最多挂几关，默认 8'}},
          ['keyword']),
    _tool('apply_plan',
          '把 plan_multi_account 排出的方案写进 MAA 并开始跑。'
          '**调用前必须先让用户确认要挂哪些账号、哪些关**；'
          'autoApply 关闭时本工具只会返回待确认。',
          {'plan_id': {'type': 'string', 'description': 'plan_multi_account 返回的 planId'},
           'start': {'type': 'boolean', 'description': '写完是否立刻启动，默认 true'}},
          ['plan_id']),
    _tool('maa_capabilities',
          '看这台 MAA 能配哪些任务（开始唤醒/自动公招/基建换班/理智作战/'
          '自动战斗/信用收支/领取奖励/生息演算…）以及现成的几种预设。'
          '用户问「你能干什么」「能不能自动做日常」时用它回答，不要凭印象说。'),
    _tool('build_run',
          '配一套完整的挂机任务并开跑（**主力工具**）。'
          '会新建一份 PRTS- 开头的配置档案（不动用户自己的档案），按需要拼任务队列、'
          '下作业，然后直接驱动 MAA 核心执行 —— 没人需要去界面点开始，日志自己往跑机面板写。'
          '调用前必须让用户确认「给谁、跑什么、挂哪几关」。',
          {'accounts': {'type': 'array', 'items': {'type': 'string'},
                        'description': '账号文件名列表；空数组=左侧全部方舟账号'},
           'mode': {'type': 'string',
                    'enum': ['copy', 'fight', 'daily', 'daily_copy'],
                    'description': 'copy=只抄作业（要 keyword）；'
                                   'fight=只刷理智（要 keyword，不抄作业，纯挂关卡）；'
                                   'daily=只做日常（公招/基建/领奖，不要关卡）；'
                                   'daily_copy=日常+抄作业（要 keyword）。默认 copy'},
           'keyword': _STAGE,
           'tasks': {'type': 'array', 'items': {'type': 'string'},
                     'description': '可选，直接指定任务顺序，覆盖 mode。'
                                    '可用值：startup recruit infrast fight '
                                    'copilot mall award reclamation roguelike'},
           'formation': {'type': 'boolean',
                         'description': '抄作业时是否自动编队，默认 true'},
           'names': {'type': 'object',
                     'description': '{账号文件名: 切号名}。官服填手机号后 4 位'
                                    '（如 5505），B 服填游戏内用户名。'
                                    '缺的时候要问用户，**原样传**，别自己拼格式'},
           'limitStages': {'type': 'integer', 'description': '最多挂几关，默认 8'},
           'profile': {'type': 'string',
                       'description': '档案名后缀，比如「活动-时间啊向前」。不给就自动起名'},
           'start': {'type': 'boolean', 'description': '写完是否立刻启动，默认 true'}},
          []),
    _tool('list_profiles',
          '看 MAA 里现在有哪些配置档案、当前用的是哪个、各自挂了几个作业。'
          '用户问「现在跑的是哪个号」时用它。'),
    _tool('stop_run',
          '停掉正在跑的 MAA（关进程）。用户说「停下来」时用。',
          {'maa_dir': {'type': 'string', 'description': '留空用默认目录'}}),
    _tool('add_stages',
          '给**一个**账号挂一组关卡的快捷方式（等价于 mode=copy 的 build_run）。',
          {'account': _ACC, 'keyword': _STAGE,
           'start': {'type': 'boolean', 'description': '写完是否立刻启动，默认 true'}},
          ['keyword']),
    # ---- 挂关 skill 化（2026-11-17）：唤醒 / 导航 / 剧情 / 战斗 / 签到 / 周目 ----
    _tool('skill_wakeup',
          '唤醒游戏到主界面（游戏没开/停在登录页/START 页时用）。'
          '可选 accountName 按切号名换账号（官服=手机号后 4 位，如 5505）。'
          '返回 taskId，后台跑。',
          {'account': _ACC,
           'accountName': {'type': 'string',
                           'description': '切号名；不给就唤醒当前账号'}}),
    _tool('skill_navigate',
          '把游戏导航到指定活动（老活动走曲谱，当期活动走区域列表；EX / S / MO 关会自动切到活动地图里的对应分区）。'
          'stage 可以传**中文活动名（推荐，如「孤星」「愚人号」）**、关卡号'
          '（MN-1 / TL-5 / XX-EX-1）或活动 id（act25side），内部会自己换成真实'
          '关卡代号。**别自己凭记忆把中文名换算成代号**（孤星是 CW，不是 SV）。'
          '返回 taskId。',
          {'stage': _STAGE,
           'activity': {'type': 'string',
                        'description': '活动中文名（如「孤星」「愚人号」）；'
                                       '和 stage 二选一，中文名最不容易错'},
           'account': _ACC}, []),
    _tool('skill_skip_story',
          '过剧情关（MN-ST-1 这类 ST 关）：自动点开 ST 关并跳过剧情。'
          '周目流程里每遇到 ST 关就调一次。返回 taskId。',
          {'stage': _STAGE, 'account': _ACC}, ['stage']),
    _tool('skill_fight',
          '自动战斗：编队 / 借助战 / 吃理智药 / 开打，一次挂一组关。'
          'stages 传关卡代号列表（如 ["MN-1","MN-2"]），mode=copy 抄作业 / '
          'fight 纯刷关。**开打前会自动预检**：按游戏顺序排、把没打的前置关'
          '补进计划（如要 BB-S-3 会先打 BB-S-1、BB-S-2），再拿作业要求比对'
          '这号的练度——练度不够/缺干员/没作业的关**不会排队**，结果里的 '
          'precheck 会列出哪几关打不了、为什么。'
          '**默认不唤醒不导航**——界面在哪就从哪直接开打；'
          '用户还在主界面 / 游戏没开时要先 skill_navigate 或 skill_wakeup，'
          '或传 wakeup=true 走全套。**调用前先跟用户确认要打哪些关**。'
          '返回 taskId。',
          {'stages': {'type': 'array', 'items': {'type': 'string'}},
           'mode': {'type': 'string', 'enum': ['copy', 'fight']},
           'wakeup': {'type': 'boolean',
                      'description': 'true=连唤醒+导航一起跑（默认 false 直跑）'},
           'autoPrereq': {'type': 'boolean',
                          'description': 'true=基础关的前置也一起补。默认只自动补 '
                                         'EX/S/MO 的前置；mode=fight 纯刷关默认一关'
                                         '都不补（刷材料时补上就是白烧理智）'},
           'account': _ACC}, ['stages']),
    _tool('skill_weekly',
          '**周目编排（一站式）**：把一个活动从头打到尾——ST 剧情关自动过、'
          '主线按顺序批量打、EX 最后清。stage 传活动代号前缀（MN / TL）。'
          '**调用前必须让用户确认活动与账号**。任何一步失败会停下并报卡在哪关。',
          {'stage': _STAGE, 'account': _ACC,
           'mode': {'type': 'string', 'enum': ['copy', 'fight'],
                    'description': '战斗关抄作业还是纯刷，默认 copy'}},
          ['stage']),
    _tool('skill_signin',
          '森空岛签到（账号给了只签这个号，不给签全部）。返回 taskId。',
          {'account': _ACC}),
    _tool('skill_train',
          '培养干员（MAA 的「干员培养」）：升精英化 / 技能基础等级 / 专精，'
          '可以一次排多个干员。operators 每项写 {name, elite, skillLevel, mastery}：'
          'elite 填 1/2（升到精一 / 精二），skillLevel 填 2~7，mastery 是**绝对专精'
          '等级**的 3 元数组（[0,3,0] = 技能2 专三；技能2 已经是专三就不用写）。'
          '**动手前先跟用户确认名单和每个干员的练度目标**。默认先「开始唤醒」；'
          'switchAccount=true 才切号（用户说不用切号就传 false）。'
          '材料不足会暂停并报缺什么，之后用 resume=true 接着做。返回 taskId。',
          {'operators': {
              'type': 'array',
              'description': '要培养的干员列表，按顺序做',
              'items': {
                  'type': 'object',
                  'properties': {
                      'name': {'type': 'string', 'description': '干员名（中文名）'},
                      'elite': {'type': 'integer',
                                'description': '目标精英化 1 或 2；不升就不写'},
                      'skillLevel': {'type': 'integer',
                                     'description': '目标技能基础等级 2~7；不升就不写'},
                      'mastery': {'type': 'array', 'items': {'type': 'integer'},
                                  'description': '[技能1,技能2,技能3] 的专精等级 0~3，'
                                                 '如 [0,3,0] = 二技能专三'},
                  },
                  'required': ['name'],
              }},
           'operator': {'type': 'string', 'description': '只培养一个干员时的简写'},
           'elite': {'type': 'integer', 'description': '简写模式的目标精英化（1/2）'},
           'skillLevel': {'type': 'integer', 'description': '简写模式的目标技能等级（2~7）'},
           'mastery': {'type': 'array', 'items': {'type': 'integer'},
                       'description': '简写模式的专精目标，如 [0,3,0]'},
           'account': _ACC,
           'accountName': {'type': 'string',
                           'description': '切号名（官服=手机号后 4 位）。'
                                          '账号已绑定尾号（list_accounts 的 '
                                          'accountName 非空）时不用传，程序会自己用'},
           'switchAccount': {'type': 'boolean',
                             'description': 'false=只唤醒、不切号（用户说不用换号时传）'},
           'wakeup': {'type': 'boolean',
                      'description': 'false=不唤醒，直接从当前界面开始（游戏得开着）'},
           'resume': {'type': 'boolean',
                      'description': 'true=接着上次没做完的培养计划'}},
          ['operators']),
]


# mode → 默认任务链。reason 用来在确认卡片上解释这套在干什么
MODE_TASKS = {
    'copy': ['startup', 'copilot'],
    'fight': ['startup', 'fight'],
    'daily': ['startup', 'recruit', 'infrast', 'mall', 'award'],
    'daily_copy': ['startup', 'recruit', 'infrast', 'copilot', 'mall', 'award'],
}
MODE_DESC = {
    'copy': '抄作业逐关打',
    'fight': '理智作战刷指定关卡（不抄作业）',
    'daily': '日常一条龙（公招 / 基建 / 领奖）',
    'daily_copy': '日常一条龙 + 抄作业',
}


def _stage_rows(keyword, account=None):
    snap = None
    if account:
        try:
            snap = accounts.get_account(account)
        except Exception:
            snap = None
    if snap is None:
        # 没指定账号：拿第一个有练度的当参考，只为把作业池子拉下来
        for c in accounts.list_accounts():
            if (c.get('game') or 'arknights') == 'arknights':
                try:
                    snap = accounts.get_account(c['file'])
                    break
                except Exception:
                    continue
    return copilot.search(keyword, snap or {}, pages=1, limit=8)


def _default_account():
    rows = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    if not rows:
        return ''
    last = settings.load().get('lastAccount') or ''
    if any(r['file'] == last for r in rows):
        return last
    return rows[0]['file']


def _tool_list_accounts(args):
    out = []
    for c in accounts.list_accounts():
        if (c.get('game') or 'arknights') != 'arknights':
            continue
        p = c.get('progress') or {}
        out.append({
            # 账号列表给的是 nickName / alias / channelName，没有 name / channel
            # （2026-11-16：此处曾直接下标 'name'，一问进度就 KeyError，模型连试
            #  两次都拿到同一个异常，最后只能让用户自己报账号名）
            'file': c['file'],
            'name': c.get('alias') or c.get('nickName') or c['file'],
            'channel': c.get('channelName') or '',
            'level': c.get('level'),
            'maaProfile': c.get('maaProfile') or '',
            # 已绑定的切号名（手机尾号）必须给出来：用户 2026-11-18 报
            # 「设置里配过尾号，助手每次还问」——列表里带出来，模型就不用
            # 再开口问；只有这里是空的才轮到问用户。
            'accountName': c.get('maaAccountName') or '',
            # 字段名对齐 progress.summary()：mainRemaining / activityLeft /
            # activityStages 才是真名（曾经写成 mainLeft / activityZones，
            # 结果每个账号都返回 null，模型只能挨个再问一遍 account_progress）
            'mainKnown': p.get('mainKnown'),
            'mainLeft': p.get('mainRemaining'),
            'mainNext': p.get('mainNext'),
            'campaignLeft': p.get('campaignLeft'),
            'activityLeft': p.get('activityLeft'),
            'activityStages': p.get('activityStages'),
        })
    return {'accounts': out, 'count': len(out),
            'hint': 'accountName 就是已绑定的切号名（手机尾号）：**非空的直接按它跑，'
                    '不要再问用户**；只有为空的账号才问「手机号后 4 位」'
                    '（B 服问游戏内用户名）'}


def _tool_list_activities(args):
    n = max(1, min(40, int(args.get('limit') or 12)))
    rows = resources.activity_list(n) or []
    return {'activities': [{'name': r.get('name'), 'prefix': r.get('prefix'),
                            'stages': r.get('count') or r.get('stages')}
                           for r in rows]}


def _tool_search_stages(args):
    kw = str(args.get('keyword') or '').strip()
    if not kw:
        return {'error': '缺少关键词'}
    res = resources.resolve(kw)
    stages = [{'code': s['code'], 'stageId': s['stageId'],
               'difficulty': s.get('difficultyCN') or s.get('difficulty') or '',
               'story': bool(s.get('story')), 'name': s.get('name') or ''}
              for s in (res.get('stages') or [])[:30]]
    out = {'mode': res.get('mode'), 'title': res.get('title'),
           'stages': stages, 'count': len(stages)}
    if stages:
        m = re.match(r'^([A-Za-z][A-Za-z0-9]*)-', stages[0]['code'])
        if m:
            from .stagenav import EVENTS
            pfx = m.group(1).upper()
            out['prefix'] = pfx
            if pfx in EVENTS:
                out['activity'] = EVENTS[pfx]
    if res.get('mode') == 'story':
        out['note'] = '这是剧情关，没有作业可抄，挂机会停在这里'
    st = resources.stories_between(res.get('stages') or []) \
        if hasattr(resources, 'stories_between') else []
    if st:
        out['storyStages'] = [{'code': x['code'], 'name': x.get('name')}
                              for x in st[:8]]
        out['note'] = ('这段剧情里有 %d 个剧情关（%s），MAA 挂机会停在剧情界面'
                       '——我们的自动恢复会关掉重开接着跑，但也可以让用户先手动跳过'
                       % (len(st), '、'.join(x['code'] for x in st[:5])))
    return out


def _tool_find_ops(args):
    kw = str(args.get('keyword') or '').strip()
    if not kw:
        return {'error': '缺少关键词'}
    limit = max(1, min(8, int(args.get('limit') or 3)))
    acc = args.get('account') or _default_account()
    res = _stage_rows(kw, acc)
    rows = []
    for packed in (res.get('stages') or [])[:12]:
        st = packed.get('stage') or {}
        ops = []
        for op in (packed.get('ops') or [])[:limit]:
            ops.append({'copilotId': op.get('copilotId'),
                        'title': (op.get('title') or '')[:60],
                        'rating': op.get('rating'),
                        'mode': op.get('mode')})
        rows.append({'stage': st.get('code'), 'stageId': st.get('stageId'),
                     'difficulty': st.get('difficultyCN') or '',
                     'variant': st.get('variant'), 'ops': ops,
                     'total': packed.get('total')})
    return {'account': acc, 'mode': res.get('mode'), 'title': res.get('title'),
            'rows': rows,
            'note': 'mode: ready=能直接抄 / borrow=要借一个人 / '
                    'train=练度不够 / blocked=打不了'}


def _tool_op_detail(args):
    cid = args.get('copilot_id')
    if not cid:
        return {'error': '缺少 copilot_id'}
    acc = args.get('account') or _default_account()
    d = copilot.operation_detail(int(cid))
    content = d.get('content') or {}
    owned = None
    if acc:
        try:
            owned = copilot.build_owned(accounts.get_account(acc))
        except Exception:
            owned = None
    opers = copilot.detail_opers(content, owned)
    lack, borrow = [], []
    for o in opers:
        if o.get('owned') is False:
            borrow.append(o.get('name'))
    return {
        'id': int(cid),
        'title': (content.get('doc') or {}).get('title') or '',
        'stage': content.get('stage_name') or '',
        'opers': [{'name': o.get('name'), 'owned': o.get('owned'),
                   'ok': o.get('ok'), 'elite': o.get('elite'),
                   'level': o.get('level')} for o in opers],
        'notOwned': borrow,
        'note': 'owned=false 是要借的人；ok=false 是练度不够',
    }


def _tool_account_progress(args):
    acc = args.get('account') or _default_account()
    if not acc:
        return {'error': '还没有账号'}
    snap = accounts.get_account(acc)
    prog = snap.get('progress') or {}
    if not prog:
        return {'account': acc, 'hasProgress': False,
                'note': '这个账号还没同步过森空岛数据，进度得先点「同步」才有'}
    return {'account': acc, 'hasProgress': True,
            'summary': progress.summary(prog),
            'main': {k: v for k, v in (prog.get('main') or {}).items()
                     if k != 'stages'},
            'campaign': [{'name': c.get('name'), 'maxKills': c.get('maxKills'),
                          'full': c.get('full')}
                         for c in (prog.get('campaign') or []) if not c.get('full')][:8],
            'activities': [{'name': a.get('name'), 'left': a.get('left'),
                            'matched': a.get('matched')}
                           for a in (prog.get('activities') or []) if a.get('left')][:8],
            }


def _tool_run_status(args):
    d = args.get('maa_dir') or settings.load().get('maaDir') or ''
    snap = watch.snapshot(d)
    w = watch.get(d)
    return {
        'maaDir': d,
        'state': snap.get('state'),
        'stage': snap.get('stage'),
        'index': snap.get('index'),
        'node': snap.get('node'),
        'retry': '%s/%s' % (snap.get('retry'), snap.get('retryTotal')),
        'explain': asstlog.explain(snap),
        'resumeCount': (w.resume_count if w else snap.get('resumeCount')),
        'recent': [{'kind': e.get('kind'), 'stage': e.get('stage'),
                    'node': e.get('node')} for e in (snap.get('events') or [])[-12:]],
    }


_plans = {}
_plans_lock = threading.Lock()
PLAN_TTL = 3600


def _tool_plan_multi(args):
    req = {
        'keyword': str(args.get('keyword') or '').strip(),
        'kind': args.get('kind') or '',
        'accounts': [str(x) for x in (args.get('accounts') or []) if str(x)],
        'names': args.get('names') or {},
        'limitStages': args.get('limitStages') or 8,
    }
    p = multiacct.plan(req)
    pid = uuid.uuid4().hex[:10]
    p['planId'] = pid
    with _plans_lock:
        now = time.time()
        for k in [k for k, v in _plans.items() if now - v['at'] > PLAN_TTL]:
            _plans.pop(k, None)
        _plans[pid] = {'at': now, 'plan': p}
    out = {k: p[k] for k in ('ok', 'keyword', 'kind', 'mode', 'totalStages',
                             'totalOps', 'summary', 'missing', 'warnings',
                             'planId')}
    out['accounts'] = [{'file': a['file'], 'name': a['name'],
                        'accountName': a['accountName'], 'profile': a['profile'],
                        'stages': [o['stageCode'] for o in a['ops']],
                        'count': len(a['ops'])} for a in p.get('accounts') or []]
    return out


def _plan_of(pid):
    with _plans_lock:
        row = _plans.get(str(pid))
    if not row:
        raise ValueError('方案过期了或不存在，重新排一次')
    return row['plan']


def _tool_apply_plan(args, session=None):
    plan = _plan_of(args.get('plan_id'))
    start = args.get('start')
    start = True if start is None else bool(start)
    blocked = _guard(session, 'apply_plan',
                     {'plan_id': args.get('plan_id'), 'start': start},
                     _confirm_text(plan))
    if blocked:
        return blocked
    tid = tasks.create('多账号挂机（%d 个账号）' % len(
        [a for a in plan['accounts'] if a.get('ops')]))
    def go():
        try:
            res = multiacct.apply(plan, tid)
            tasks.log(tid, '开始跑 %d 个账号…' % len(res['profiles']))
            if start:
                rep = multiacct.run(plan, tid)
                tasks.finish(tid, result={'profiles': res, 'report': rep})
            else:
                tasks.finish(tid, result=res)
        except Exception as e:
            tasks.finish(tid, error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})
    threading.Thread(target=go, daemon=True, name='agent-apply').start()
    return {'started': True, 'taskId': tid,
            'note': '已开始写入并启动，进度在跑机面板看；不要在这里等它跑完'}


def _tool_add_stages(args, session=None):
    kw = str(args.get('keyword') or '').strip()
    if not kw:
        return {'error': '缺少关键词'}
    acc = args.get('account') or _default_account()
    if not acc:
        return {'error': '还没有账号'}
    res = _stage_rows(kw, acc)
    items, skipped = [], []
    for packed in (res.get('stages') or []):
        st = packed.get('stage') or {}
        best = next((o for o in (packed.get('ops') or [])
                     if o.get('mode') != 'blocked'), None)
        if not best:
            skipped.append(st.get('code'))
            continue
        items.append({'copilotId': best.get('copilotId'),
                      'stageCode': st.get('code'), 'title': best.get('title'),
                      'variant': st.get('variant'), 'name': st.get('code')})
    if not items:
        return {'error': '这份关键词下没有能打的作业', 'skipped': skipped}
    start = args.get('start')
    start = True if start is None else bool(start)
    summary = '给「%s」挂 %d 关：%s\n（会替换 MAA 里现有的作业列表，原来的会被清掉）' % (
        acc, len(items), '、'.join(i['stageCode'] for i in items[:10]))
    blocked = _guard(session, 'add_stages',
                     {'account': acc, 'keyword': kw, 'start': start}, summary)
    if blocked:
        return blocked
    tid = maa.start_apply(items, profile=None, mode='replace',
                          auto_start=start,
                          maa_dir=(accounts.get_account(acc).get('maa') or {}).get('dir') or None)
    return {'started': True, 'taskId': tid, 'count': len(items),
            'skipped': skipped, 'note': '已提交写入 MAA，不要在这里等它跑完'}


def _tool_maa_capabilities(args, session=None):
    """这台 MAA 能配哪些任务 —— 让模型先看清单再开口，别凭印象编"""
    from . import maaplan
    cap = maaplan.capabilities(args.get('maa_dir') or None)
    return {
        'tasks': [{'id': t['id'], 'label': t['label'], 'desc': t['desc'],
                   'available': t['available'], 'needsStage': t['needsStage']}
                  for t in cap['tasks']],
        'prototypes': cap.get('prototypes'),
        'presets': [{'id': p['id'], 'label': p['label'], 'desc': p['desc'],
                     'tasks': p['tasks']} for p in cap['presets']],
        'modes': [{'id': k, 'label': MODE_DESC[k], 'tasks': v}
                  for k, v in MODE_TASKS.items()],
        'note': '写操作都会新建 PRTS- 开头的档案，用户自己的档案一个字段都不动。',
    }


def _tool_list_profiles(args, session=None):
    from . import maaplan
    info = maa.profiles(args.get('maa_dir') or None)
    return {
        'current': info.get('current') or '',
        'profiles': [{
            'name': p['name'],
            'accountName': p.get('accountName') or '',
            'ours': str(p['name']).startswith(maaplan.PROFILE_PREFIX),
            'tasks': p.get('tasks') or 0,
            'ops': p.get('copilot') or 0,
        } for p in (info.get('profiles') or [])],
        'note': 'PRTS- 开头的是本程序建的，随时能删；其余是用户自己的档案。',
    }


def _tool_stop_run(args, session=None):
    watch.stop_all()
    d = args.get('maa_dir') or None
    # 直连跑的时候机器上没有 MAA 进程，光关进程是关不掉的，得让引擎自己 AsstStop
    from . import maacore
    n = maacore.stop_all_live()
    ok = maa.close(d)
    return {'stopped': bool(ok) or bool(n),
            'engines': n,
            'note': '已经停下。没跑完的关卡不会自己续上，下次要重挂。'}


# 官服「账号管理器」里显示的条目是打码手机号：181****5505。MAA 切号就是拿
# AccountName 去 OCR 全匹配这一行，写昵称永远匹配不到 —— 表现是 select_account
# 死循环几十次、最后 "Account switch failed after restart"（2026-11-17 实测，
# 档案 moren 里 AccountName 存的是昵称「小卡拉帕鲁灰」，跑了 4 分钟没进去）。
# B 服显示的是游戏内用户名，昵称才是对的。
#
# 但「必须 3+4 星+4」是错的：MAA 是子串匹配，官方说明写的是「可输入
# 123****4567、4567 或 23****45」。卡死一种格式会让用户照文档填了却被拒，
# 助手只能反复追问。归一化与校验都在 acctname 里，本文件只调它。


def _is_official(channel) -> bool:
    from . import acctname
    return acctname.is_official_channel(channel)


def _check_account_name(nm, channel):
    """切号名是否可用；返回（归一化后的值, 原因）

    ``181 5505`` / ``181＊5505`` / ``5505`` / ``18112345505`` 都能吃下，
    官服只拦「填了昵称」这一种真跑不起来的情况。
    """
    from . import acctname
    return acctname.check(nm, channel)


def _roster(want_files, names, maa_dir):
    """把「哪些账号 + 各自的切号名」理出来

    切号名的来源，只有两个可信的：
    1. 用户这次显式给的 ``names``（还会回写账号文件，下次不用再问）
    2. 账号文件里存过的 ``maa.accountName``（上次给的 / 设置页填的）

    **不从用户的 MAA 配置档案里猜**（2026-11-18 用户口径：agent 模式不碰
    用户的 MAA 配置）：以前会拿 ``maa.profile`` 档案里的 AccountName、甚至
    拿昵称去比对档案名来凑切号名——程序里两个游戏账号对 MAA 里五个尾号
    档案，档案名和 AccountName 根本说不清谁是谁，错配的代价是官服传错号
    MAA 在账号列表里死找 4 分钟超时，整轮挂机死在「登录」。宁可多问一句。
    """
    cands = multiacct.candidates()
    if want_files:
        keep = set(want_files)
        cands = [c for c in cands if c['file'] in keep or c['name'] in keep]
    if not cands:
        return [], [], '没有可挂的明日方舟账号'

    rows, missing = [], []
    for c in cands:
        raw = (names.get(c['file']) or names.get(c['name']) or '')
        raw = str(raw).strip() if raw else ''
        # check() 自己会归一化（181 5505 / 181＊5505 / 5505 都能吃下），
        # 这里别再提前 norm 一遍——曾经两层各自处理，第二层收到的已经是
        # 半成品，星号数量被改坏后又被严格正则判成非法。
        nm, why = _check_account_name(raw, c.get('channel'))
        source = 'user'
        if not nm:
            saved = c.get('accountName') or ''
            nm, _ = _check_account_name(saved, c.get('channel'))
            source = 'saved'
            if nm and nm != saved:      # 老版本存进去的怪格式，顺手纠正
                try:
                    accounts.save_maa_meta(c['file'], {'accountName': nm})
                except Exception:
                    pass
        if not nm:
            official = _is_official(c.get('channel'))
            ask = ('手机号后 4 位是多少？给这四位就行（如 5505）——'
                   'MAA 切号用的就是它，基本不会撞号'
                   if official else '在 MAA 里切号用的名字是什么？（B 服填游戏内用户名）')
            missing.append({
                'file': c['file'], 'name': c['name'],
                'channel': c.get('channel') or '',
                'reason': why if raw else '',
                'question': (
                    '账号「%s」（%s）%s。%s'
                    % (c['name'], c.get('channel') or '未知渠道', ask,
                       ('你给的「%s」%s。' % (raw, why)) if raw and why else '')),
            })
            continue
        if source == 'user':
            try:
                accounts.save_maa_meta(c['file'], {'accountName': nm})
            except Exception:
                pass
        rows.append({'file': c['file'], 'name': c['name'],
                     'channel': c.get('channel') or '',
                     'accountName': nm, 'nameSource': source,
                     'ops': [], 'stages': []})
    return rows, missing, ''


def _tool_build_run(args, session=None):
    """配一套完整任务并启动：建 PRTS- 档案 → 拼任务队列 → 写作业 → 跑起来"""
    from . import maaplan

    # 确认后的重入：args 只有一个 plan_id，方案早就排好了，别再搜一遍作业
    pid = str(args.get('plan_id') or '')
    if pid.startswith('b:'):
        with _plans_lock:
            row = _plans.get(pid)
        if not row:
            return {'error': '方案过期了，重新排一次'}
        return _start_build(row['plan'])

    mode = str(args.get('mode') or 'copy').strip().lower()
    if mode not in MODE_TASKS:
        mode = 'copy'
    kw = str(args.get('keyword') or '').strip()
    formation = args.get('formation')
    formation = True if formation is None else bool(formation)
    start = args.get('start')
    start = True if start is None else bool(start)
    limit = max(1, min(20, int(args.get('limitStages') or 8)))
    names = {str(k): v for k, v in (args.get('names') or {}).items()}
    want_files = [str(x) for x in (args.get('accounts') or []) if str(x).strip()]

    # 任务链：显式 tasks 优先，否则按 mode
    want = [str(t).strip().lower() for t in (args.get('tasks') or [])]
    tl = [t for t in want if t in maaplan.TASK_MAP]
    if want and not tl:
        return {'error': 'tasks 里的值都不认识', 'valid': list(maaplan.TASK_MAP)}
    custom = bool(tl)
    if not tl:
        tl = list(MODE_TASKS[mode])
    need_ops = 'copilot' in tl
    need_stage = need_ops or 'fight' in tl
    if need_stage and not kw:
        return {'error': '还没说挂哪些关',
                'hint': '问用户要关卡关键词（TL-5 / 15-3 / 活动名都行）。'
                        '只想做日常就用 mode=daily，不需要关卡。'}

    d = multiacct._maa_dir({}) or (settings.load().get('maaDir') or '')
    if not d:
        return {'error': '还没设置 MAA 目录（设置 → MAA 根目录）',
                'hint': '让用户在设置里指一下 MAA 装在哪个文件夹'}

    rows, missing, err = _roster(want_files, names, d)
    if err:
        return {'error': err}
    if missing:
        return {'need_info': 'accountName', 'missing': missing, 'ready': len(rows),
                'note': '一次性把所有缺的账号问用户，别一个一个问。'
                        '补齐后带上 names 重新调 build_run。'}
    if not rows:
        return {'error': '没有能挂的账号'}

    # 关卡：fight 用代号列表（MAA 自己导航），copilot 用作业
    fight_stages = []
    if 'fight' in tl:
        try:
            res = resources.resolve(kw)
            fight_stages = [s['code'] for s in (res.get('stages') or [])
                            if not s.get('story')][:limit]
        except Exception as e:
            return {'error': '关卡查不到：%s' % e}
        if not fight_stages:
            return {'error': '关卡「%s」没找到，换个关键词' % kw}

    if need_ops:
        for r in rows:
            try:
                snap = multiacct._snap_of(r['file'])
                res = copilot.search(kw, snap, pages=1, limit=8)
            except Exception as e:
                r['note'] = '搜作业失败：%s' % e
                continue
            for packed in (res.get('stages') or [])[:limit]:
                st = packed.get('stage') or {}
                best = next((o for o in (packed.get('ops') or [])
                             if o.get('mode') != 'blocked'), None)
                if not best:
                    continue
                r['ops'].append({'copilotId': best.get('copilotId'),
                                 'stageCode': st.get('code') or '',
                                 'title': best.get('title') or '',
                                 'variant': st.get('variant')})
            r['stages'] = [o['stageCode'] for o in r['ops']]
        usable = [r for r in rows if r['ops']]
        if not usable:
            return {'error': '这些账号在这批关卡里一份能打的作业都没有',
                    'hint': '换个关键词；或者用 mode=fight 直接刷关卡（不抄作业）'}
        rows = usable

    profile_hint = str(args.get('profile') or '').strip()
    head = '%s · %d 个账号' % (
        MODE_DESC.get(mode) if not custom else '自定义任务链', len(rows))
    lines = [head,
             '任务链：' + ' → '.join(maaplan.TASK_MAP[t]['label'] for t in tl)]
    for r in rows:
        if r['ops']:
            tail = '挂 ' + '、'.join(o['stageCode'] for o in r['ops'][:8])
            if len(r['ops']) > 8:
                tail += ' 等 %d 关' % len(r['ops'])
        else:
            tail = '理智作战 ' + '、'.join(fight_stages[:6])
        lines.append('· %s（切号名 %s）→ %s' % (r['name'], r['accountName'], tail))
    if formation and need_ops:
        lines.append('抄作业时自动编队：开')
    lines.append('会直接驱动 MAA 核心跑（不用手动点开始），跑完自动切下一个账号。')

    payload = {'rows': rows, 'tasks': tl, 'formation': formation,
               'fightStages': fight_stages, 'profileHint': profile_hint,
               'start': start, 'maaDir': d, 'mode': mode}
    pid = 'b:' + uuid.uuid4().hex[:8]
    with _plans_lock:
        now = time.time()
        for k in [k for k, v in _plans.items() if now - v['at'] > PLAN_TTL]:
            _plans.pop(k, None)
        _plans[pid] = {'at': now, 'plan': payload}

    blocked = _guard(session, 'build_run', {'plan_id': pid}, '\n'.join(lines))
    if blocked:
        blocked['planId'] = pid
        return blocked
    return _start_build(payload)


def _start_build(payload):
    """真正执行：准备作业 → 直连 MaaCore 跑 → 日志在跑机面板看

    为什么不用「写配置让 MAA 自己跑」那套：MAA 的任务队列不认 Copilot 任务
    （见 maaplan 顶部注释），抄作业这条路只能自己驱动核心。
    """
    from . import maaplan

    rows = payload['rows']
    d = payload['maaDir']

    if not payload.get('start', True):
        # 用户说「配好先别跑」：只写 PRTS- 档案，不动模拟器。
        # 抄作业那份配不出来——Copilot 进不了 MAA 的任务队列（见模块注释），
        # 只能在真正开跑时由直连塞给核心，所以这里如实说明。
        wrote, failed = [], []
        for r in rows:
            try:
                res = maaplan.ensure_profile({
                    'tasks': payload['tasks'], 'stages': payload['fightStages'],
                    'accountName': r['accountName'], 'maaDir': d,
                    'profile': payload.get('profileHint') or r['name'],
                }, d)
                wrote.append(res['profile'])
            except Exception as e:
                failed.append('%s: %s' % (r['name'], e))
        out = {'started': False, 'profiles': wrote, 'maaDir': d,
               'note': '只写了配置档案，没启动模拟器。要开跑再调一次 build_run（start=true）。'}
        if 'copilot' in payload['tasks']:
            out['copilotNote'] = ('抄作业那部分没写进档案：MAA 的任务队列不支持 Copilot'
                                  '任务，开跑时才由本程序直接驱动核心。')
        if failed:
            out['failed'] = failed
        return out

    tid = tasks.create('挂机（%d 个账号）' % len(rows))

    def go():
        try:
            res = maaplan.run_plan(rows, {
                'tasks': payload['tasks'],
                'stages': payload['fightStages'],
                'copilot': {'formation': payload['formation']},
                'maaDir': d,
            }, tid)
            tasks.finish(tid, result=res)
        except Exception as e:
            tasks.finish(tid, error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': traceback.format_exc()})

    threading.Thread(target=go, daemon=True, name='agent-build-run').start()
    return {'started': True, 'taskId': tid,
            'profiles': len(rows),
            'note': '已经在后台跑了。进度看跑机面板，别在这里等它跑完。'}


def _confirm_text(plan):
    rows = [a for a in plan.get('accounts') or [] if a.get('ops')]
    head = '要给 %d 个账号挂 %d 关（共 %d 份作业）：' % (
        len(rows), plan.get('totalStages') or 0, plan.get('totalOps') or 0)
    lines = [head]
    for a in rows:
        lines.append('· %s（%s）→ %s' % (
            a['name'], a['accountName'],
            '、'.join(o['stageCode'] for o in a['ops'][:8])
            + ('…' if len(a['ops']) > 8 else '')))
    return '\n'.join(lines)


def _guard(session, action, args, summary):
    """写操作的闸门：autoApply 关着就停下来等用户点确认"""
    if settings.load().get('agent', {}).get('autoApply'):
        return None
    if session is None:
        return {'need_confirm': True, 'summary': summary,
                'note': '需要用户确认后才能写入'}
    session.pending = {'action': action, 'args': args, 'summary': summary}
    return {'need_confirm': True, 'summary': summary,
            'note': '已经把方案摆出来了，等用户在界面上点「确认执行」'}


def _tool_skill_wakeup(args, session=None):
    return {'started': True, 'taskId': skills.run_skill('wakeup', args),
            'note': '唤醒在后台跑（约 1 分钟），跑完自动回主界面。'}


def _tool_skill_navigate(args, session=None):
    return {'started': True, 'taskId': skills.run_skill('navigate', args),
            'note': '导航在后台跑，完成或失败都写进任务日志。'}


def _tool_skill_signin(args, session=None):
    return {'started': True, 'taskId': skills.run_skill('signin', args),
            'note': '签到在后台跑，日志里有每个号的结果。'}


def _precheck_fight(args):
    """开打前同步预检（几秒，只问作业站 + 比对账号干员）

    用户实测踩过的坑（2026-11-18）：助手直接挂 BB-S-1~4，账号练度不够，
    MAA 跑 8 分钟第一关就过不去，后面三关根本没解锁，用户只看到「0/1」。
    所以这里在**摆确认卡之前**就把结论拿到手：哪几关能打、哪几关打不了、
    为什么（缺练度 / 缺干员 / 没作业 / 前置没通），确认卡上也写清楚。

    返回 ``{'text', 'playable', 'blocked', 'rep'}``；预检本身出错就返回
    ``None``（不能因为作业站抽风就不让打）。
    """
    from . import stageplan
    from . import skills as sk
    stages = [s for s in (args.get('stages') or []) if s]
    if not stages:
        return None
    file = ''
    try:
        file = sk._account_file(args.get('account') or '')
    except Exception:                       # noqa: BLE001 没账号就当匿名预检
        file = ''
    try:
        auto = args.get('autoPrereq', 'auto')
        mode = args.get('mode') or 'copy'
        # 纯刷关默认不补前置（补上就变成每关打一遍，理智白花在已通关的关上）
        rep = stageplan.review(stages, file, mode=mode,
                               with_prereq=(auto is not False) and
                               (mode == 'copy' or auto is True),
                               fill_base=auto is True)
    except Exception as e:                  # noqa: BLE001
        return {'text': '预检没做成（%s），按原计划打' % e,
                'playable': stages, 'blocked': [], 'rep': None}
    lines = stageplan.details(rep)
    notes = [n for n in (rep.get('notes') or []) if n]
    text = '\n'.join(['预检：' + (rep.get('summary') or '')] + lines + notes)
    return {'text': text, 'playable': rep['playable'],
            'blocked': rep['blocked'], 'rep': rep}


def _train_what(args):
    """培养干员的确认卡文案：谁、每个升到什么样

    用户看到的必须是「泥岩（精二 / 技能7 / 技能2专3）」这种，而不是一串
    JSON —— 确认卡点下去就开跑，看错一个数就是几百理智白花。
    """
    from . import skills as sk
    try:
        rows = sk._train_rows(args)
    except Exception:
        rows = []
    if not rows:
        return '上次没做完的培养计划（resume）' if args.get('resume') else ''
    bits = []
    for r in rows:
        target = []
        e = sk._to_int(r.get('elite'))
        if e:
            target.append('精%s' % ('二' if e == 2 else '一'))
        sl = sk._to_int(r.get('skillLevel'))
        if sl:
            target.append('技能%d' % sl)
        try:
            arr = sk._norm_mastery(r.get('mastery')) or []
        except Exception:
            arr = []
        for i, lv in enumerate(list(arr)[:3]):
            if lv:
                target.append('技能%d专%d' % (i + 1, lv))
        bits.append('%s（%s）' % (r['name'], ' / '.join(target) or '目标没写'))
    return '、'.join(bits)


def _skill_via_guard(name, args, session):
    """fight / skip_story / weekly 共用：先摆确认卡，点了才跑后台任务

    确认重入的标记是 args 里的 _confirmed：pending 存的 args 带上它，用户
    点「确认执行」再进来时直接跑，不会再摆一次卡。

    自动战斗在摆卡**之前**先做一次预检（见 _precheck_fight），把「这号打
    不了哪几关」写进确认卡；一关都打不了就直接返回结果，不摆卡也不开打。
    """
    if args.get('_confirmed'):
        tid = skills.run_skill(name, {k: v for k, v in args.items()
                                      if not str(k).startswith('_')})
        out = {'started': True, 'taskId': tid,
               'note': '已在后台跑。任何一步失败会停并写明卡在哪关，'
                       '主动告诉用户卡在哪、该怎么处理。'}
        if args.get('_precheckNote'):
            out['precheck'] = args['_precheckNote']
        return out
    args = dict(args)
    desc = {'fight': '自动战斗', 'skip_story': '过剧情关',
            'weekly': '周目编排', 'train': '培养干员'}.get(name, name)
    if name == 'train':
        what = _train_what(args)
    else:
        raw = args.get('stages') or args.get('stage') or ''
        what = '、'.join(raw) if isinstance(raw, (list, tuple)) else str(raw)
    note = ''
    if name == 'fight' and not args.get('_prechecked'):
        chk = _precheck_fight(args)
        if chk:
            note = chk['text']
            if not chk['playable']:
                return {'error': '这号一关都打不了，没有排队',
                        'precheck': note,
                        'note': '把打不了的关和原因原样告诉用户，不要开打；'
                                '可以建议换作业（find_ops）或先补练度。'}
            args = dict(args, stages=chk['playable'], _prechecked=True,
                        _precheckNote=note)
            what = '、'.join(chk['playable'])
    summary = '%s：%s' % (desc, what)
    if note:
        summary += '\n' + note
    blocked = _guard(session, 'skill_' + name,
                     dict(args, _skill=name, _confirmed=True), summary)
    if blocked:
        blocked['skill'] = name
        if note:
            blocked['precheck'] = note
        return blocked
    return _skill_via_guard(name, dict(args, _confirmed=True), session)


def _tool_skill_fight(args, session=None):
    return _skill_via_guard('fight', args, session)


def _tool_skill_skip_story(args, session=None):
    return _skill_via_guard('skip_story', args, session)


def _tool_skill_weekly(args, session=None):
    return _skill_via_guard('weekly', args, session)


def _tool_skill_train(args, session=None):
    return _skill_via_guard('train', args, session)


_HANDLERS = {
    'list_accounts': _tool_list_accounts,
    'list_activities': _tool_list_activities,
    'search_stages': _tool_search_stages,
    'find_ops': _tool_find_ops,
    'op_detail': _tool_op_detail,
    'account_progress': _tool_account_progress,
    'run_status': _tool_run_status,
    'plan_multi_account': _tool_plan_multi,
    'maa_capabilities': _tool_maa_capabilities,
    'list_profiles': _tool_list_profiles,
    # skill：轻量的免确认，直接丢后台（失败自然有日志，用户看得到）
    'skill_wakeup': _tool_skill_wakeup,
    'skill_navigate': _tool_skill_navigate,
    'skill_signin': _tool_skill_signin,
}

_WRITE_HANDLERS = {
    'apply_plan': _tool_apply_plan,
    'build_run': _tool_build_run,
    'add_stages': _tool_add_stages,
    'stop_run': _tool_stop_run,
    # skill：会驱动 MAA 打关/过剧情，先摆出来等用户点头
    'skill_fight': _tool_skill_fight,
    'skill_skip_story': _tool_skill_skip_story,
    'skill_weekly': _tool_skill_weekly,
    'skill_train': _tool_skill_train,
}


PROBE_SYSTEM = """你在做 skill 联调测试：用户会给一句话，你只需要选出该调用哪个
工具、参数怎么填，然后直接调用。

规则：
- 严格按用户这句话的意图选工具，不要补充没提到的动作
- 用户说「保持在主页」「别唤醒」「不要导航」时，不要调 skill_wakeup / skill_weekly
- 关卡/活动代号尽量规整（MN / MN-1 / TL-5 / 活动代号）
- 拿不准也给一个最可能的调用，不要反问、不要解释

不要输出思考过程。"""


def try_skill(text, skill=None, run=False):
    """用大模型解析一句话：看它选哪个 skill、参数抽得准不准（联调测试用）

    ``skill`` 给了就只挂这一个工具（强制它用这个），但参数仍然由模型抽——
    这样既能测「会不会选对工具」，也能测「参数对不对」。
    ``run=True`` 时直接跑（跳过确认卡，测试就是要它立刻动），返回 taskId。
    """
    from . import skills as _skills

    text = (text or '').strip()
    if not text:
        raise ValueError('缺 text（要测的那句话）')
    name = (skill or '').strip()
    tools = [t for t in TOOLS if not name or t['function']['name'] == name]
    if not tools:
        raise ValueError('没有这个工具：%s' % name)
    msgs = [{'role': 'system', 'content': PROBE_SYSTEM},
            {'role': 'user', 'content': text}]
    said, calls, fin = _plain(msgs, tools)
    out = {'text': text, 'said': said, 'picked': '', 'args': {},
           'matched': None, 'taskId': '', 'note': ''}
    if not calls:
        out['note'] = ('模型没给工具调用：这句话可能没有可执行意图，'
                       '或者模型没按测试指令直接调')
        return out
    fn = (calls[0].get('function') or {})
    tname = fn.get('name') or ''
    try:
        args = json.loads(fn.get('arguments') or '{}')
    except ValueError:
        args = {}
    out['picked'] = tname
    out['args'] = args
    out['matched'] = (not name) or tname == name
    if name and tname != name:
        out['note'] = '指定的是 %s，模型却选了 %s' % (name, tname)
        return out
    if run:
        sk = tname[6:] if tname.startswith('skill_') else tname
        if sk not in _skills.SKILLS:
            out['note'] = '%s 不是能单独跑的 skill（只有 skill_* 开头的能跑）' % tname
            return out
        out['taskId'] = _skills.run_skill(sk, args)
    return out


def run_tool(name, args, session=None):
    """跑一个工具。任何异常都兜成 ``{'error': ...}`` 喂回模型，别让 loop 炸掉。"""
    try:
        if name in _WRITE_HANDLERS:
            return _WRITE_HANDLERS[name](args or {}, session)
        fn = _HANDLERS.get(name)
        if not fn:
            return {'error': '没有这个工具：%s' % name}
        return fn(args or {})
    except Exception as e:
        # 带 hint：这类是程序自己的 bug，原样重试只会拿到同一个异常。之前
        # list_accounts 报 KeyError 时模型连调两次，最后放弃并把锅甩给用户。
        return {'error': '%s: %s' % (type(e).__name__, e),
                'hint': '这是程序内部错误，原样重试会得到同样的结果。'
                        '换工具或让用户直接给信息，不要重复同一个调用。'}


SYSTEM = """你是 MAA-PRTS 的内置助手，帮用户管理明日方舟的挂机作业。

## 你手里有什么
只能通过工具做事。工具查不到的东西你就不知道，**绝对不要凭印象编造关卡号或作业 id**。
- 关卡关键词（TL-5、15-3、活动名）必须先 search_stages 换成真实关卡
- 作业 id 必须来自 find_ops 的返回
- 账号的 file 必须来自 list_accounts

## 挂机怎么配（这是你最该会的）

**用 build_run，别自己拼任务。**

四种 mode：
- `copy`：只抄作业（要关卡）→ 开始唤醒 → 自动战斗
- `fight`：只刷理智（要关卡，不抄作业）→ 开始唤醒 → 理智作战。
  用户说「刷点材料」「把理智清了」「打 1-7」就用它
- `daily`：只做日常（不要关卡）→ 开始唤醒 → 自动公招 → 基建换班 → 信用收支 → 领取奖励
- `daily_copy`：日常 + 抄作业（要关卡）

不是这四种就用 `tasks` 直接给顺序。可用值：
startup recruit infrast fight copilot mall award reclamation roguelike

**build_run 会做的事**（跟用户解释时照这个说）：
1. 新建一份 PRTS- 开头的配置档案（**用户自己的档案一个字段都不动**）
2. 按需要拼任务队列（关卡直接写进队列，MAA 内部自己导航过去，不是靠人找）
3. copy / daily_copy 时自动挑能打的作业，从作业站下下来写进作业集
4. **直接驱动 MAA 核心开跑**——不打开 MAA 界面，也不用任何人点开始；
   跑的过程中日志实时进跑机面板（第几个账号、换哪份作业、掉了什么、哪步失败）

所以绝对不要说「已经写入，请你去 MAA 里点开始」，那是旧版本的错说法。
用户说「先配好别跑」时传 `start=false`，那就只建档案、不碰模拟器。

## 常见对话怎么做

**「帮我把所有账号挂上 TL-5」**
1. list_accounts —— **先看 accountName（已绑定的手机尾号）**：非空的账号
   直接沿用，**绝对不要再问一遍**（用户 2026-11-18 报过这个：设置里配过
   尾号，助手还在问）
2. build_run（accounts=[]、mode=copy、keyword=TL-5）
3. 返回 need_info 时：**一次性把所有缺的账号列出来问**。官服只问
   「手机号后 4 位」（如 5505），B 服问游戏内用户名。
   **不要让用户去拼 `181****5505`**——格式是程序的事。用户给全号、
   带横线、星号多少、只给尾号，一律原样放进 names，程序会归一化。
   拿到后带 names 再调一次
4. 返回 need_confirm 时，把确认卡片上的内容用自己的话复述要点

**「清一下日常」/「帮我收菜」**
build_run（mode=daily，不用 keyword）。这就是一键长草。

**「刷点 1-7 的材料」**
build_run（mode=fight、keyword=1-7）。纯理智作战，不抄作业。

**「这周活动帮我打一下」**
list_activities 确认哪个 → build_run（mode=copy 或 daily_copy，keyword=活动前缀）

**「现在跑到哪了 / 为什么失败」**
run_status。state 是 stalled 就说卡在哪个节点，failed 就说原因。
想看跑到第几个作业，用返回里的 stage / index / total。

**「停下来」** → stop_run

## 挂关 skill（2026-11-17 新增）：把流程拆开一步一步来

七个 skill，各自独立跑后台、失败单独停：
- skill_wakeup：唤醒游戏到主界面（可按尾号切号）
- skill_navigate：导航到活动（老活动 / 当期 / EX 都行）。stage 优先传**中文
  活动名**（「孤星」），代号/活动 id 也行——内部统一换，别自己换算
- skill_skip_story：过剧情关（ST 关自动点开 + 跳剧情）
- skill_fight：自动战斗（编队 / 借助战 / 吃理智药）。**默认不唤醒不导航**，
  界面在哪就从哪直接开打；要全套（唤醒+导航）得用户明说或传 wakeup=true
- skill_signin：森空岛签到
- skill_weekly：**周目编排**——ST 剧情自动过、主线按序批量打、EX 收尾
- skill_train：**培养干员**——升精英化 / 技能等级 / 专精，可一次排多个。

**什么时候用 skill 而不是 build_run**：
- 用户要「从唤醒开始，剧情关也帮我过，一口气打完」→ skill_weekly。
  MN 这种：ST-1 → MN-1~7 → ST-2 → MN-8 → EX-1~8，skill_weekly 自己排，
  遇 ST 自动过剧情、连续战斗关合并成批。build_run 遇到剧情关会卡死，用不了。
- 只想导航到某活动看看 / 手动接管 → skill_navigate
- 游戏没开、卡登录页 → skill_wakeup
- **单独打一组关**（界面已在关卡列表 / 用户自己导航好了）→ skill_fight，
  不要再补导航；游戏没开或还在主界面才需要先 skill_wakeup / skill_navigate
- 签到 → skill_signin
- **培养干员**（升精二 / 技能 7 / 专精几技能）→ skill_train

**「帮我把泥岩精二、二技能专三」**
1. 先跟用户确认：哪个号、哪些干员、各自练度目标（别说「帮你练一下」就开跑）
2. skill_train（operators=[{name:"泥岩", elite:2, skillLevel:7, mastery:[0,3,0]}]）
3. 用户说「不用切号」就传 switchAccount=false；说「已经开着了」可传 wakeup=false
4. 中途材料不足会**暂停**并写明缺什么：把缺的材料和「补齐后说继续培养」告诉用户
5. 用户补完材料说「继续」→ skill_train（resume=true），不要让他重报练度

**「现在打到哪了 / 在干什么」**
任务日志里已经有 MAA 的实时进度（换作业 / 编队 / 部署 / 开技能 / 掉落 /
理智 / 培养结果 / 卡在第几步），直接念最近几条给用户，别让他自己盯 MAA 窗口。

skill 返回 taskId 后**不要傻等**：告诉用户已在后台跑，失败会停在哪一步
并写进日志。任务失败时主动提醒用户卡在哪个 skill、哪一关、什么原因。

**「帮我把 MN 从头打到尾」**
1. list_accounts（选账号）→ 跟用户确认「MN 全程，剧情也自动过？」
2. skill_weekly（stage=MN）→ 用户确认 → 后台跑
3. 中途失败：把失败的那关和原因说清楚，问用户是手动处理还是重试

**「我还差哪些关」**
account_progress。主线看 main，剿灭看 campaign，活动看 activities。不要说"我不知道"。
注意 mainKnown=false 表示森空岛这次没给主线进度（可能主线已全通，也可能没同步到），
**只能说"主线没有进度数据"，不能说"已推完"**。活动 left 是区域余量（含剧情/突袭），
报给用户时说"还差 N 个区"，别说成"N 个可抄的关"。

**「现在跑到哪了 / 为什么失败」**
run_status。state 是 stalled 就说卡在哪个节点，failed 就说原因。

**「推荐养哪些干员」**
find_ops 多查几个用户常打的关，统计哪些干员反复出现在作业里、
再对 account_progress 看该账号实际情况，给出优先级。说明这是按作业站使用频率推的。

## 硬规矩
- **打关先看预检，别硬挂**：skill_fight 开打前会按顺序补前置关、拿作业要求
  比对这号练度。返回里有 `precheck` 且列了「练度不足 / 缺干员 / 前置未通」时，
  **必须先把「哪几关打不了、为什么」原样告诉用户**（练度不够要点出差哪个干员），
  再说能打的几关已在后台跑。一关都打不了时工具会直接返回 error，别改口说"开打了"。
- **别把没解锁的关排进队列**：前一关没通，后面的关在地图上是灰的（用户实测：
  S1/S2 没打，S3 连解锁都不行，排下去只是白等 8 分钟）。用户点名要 S3 时，
  skill_fight 会自动把 S1、S2 补进计划；只想打指定的那关就传 autoPrereq=false，
  并在回话里说清「这关可能还没解锁」。
- **活动代号一律查，不许凭记忆换算**：用户说「孤星」就搜 search_stages("孤星")
  或直接把中文名交给 skill_navigate，**不要自己猜成 SV**（孤星=CW，
  覆潮之下=SV，愚人号=SN，不义之财=CV）。猜错一次就是几轮白跑。
- 中文活动名 → 代号走 search_stages；连活动名都拿不准就先 list_activities
  （返回正式名 + 代号前缀）让用户挑
- 写 MAA 之前必须让用户看到「给谁、跑哪些任务、挂哪几关」，等他确认
- skill_fight / skill_skip_story / skill_weekly / skill_train 同样要先确认再跑
  （工具会自动摆确认卡）
- 多账号缺切号名时，明确说「这个信息森空岛拿不到，得你告诉我」；
  官服只要**手机号后 4 位**（如 5505），不要要求用户拼星号。
  **问之前先看 list_accounts 的 accountName**：非空就是已经绑好的，
  直接用，别重复问（用户设置里配过的东西，助手再问一遍很烦）
- 多账号是**串行**的（一个模拟器同时只能登一个号），一个跑完自动切下一个
- **模拟器不用你操心**：每个 skill 开跑前会自动确认模拟器在不在、没开就替用户
  启动（路径从 MAA 配置里读）。任务报「环境没准备好」时，把日志那句话原样转给
  用户让他手动开，别自己反复重试、也别去改 MAA 的连接设置
- 不确定能配什么就先调 maa_capabilities，别猜
- 回答用中文，简洁，别写长篇。不要输出 JSON 给用户看。
- 涉及具体操作时，最后告诉用户去界面上哪看结果（跑机面板 / 左侧账号栏）
"""


# ---------------------------------------------------------------- 会话
class Session:
    def __init__(self, sid=None):
        self.id = sid or uuid.uuid4().hex[:12]
        self.messages = []
        self.events = []
        self.pending = None
        self.pending_call = None
        self.running = False
        self.thread = None
        self.created = time.time()
        self.error = ''
        # 事件序号单独计数：不能用 len(events)——裁剪旧事件后 len 会退回去，
        # 序号跟着重复，前端按 i 索引 DOM 就会张冠李戴
        self._seq = 0

    def push(self, kind, **kw):
        ev = {'i': self._seq, 'kind': kind, 't': time.time()}
        self._seq += 1
        ev.update(kw)
        self.events.append(ev)
        if len(self.events) > 600:
            del self.events[:200]
        return ev

    def view(self, since=0):
        """给前端：从 since 开始的事件

        流式输出中的那条一直要带上——它的 i 在第一片文字时就定了，之后只是
        原地累加 text。只按 i >= since 过滤的话，前端 since 一前进就再也拿不到
        后续文字，表现为「只显示第一段」。

        最后一条 say 也要一直带（不管 i）：流式刚一结束 ``live`` 就摘掉了，
        若前端在这一轮 poll 里已经越过它的 i（后续又 push 了 tool / done），
        它就再也不会被返回——表现是「回复最后一句被吃掉」，用户 2026-11-17
        截图反馈的正是这个。带一条的开销可以忽略。
        """
        since = int(since or 0)
        out = [e for e in self.events if e['i'] >= since or e.get('live')]
        for e in reversed(self.events):
            if e.get('kind') == 'say':
                if e not in out:
                    out.append(e)
                    out.sort(key=lambda x: x['i'])
                break
        return {'id': self.id, 'running': self.running, 'events': out,
                'next': self._seq, 'pending': self.pending,
                'error': self.error}


_sessions = {}
_sess_lock = threading.Lock()


def session(sid=None, create=True):
    with _sess_lock:
        s = _sessions.get(sid or '')
        if s is None and create:
            s = Session()
            _sessions[s.id] = s
            # 只留最近 20 个会话，聊天记录不值得常驻内存
            if len(_sessions) > 20:
                for k in sorted(_sessions, key=lambda x: _sessions[x].created)[:5]:
                    if not _sessions[k].running:
                        _sessions.pop(k, None)
        return s


def reset(sid):
    with _sess_lock:
        s = _sessions.get(sid)
    if s and not s.running:
        s.messages = []
        s.events = []
        s.pending = None
        s.error = ''
    return s.view(0) if s else None


def chat(sid, text):
    """用户发一句话，后台起 loop；前端轮询 view() 拿事件"""
    s = session(sid)
    if s.running:
        return {'ok': False, 'error': '上一条还在处理，等它说完'}
    text = str(text or '').strip()
    if not text:
        return {'ok': False, 'error': '空消息'}
    s.error = ''
    s.push('user', text=text)
    s.messages.append({'role': 'user', 'content': text})
    _trim(s)
    s.running = True
    s.thread = threading.Thread(target=_loop, args=(s,), daemon=True,
                                name='agent-chat')
    s.thread.start()
    return {'ok': True, 'session': s.id}


def confirm(sid):
    """用户点了「确认执行」：把待确认的动作跑掉，然后让模型继续"""
    s = session(sid, create=False)
    if not s:
        return {'ok': False, 'error': '会话不存在'}
    if s.running:
        return {'ok': False, 'error': '还在处理中'}
    if not s.pending:
        return {'ok': False, 'error': '没有待确认的操作'}
    s.running = True
    s.thread = threading.Thread(target=_run_pending, args=(s,), daemon=True,
                                name='agent-confirm')
    s.thread.start()
    return {'ok': True}


def cancel(sid):
    s = session(sid, create=False)
    if s:
        s.pending = None
        s.push('say', text='（已取消）')
    return {'ok': True}


def _trim(s):
    if len(s.messages) > KEEP_MESSAGES:
        # 保留第一条 user 和最近若干条，避免上下文无限长
        s.messages = s.messages[:1] + s.messages[-(KEEP_MESSAGES - 1):]


def _say_stream(s):
    """返回 (on_delta, close)：把模型吐的字边收边推到事件里

    同一个 i 原地累加 text（前端按 i 更新），所以这条事件要一直带给前端
    （``live`` 标记，见 Session.view）；一轮说完调 close() 摘掉标记。
    """
    holder = {'ev': None}

    def on_delta(piece):
        if holder['ev'] is None:
            holder['ev'] = s.push('say', text='', live=True)
        holder['ev']['text'] += piece

    def close():
        if holder['ev'] is not None:
            holder['ev'].pop('live', None)
            holder['ev'] = None

    return on_delta, close


def _loop(s):
    try:
        for _turn in range(MAX_TURNS):
            msgs = [{'role': 'system', 'content': SYSTEM}] + s.messages
            on_delta, close = _say_stream(s)
            try:
                content, calls, fin = _stream(msgs, TOOLS, on_delta)
            except LLMError:
                raise
            except Exception:
                # stream+tools 不被支持时退回非流式
                content, calls, fin = _plain(msgs, TOOLS)
                if content:
                    s.push('say', text=content)
            finally:
                close()
            # 撞 token 上限：模型没说完，得让用户知道并接着要（否则就是
            # 「回复半句就没了」，用户 2026-11-17 反馈的另一种截断）
            if fin == 'length':
                s.push('say', text='（这条回复撞到模型输出上限，被截断了。'
                                   '说「继续」我接着讲）')
            if not calls:
                s.messages.append({'role': 'assistant', 'content': content or ''})
                break
            s.messages.append({'role': 'assistant', 'content': content or '',
                               'tool_calls': calls})
            stop = False
            for call in calls:
                stop = _do_call(s, call) or stop
            if stop:
                break
        else:
            s.push('say', text='（说得有点久了，先停在这里。需要继续就说一声）')
    except LLMError as e:
        s.error = str(e)
        s.push('error', text=str(e))
    except Exception as e:
        s.error = '%s: %s' % (type(e).__name__, e)
        s.push('error', text=s.error)
    finally:
        s.running = False
        s.push('done')


def _do_call(s, call):
    """执行一个工具调用；返回 True 表示要停下来等用户确认"""
    name = (call.get('function') or {}).get('name') or ''
    raw = (call.get('function') or {}).get('arguments') or '{}'
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        args = {}
    s.push('tool', name=name, args=args)
    res = run_tool(name, args, s)
    if isinstance(res, dict) and res.get('need_confirm'):
        s.pending_call = call
        s.push('need_confirm', text=res.get('summary') or '',
               action=s.pending.get('action') if s.pending else '')
        return True
    s.messages.append({'role': 'tool', 'tool_call_id': call.get('id') or '',
                       'content': json.dumps(res, ensure_ascii=False)[:6000]})
    s.push('tool_result', name=name,
           text=json.dumps(res, ensure_ascii=False)[:1200])
    return False


def _run_pending(s):
    try:
        pend = s.pending or {}
        action, args = pend.get('action'), pend.get('args') or {}
        s.push('say', text='好，开始执行…')
        fn = _WRITE_HANDLERS.get(action)
        if not fn:
            s.push('error', text='不认识的操作：%s' % action)
            return
        # 这一步是用户点过确认的，绕过闸门直接执行
        old = settings.load().get('agent', {}).get('autoApply')
        try:
            settings.save({'agent': dict(settings.load().get('agent') or {},
                                         autoApply=True)})
            res = fn(args, None)
        finally:
            settings.save({'agent': dict(settings.load().get('agent') or {},
                                         autoApply=bool(old))})
        s.pending = None
        call = s.pending_call or {'id': 'confirm', 'function': {'name': action}}
        s.pending_call = None
        s.messages.append({'role': 'tool', 'tool_call_id': call.get('id') or '',
                           'content': json.dumps(res, ensure_ascii=False)[:6000]})
        s.push('tool_result', name=action,
               text=json.dumps(res, ensure_ascii=False)[:1200])
        _loop(s)
    except Exception as e:
        s.error = '%s: %s' % (type(e).__name__, e)
        s.push('error', text=s.error)
        s.running = False


def status():
    """设置页/面板判断能不能用"""
    cfg = config()
    return {'configured': bool(cfg['hasKey'] and cfg['model'] and cfg['baseUrl']),
            **cfg, 'sessions': len(_sessions)}
