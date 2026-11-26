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

from . import (accounts, acctbind, asstlog, copilot, maa, multiacct, progress,
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


def _sanitize(messages):
    """让每条 tool 消息都能对上前面的 assistant.tool_calls，坏掉的丢掉

    用户 2026-11-19 02:59 连着三次「还是失败了」都是同一个 400：
    ``Messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'``。原因是历史里出现了两种断链：

    - ``_trim`` 按条数裁剪，切点正好落在「assistant.tool_calls」和它的 tool
      结果中间，历史开头变成一条孤儿 tool；
    - 工具要用户确认时助手先回了 tool_calls，用户点「取消」/直接说下一句，
      那条 tool_call 永远没有响应。

    断一次就**每轮都断**（坏历史一直留在 s.messages 里），所以「老是存在
    这种报错」。发送前统一修一遍：孤儿 tool 丢弃，缺响应的 tool_call 补一句
    占位，保证序列永远合法。
    """
    out, pending = [], []
    for m in messages or []:
        role = m.get('role')
        if role == 'tool':
            cid = m.get('tool_call_id') or ''
            if cid in pending:
                pending.remove(cid)
                out.append(m)
            continue                        # 对不上的孤儿 tool 直接丢
        if pending:
            for cid in pending:
                out.append({'role': 'tool', 'tool_call_id': cid,
                            'content': '（这一步没有执行完）'})
            pending = []
        out.append(m)
        if role == 'assistant':
            for tc in (m.get('tool_calls') or []):
                cid = tc.get('id') or ''
                if cid:
                    pending.append(cid)
    for cid in pending:
        out.append({'role': 'tool', 'tool_call_id': cid,
                    'content': '（这一步没有执行完）'})
    return out


def _payload(messages, tools=None, stream=True):
    st = settings.load().get('agent') or {}
    model = st.get('model') or ''
    if not model:
        raise LLMError('还没设置模型名（设置 → 内置助手）')
    body = {'model': model, 'messages': _sanitize(messages), 'temperature': 0.2,
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
    low = (text or '').lower()
    if code == 400 and 'tool' in low and 'role' in low:
        # 专属解释：不是模型不支持工具调用，是历史里的工具调用记录断链了
        # （发送前已由 _sanitize 修好），再说一次就能继续
        return '上下文里的工具调用记录断了（这条已自动修复），把刚才那句再说一遍就行'
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
          '**任何关卡号都必须先过这一步**，不要凭印象写 stageCode。'
          '用户说的是模组/干员/玩法这类**不是关卡名**的词（「结城理模组」）时'
          '这里会返回 0 关——别就此说「没有作业」，改用 find_ops：它会走作业站'
          '全文搜，把带这个词的作业连同关卡一起列出来。',
          {'keyword': _STAGE}, ['keyword']),
    _tool('find_ops',
          '查这些关卡在作业站上有哪些作业，按该账号练度分档。'
          '返回每关最优几份（copilotId / 星级 / 能否直接抄）。'
          '**关键词不是关卡名也能用**（「结城理模组」这种）：会退回作业站全文搜，'
          '跨关卡把相关的作业列出来，每行都带关卡代号。'
          '要锁定某一关就在关键词后面带上关卡代号（「结城理模组 4-3」），'
          '作业站那边是「关键词 + 关卡」强制过滤，基本必出。',
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
                                    '（如 5505），前 3 位 + 后 4 位也行'
                                    '（如 157****3635）；B 服填游戏内用户名。'
                                    '缺的时候要问用户，**原样传**，别自己拼格式'},
           'limitStages': {'type': 'integer', 'description': '最多挂几关，默认 8'},
           'raid': {'type': 'string', 'enum': ['normal', 'raid', 'both'],
                    'description': '抄作业的难度，**照用户原话传**：说「突袭」→ '
                                   '必须显式传 raid；说「普通」→ normal；说'
                                   '「都要」→ both。**省略 = 只打普通**。'
                                   '要突袭却漏了这个参数，排下去的就是普通'
                                   '（2026-11-23 用户报过这个）'},
           'profile': {'type': 'string',
                       'description': '档案名后缀，比如「活动-时间啊向前」。不给就自动起名'},
           'start': {'type': 'boolean', 'description': '写完是否立刻启动，默认 true'}},
          []),
    _tool('set_account_name',
          '**配置账号登录尾号**：用户说出某个账号的手机号尾号（后 4 位，或'
          '「前 3 位 + 后 4 位」如 157****3635）时，把它存进那个账号的设置'
          '（设置 → MAA 路径 → 切号名），下次挂机、切号都不用再问。'
          '只改本程序自己的账号配置，**不会动 MAA 的任何字段**。'
          '用户只是顺口报了个尾号、还没说要挂什么时也可以先存下来。',
          {'account': _ACC,
           'accountName': {'type': 'string',
                           'description': '用户给的尾号/切号名，**原样传**，别自己拼格式'}},
          ['accountName']),
    _tool('bind_emulator',
          '**把账号钉到某台模拟器**：用户说「这个号用第二台 / 用 5556 / 别和'
          '那个号挤一台」时调用。存进那个账号的设置（设置 → 账号 → 模拟器），'
          '以后每次跑机都连这一台，不靠聊天里临时指定——助手忘一次就不会串。'
          '只改本程序自己的账号配置，不动 MAA。',
          {'account': _ACC,
           'address': {'type': 'string',
                       'description': 'adb 地址，如 emulator-5556 或 127.0.0.1:16384'}},
          ['account', 'address']),
    _tool('list_profiles',
          '看 MAA 里现在有哪些配置档案、当前用的是哪个、各自挂了几个作业。'
          '用户问「现在跑的是哪个号」时用它。'),
    _tool('stop_run',
          '停挂机。**指定账号就只停那一个号**（account 传昵称或文件名，'
          '如「示例昵称#0384」「b服那个」），不传才全部停。用户说'
          '「停 b 服」「停那个号」时必须带 account，别把别人正在打的关一起砍了。',
          {'account': {'type': 'string',
                       'description': '要停哪个账号（昵称 / 文件名都行）；'
                                      '留空=全停'},
           'maa_dir': {'type': 'string', 'description': '留空用默认目录'}}),
    _tool('add_stages',
          '给**一个**账号挂一组关卡的快捷方式（等价于 mode=copy 的 build_run）。',
          {'account': _ACC, 'keyword': _STAGE,
           'raid': {'type': 'string', 'enum': ['normal', 'raid', 'both'],
                    'description': '难度，**照用户原话传**：说「突袭」→ raid，'
                                   '「普通」→ normal，「都要」→ both；省略=只写'
                                   '普通行。要突袭却没有突袭作业的关会被跳过并'
                                   '在 raidMissing 里列出来'},
           'start': {'type': 'boolean', 'description': '写完是否立刻启动，默认 true'}},
          ['keyword']),
    # ---- 挂关 skill 化（2026-11-17）：唤醒 / 导航 / 剧情 / 战斗 / 签到 / 周目 ----
    _tool('skill_wakeup',
          '唤醒游戏到主界面（游戏没开/停在登录页/START 页时用）。'
          '可选 accountName 按切号名换账号（官服=手机号后 4 位，如 5505；'
          '也可以给前 3 位 + 后 4 位，如 157****3635）。'
          '返回 taskId，后台跑。',
          {'account': _ACC,
           'accountName': {'type': 'string',
                           'description': '切号名；不给就唤醒当前账号'}}),
    _tool('skill_navigate',
          '把游戏导航到指定活动（老活动走曲谱，当期活动走终端页的活动提示条；'
          'EX / S / MO 关会自动切到活动地图里的对应分区）。'
          'stage 可以传**中文活动名（推荐，如「孤星」「愚人号」）**、关卡号'
          '（MN-1 / TL-5 / XX-EX-1）、活动 id（act25side），也可以直接传'
          '「最新活动 / 当期活动」。**主线关也走它**（15-3 / 0-11 / H15-1 都行，'
          '会走「终端 → 曲谱 → 乐章收录 → 章节卡 → 前往章节」那条路）。'
          '**主线 9~14 章要切作战环境时把 difficulty 一起传**（磨难/剧情/标准），'
          '导航到位后顺手切档——不要「先导航、再调 skill_difficulty」两步走，'
          '助手老是忘第二步，用户看到的就是「导航了但没切磨难」（2026-11-25）。'
          '内部会自己换成真实关卡代号。'
          '**别自己凭记忆把中文名换算成代号**（孤星是 CW，不是 SV）。'
          '**游戏没开不用先调 skill_wakeup**：它一进场发现进程不在就自己唤醒'
          '（有切号名走 MAA 开始唤醒），唤醒完再导航；只有用户要求切号换号时'
          '才单独唤醒。返回 taskId。',
          {'stage': _STAGE,
           'activity': {'type': 'string',
                        'description': '活动中文名（如「孤星」「愚人号」）；'
                                       '和 stage 二选一，中文名最不容易错'},
           'difficulty': {'type': 'string', 'enum': ['story', 'normal', 'hard'],
                          'description': '主线 9~14 章的作战环境：剧情体验(story) '
                                         '/ 标准实战(normal) / 磨难险地(hard)。'
                                         '导航到位后自动切；0~8 / 15~17 章没有'
                                         '这条栏，别传'},
           'account': _ACC}, []),
    _tool('skill_difficulty',
          '切主线第 9~14 章的「作战环境」（剧情体验 / 标准实战 / 磨难险地）。'
          '第 9 章只有剧情 / 标准两档，第 10~14 章三档；0~8 章和 15~17 章没有'
          '这条栏（那几章只有普通 / 突袭）。给了 stage 就先导航到那一章的关卡'
          '列表再切；不给就按「界面已经停在目标章节的关卡列表上」处理。'
          '**「导航过去顺便切档」时别用它**——直接给 skill_navigate 传 difficulty，'
          '一次调用搞定。它适合「人已经在那一章、只想换档」或者用户明确要单独'
          '切一次的时候。返回 taskId；同一个号还有任务在跑时会自动排队等它。',
          {'difficulty': {'type': 'string', 'enum': ['story', 'normal', 'hard'],
                          'description': '剧情体验环境(story) / 标准实战环境'
                                         '(normal) / 磨难险地环境(hard)'},
           'stage': {'type': 'string',
                     'description': '关卡代号（10-3 / 12-1）：给了就先导航到'
                                    '那一章的关卡列表再切'},
           'account': _ACC}, ['difficulty']),
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
          '**默认补唤醒+导航**（传 wakeup=true）：用户点名了账号、又没说'
          '「不用切号 / 已经开着这个号」时，必须传 wakeup=true——先唤醒'
          '（MAA 的开始唤醒，按该账号档案切号）→ 导航到这组关 → 开打。'
          '只有他明说「直接跑 / 已经在关卡列表 / 别切号」才直跑'
          '（assumeStageList=true 或 noSwitch=true）。'
          '**调用前先跟用户确认要打哪些关**。'
          '**难度必须显式说**：不传 raid、也不带 @ 后缀 = **只打普通**（跟着'
          '用户原话走，别自动升级）。要突袭就写 raid="raid" 或 '
          'stages=["SV-EX-1@raid"]；普通还没通关、要一次打通写 @both。'
          '用户说「打突袭」时**漏了这两个字段，实际排下去的就是普通**——'
          '2026-11-23 用户报「说突袭、两个号导入的都是普通 EX-8」就是这么来的。'
          '**用户点名某份作业（「用 41624」「换成令的单核」）时必须用 ops 传 '
          'copilotId**，光说"已经换了"没用——不传 ops 就会重新搜索挑回同一份。'
          '每关会自动准备 2~3 份备选作业：某一关打不过（比如只打 2 星）就'
          '**先回关卡菜单、再只换那一关的作业重挂**，后面的关照原样继续；'
          '这关没有别的作业就同一份再试一次。前面已打完的关不会重打。'
          '翻车重挂是自动的，别让用户自己再喊一遍。'
          '**日志只报「跑到哪一关」和任务链成败**——MAA 不提供逐关结果，'
          '别对用户说「成功几关」。'
          '返回 taskId。',
          {'stages': {'type': 'array', 'items': {'type': 'string'},
                      'description': '关卡代号，可加 @ 后缀逐关指定难度：'
                                     '"SV-EX-1@raid" 只打突袭、'
                                     '"SV-EX-1@both" 普通+突袭、'
                                     '"SV-EX-1@normal" 只打普通。**不带后缀'
                                     '就走 raid 参数，而 raid 不传只打普通**：'
                                     '用户说突袭就必须写 @raid 或传 raid="raid"'},
           'mode': {'type': 'string', 'enum': ['copy', 'fight']},
           'wakeup': {'type': 'boolean',
                      'description': 'true=先唤醒（按账号档案切号）再导航到这组关，'
                                     '然后开打。**点名了账号、用户没说「不用切号/'
                                     '已经在关卡列表」时必须传 true**；只有用户明确'
                                     '要求直跑才省略'},
           'autoPrereq': {'type': 'boolean',
                          'description': '默认 false=不补前置，点名的关就是全部。'
                                         'true=把前置关也补进计划（补进来的只打普通）'
                                         '。用户说「S-3 点不开/没解锁，把前置一起补」'
                                         '时才传 true，别自己默认补'},
           'difficulty': {'type': 'string',
                          'enum': ['story', 'normal', 'hard'],
                          'description': '主线 9~14 章的作战环境：剧情体验(story) '
                                         '/ 标准实战(normal) / 磨难险地(hard)。'
                                         '**这几章必须传**——用户没说就传 normal '
                                         '并在回复里说明「按标准实战环境打」；'
                                         '用户说磨难就传 hard。程序会在导航到那'
                                         '一章的关卡列表后自动切，切不过去会直接'
                                         '报错（不会闷头打错档）。0~8 章和 15~17 '
                                         '章没有这条栏，传了不起作用'},
           'raid': {'type': 'string', 'enum': ['normal', 'raid', 'both'],
                    'description': '难度档位，**照用户原话传**：'
                                   '用户说「突袭/突袭模式」→ 必须显式传 raid；'
                                   '说「普通」→ normal；说「普通和突袭都打」'
                                   '→ both。**省略 = 只打普通**（不是突袭，'
                                   '也不是自动）。**绝不许因为「EX 关」「复刻'
                                   '活动」「一般都要打突袭」之类理由替用户升级'
                                   '或降级**——2026-11-23 用户投诉过一次说普通'
                                   '排成突袭、一次说突袭结果排成普通。'
                                   '主线磨难/险地不是突袭，不受影响'},
           'ops': {'type': 'array',
                   'description': '指定用哪一份作业（用户点名「用 41624 那份」'
                                  '「换成令的单核」时**必须**传，否则每次都重新'
                                  '搜索、挑回评分最高的同一份，用户会看到「换了'
                                  '作业没生效」）。copilotId 从 find_ops 拿',
                   'items': {'type': 'object',
                             'properties': {
                                 'stage': {'type': 'string',
                                           'description': '关卡代号'},
                                 'copilotId': {'type': 'integer',
                                               'description': '作业 id'},
                                 'raid': {'type': 'string',
                                          'enum': ['raid', 'normal', 'both'],
                                          'description': '这关打哪档；不写就按'
                                                         ' skill_fight 的 raid '
                                                         '参数（默认只打普通）'}},
                             'required': ['stage', 'copilotId']}},
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
                           'description': '切号名（官服=手机号后 4 位，或前 3 位 + 后 4 位）。'
                                          '账号已绑定尾号（list_accounts 的 '
                                          'accountName 非空）时不用传，程序会自己用'},
           'switchAccount': {'type': 'boolean',
                             'description': 'false=只唤醒、不切号（用户说不用换号时传）'},
           'wakeup': {'type': 'boolean',
                      'description': 'false=不唤醒，直接从当前界面开始（游戏得开着）'},
           'resume': {'type': 'boolean',
                      'description': 'true=接着上次没做完的培养计划'}},
          ['operators']),
    _tool('skill_sweep',
          '**一键扫图**：把一个活动按「普通关 → EX 普通 → EX 突袭 → S 关」'
          '逐段挂完。段与段之间会自己重新导航（EX / S 是活动地图里不同的分区，'
          '不导航 MAA 认不出关卡名）。**S 段只打普通**——活动 S 突袭没有奖励。'
          '每关打不过会自动回关卡菜单、只换那一关'
          '的作业重挂（没候选就同一份再试），日志报「跑到哪几关 + 卡在哪 + 为什么」。'
          '用户说「把这个活动从头打到尾」「EX 和 S 都帮我清了」时用它。'
          '**调用前先跟用户确认：哪个活动、哪个号、要不要打突袭**。返回 taskId。',
          {'stage': _STAGE,
           'account': _ACC,
           'tiers': {'type': 'array', 'items': {'type': 'string'},
                     'description': '要扫哪几段，默认 base/ex/s；只想清 EX 和 S '
                                    '就传 ["ex","s"]'},
           'raid': {'type': 'string', 'enum': ['both', 'raid', 'normal'],
                    'description': 'EX 段怎么打：both（默认）=先普通、再导航过去'
                                   '打突袭；raid=只打突袭；normal=只打普通'},
           'wakeup': {'type': 'boolean',
                      'description': '默认 true（先唤醒到主界面）；游戏已经开着'
                                     '并停在关卡列表时传 false'},
           'accountName': {'type': 'string',
                           'description': '切号名（官服=手机号后 4 位，或前 3 位 + 后 4 位）'}},
          ['stage']),
    _tool('skill_tool',
          '**MAA 小工具（牛杂）**：跑 MAA「小工具 → 牛杂」里的功能，分两类：\n'
          '- **活动里的入口**（会先导航到当期活动地图再点进去）：\n'
          '  `name="wartime_press"` 战时新闻委员会（TL 的小游戏，第一次要手动过教程）\n'
          '  `name="os_rd"` 喀兰贸易技术研发部（OS 的小游戏，第一次要手动过引导）\n'
          '  `name="event_shop"` 活动商店（按黑名单买，不碰无限池）\n'
          '- **常驻商店**（不用导航，人在主界面就行，任务自己走到凭证交易所）：\n'
          '  `name="green_shop"` 绿票商店——默认一层全买、二层只买寻访凭证和招聘许可\n'
          '  `name="yellow_shop"` 黄票商店——默认花 258 黄票买 38 抽\n'
          '用户说「牛杂里的绿票商店 / 黄票商店」就是这两项，**别去调活动商店**。\n'
          '**黄票商店是高危**（花 258 黄票换 38 抽，不可撤销）：程序一定会先弹'
          '确认卡，用户点了才跑；把卡上的内容复述给用户，别自己替他确认。\n'
          '绿票 / 黄票都从主界面跑，不需要先导航到活动地图。返回 taskId。',
          {'name': {'type': 'string',
                    'enum': ['wartime_press', 'os_rd', 'event_shop',
                             'green_shop', 'yellow_shop'],
                    'description': 'wartime_press=战时新闻委员会；'
                                   'os_rd=喀兰贸易技术研发部；event_shop=活动商店；'
                                   'green_shop=绿票商店；yellow_shop=黄票商店（高危）'},
           'account': _ACC,
           'blacklist': {'type': 'array', 'items': {'type': 'string'},
                         'description': '活动商店黑名单（商品名，子串匹配）。'
                                        '不传就用默认四个；**别传空数组**，'
                                        '空着 MAA 会把无限池也买掉'},
           'prefix': {'type': 'string',
                      'description': '活动代号（OS / TL…），默认按工具自己登记的那个'},
           'wakeup': {'type': 'boolean',
                      'description': 'true=先唤醒并按尾号切号再跑。多账号批量时程序'
                                     '会自动补；单账号、或用户说「不用切号」时不要传'},
           'accountName': {'type': 'string',
                           'description': '切号名（官服=手机号后 4 位，或前 3 位 + 后 4 位；'
                                          'B 服=游戏内用户名）。不传就用账号里存的'},
           'confirm': {'type': 'boolean',
                       'description': '只有黄票商店认这个：用户已明确同意花 258 黄票'
                                      '才传 true（一般不用传——助手这边是先弹确认卡）'}},
          ['name']),
    _tool('skill_claim',
          '**领取活动奖励**：导航到活动奖励页（OS 是「神谕谜题」），点一次'
          '「一键领取」，把已完成的任务奖励收掉。纯 adb 操作，不走 MAA 任务。'
          '用户说「把活动奖励领了 / 领一下神谕谜题」时用它。'
          '**领完要顺手搬商店的话**，用 run_steps 把 claim + tool(event_shop) '
          '串起来，别让用户自己再喊一次。返回 taskId。',
          {'account': _ACC,
           'prefix': {'type': 'string',
                      'description': '活动代号（OS…），默认当期活动'},
           'wakeup': {'type': 'boolean',
                      'description': 'true=先唤醒再领（游戏没开/停在登录页时用）'}}),
    _tool('run_steps',
          '**按账号批量跑一组 skill（推荐用来做「活动一条龙」）**。把一个账号'
          '要做的几步按顺序排好，程序自己决定怎么分配模拟器：\n'
          '- 几个号绑的是**同一台模拟器** → 排队：一个号做完所有步骤，切下一个号\n'
          '- 几个号绑的是**不同模拟器** → 多开并行，各跑各的\n'
          '用户说「把这 5 个号的 OS 活动挂一下（普通/突袭/S/领奖/商店）」时用它。\n'
          '每步形如 {"skill":"sweep","stage":"OS","tiers":["base","ex","s"],'
          '"raid":"normal"}、{"skill":"claim","prefix":"OS"}、'
          '{"skill":"tool","name":"event_shop","prefix":"OS"}、'
          '{"skill":"tool","name":"green_shop"}。'
          '可用 skill：wakeup navigate fight skip_story sweep tool claim '
          'weekly train signin。**steps 里不用写 account**（程序按账号循环）。\n'
          '**两个以上账号时，每个号开跑前程序会自动补一步「开始唤醒」切号**'
          '（第一步本身带唤醒的 sweep/weekly/train 不重复补），别自己再写 '
          'wakeup 步骤；用户明说不用切号才传 switchAccount=false。\n'
          '步骤里要是有**黄票商店**（花 258 黄票换 38 抽），整批会先弹一次确认卡，'
          '用户点了才开跑；这一步的钱是按账号算的，卡上会写清哪些号要花。'
          '**调用前先跟用户确认：哪些号、按什么顺序做哪几步**。',
          {'accounts': {'type': 'array', 'items': {'type': 'string'},
                        'description': '账号文件名列表；空数组 = 当前选中的账号'},
           'steps': {'type': 'array',
                     'description': '按顺序执行的 skill 步骤，每步至少要有 skill',
                     'items': {'type': 'object',
                               'properties': {
                                   'skill': {'type': 'string',
                                             'description': 'wakeup / navigate / '
                                                            'fight / skip_story / '
                                                            'sweep / tool / claim / '
                                                            'weekly / train / signin'},
                                   'assumeStageList': {
                                       'type': 'boolean',
                                       'description': '仅 fight：用户明确说'
                                                      '「我已经在关卡列表了」'
                                                      '才传 true（直跑，不导航）'},
                                   'noSwitch': {
                                       'type': 'boolean',
                                       'description': '仅 fight：用户明确说'
                                                      '「不要切号，直接开始」'
                                                      '才传 true（直跑，不导航）'},
                               },
                               'required': ['skill']}},
           'names': {'type': 'object',
                     'description': '{账号文件名: 切号名}（缺的才要问用户）'},
           'emulators': {'type': 'object',
                         'description': '{账号文件名或昵称: adb 地址}，'
                                        '如 {"abc示例#0384 bilibili服": '
                                        '"emulator-5556"}。**只覆盖这一次**；'
                                        '用户说「这个号固定用第二台」时应该先'
                                        '调 bind_emulator 钉进账号，别指望每次'
                                        '都记得传这个参数'},
           'switchAccount': {
               'type': 'boolean',
               'description': '多账号时每个号开跑前要不要「开始唤醒」切号。'
                              '**默认 true，别传**；只有用户明说「不用切号 / '
                              '都是同一个号 / 已经切好了」才传 false'}},
          ['steps']),
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


def _variant_arg(raw):
    """raid 参数归一：``'raid'/'突袭'`` → raid，``'both'`` → both，
    ``'normal'`` / 空 → normal / 不筛

    用户说的是中文还是英文都收，别让「突袭」两个字在参数里变成普通。
    """
    v = str(raw or '').strip().lower()
    if v in ('raid', '突袭', 'only', 'onlyraid'):
        return 'raid'
    if v in ('both', 'all', '都要'):
        return 'both'
    if v:
        return 'normal'
    return ''


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
    try:
        last = settings.load().get('lastAccount') or ''
    except Exception:
        last = ''
    for c in accounts.list_accounts():
        if (c.get('game') or 'arknights') != 'arknights':
            continue
        # 绑了配置文件的账号：列表里的切号名要用生效值（档案里的那个），
        # 否则用户在设置里选过档案、模型还是看到空值，照样重复问一遍
        acc_name = c.get('maaAccountName') or ''
        if c.get('maaProfile'):
            try:
                acc_name, _src = acctbind.resolve(c['file'])
            except Exception:
                pass
        d = ''
        try:
            d = acctbind.dir_of(c['file'])
        except Exception:
            d = ''
        addr = ''
        pinned = ''
        try:
            pinned = acctbind.addr_of(c['file'])
        except Exception:
            pinned = ''
        try:
            from . import maaplan
            addr = pinned or (maaplan._connection(d, acctbind.profile_of(c['file'])
                                                  or None).get('address') or '')
        except Exception:
            addr = pinned
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
            'accountName': acc_name,
            # 用户此刻在左侧点开的账号（settings.lastAccount）。用户没点名
            # 账号时，**默认就问这个号**，别再反问「你说的是哪个账号」
            'current': bool(last) and c['file'] == last,
            # 多开要看：各自的 MAA 目录和模拟器地址。同一个 emulator 的号会
            # 排队切号，不同的会同时跑（见 skills.run_batch / maaplan.run_plan）
            'maaDir': d,
            'emulator': addr,
            # true = 这个号在设置里钉过模拟器（钉过的地址绝不自动换，
            # 没钉的才可能按档案 / 在线设备去猜）
            'emulatorPinned': bool(pinned),
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
    cur = next((c['file'] for c in out if c['current']), '')
    emus = sorted({c['emulator'] for c in out if c['emulator']})
    # 实际在线的设备（adb devices）：它才是「现在有几台模拟器能同时跑」的真相。
    # 账号绑的档案可能全指向同一台，而用户其实开着两三台——这时助手要能问
    # 「哪个号用哪台」，再通过 run_steps 的 emulators 传进来。
    devs = []
    try:
        devs = skills.online_devices()
    except Exception:
        devs = []
    return {'accounts': out, 'count': len(out),
            'current': cur,
            'emulators': emus,
            'devices': devs,
            'hint': 'accountName 就是已绑定的切号名（手机号后 4 位 / 前 3 位 + 后 4 位）'
                    '：**非空的直接按它跑，不要再问用户**；只有为空的账号才问'
                    '「手机号后 4 位」（B 服问游戏内用户名）。用户报了尾号就顺手用'
                    ' set_account_name 存下来，下次不用再问。'
                    '**用户没点名账号时用 current 那个**（他刚在左侧点开的）；'
                    'current 为空才用第一个。emulator 相同 = 同一台模拟器，'
                    '那几个号只能排队切号；不同 = 可以多开同时跑。'
                    'devices 是现在 adb 上真在线的设备：emulator 值和它对不上、'
                    '又说要「两台一起跑」时，问用户哪个号用哪台，再在 run_steps 的'
                    ' emulators 里指定（MAA 里八份档案常常全指向同一台）'}


def _tool_set_account_name(args):
    """配置账号登录尾号：把用户报的尾号存进对应账号的切号名

    只写本程序自己的账号 json（``maa.accountName`` + ``accountNameFrom=manual``），
    MAA 的配置文件一个字段都不动。存下来之后 list_accounts 就能看到，
    下次挂机 / 切号不用再问。
    """
    raw = str(args.get('accountName') or args.get('tail') or '').strip()
    if not raw:
        return {'error': '没给尾号（后 4 位，或前 3 位 + 后 4 位）'}
    rows = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    if not rows:
        return {'error': '左侧还没有明日方舟账号'}
    want = str(args.get('account') or '').strip()
    file = ''
    if want:
        for c in rows:
            names = (c['file'], c.get('nickName') or '', c.get('alias') or '')
            if want in names or want in (c.get('nickName') or ''):
                file = c['file']
                break
        if not file:
            return {'error': '找不到账号：%s' % want,
                    'accounts': [{'file': c['file'], 'name': c.get('nickName')}
                                 for c in rows],
                    'hint': '按 list_accounts 的 file 或昵称再传一次'}
    elif len(rows) == 1:
        file = rows[0]['file']
    else:
        return {'need_info': 'account',
                'accounts': [{'file': c['file'], 'name': c.get('nickName')}
                             for c in rows],
                'note': '有多个账号，问用户这个尾号是哪个号的（按昵称确认）'}

    nm, why = acctbind.bind(file, raw)
    if not nm:
        return {'error': why, 'hint': '官服要手机号（后 4 位，或前 3 位 + 后 4 位）；'
                                      'B 服填游戏内用户名'}
    row = next((c for c in rows if c['file'] == file), {})
    return {'ok': True, 'account': file, 'name': row.get('nickName') or file,
            'accountName': nm,
            'note': '已存进「设置 → MAA 路径 → %s 的切号名」，下次挂机直接用；'
                    '没改 MAA 里任何配置' % (row.get('nickName') or file)}


def _tool_bind_emulator(args):
    """把账号钉到某台模拟器：写账号 json 的 ``maa.adbAddress``

    为什么要有这一步（2026-11-23 用户报「又忘了实例控制」）：之前「哪个号
    用哪台」只在聊天里说一次，助手下一轮就忘了，两个号挤一台；MAA 里八份
    档案又全写 ``emulator-5554``，档案本身分不出来。钉进账号以后，任何跑机
    入口（skill / run_steps / 界面按钮）都读同一份，忘不忘都不影响。
    """
    addr, why = acctbind.check_addr(args.get('address') or '')
    if why:
        return {'error': why}
    if not addr:
        return {'error': '没给模拟器地址（emulator-5554 / 127.0.0.1:16384）'}
    rows = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    if not rows:
        return {'error': '左侧还没有明日方舟账号'}
    want = str(args.get('account') or '').strip()
    file = ''
    if want:
        for c in rows:
            if want in (c['file'], c.get('nickName') or '', c.get('alias') or ''):
                file = c['file']
                break
        if not file:
            return {'error': '找不到账号：%s' % want,
                    'accounts': [{'file': c['file'], 'name': c.get('nickName')}
                                 for c in rows],
                    'hint': '按 list_accounts 的 file 或昵称再传一次'}
    elif len(rows) == 1:
        file = rows[0]['file']
    else:
        return {'need_info': 'account',
                'accounts': [{'file': c['file'], 'name': c.get('nickName')}
                             for c in rows],
                'note': '有多个账号，问用户这个模拟器是给哪个号的'}
    accounts.save_maa_meta(file, {'adbAddress': addr})
    row = next((c for c in rows if c['file'] == file), {})
    return {'ok': True, 'account': file,
            'name': row.get('nickName') or file, 'address': addr,
            'note': '已钉进「设置 → 账号 → %s 的模拟器」，以后跑这个号只连 %s；'
                    '没改 MAA 里任何配置' % (row.get('nickName') or file, addr)}


def _tool_list_activities(args):
    n = max(1, min(40, int(args.get('limit') or 12)))
    rows = resources.activity_list(n) or []
    out = {'activities': [{'name': r.get('name'), 'prefix': r.get('prefix'),
                           'stages': r.get('count') or r.get('stages'),
                           'reprint': bool(r.get('reprint'))}
                          for r in rows]}
    # 当期活动（我们写过导航的那几个，带有效期）：用户说「最新活动」时用
    # 这里的 prefix，别自己猜代号（2026-11-19 用户要求）
    try:
        from . import eventnav
        cur = eventnav.active()
        if cur:
            out['current'] = cur
            out['currentHint'] = ('current 是**当期还在有效期**的活动（我们写过'
                                  '导航），用户说「最新活动 / 当期活动」就用它的 '
                                  'stage（如 OS-1），EX / S 关分别是 OS-EX-1 / '
                                  'OS-S-1。reprint=true 的是**复刻活动**：用户要刷'
                                  '它的 EX / S 时，先问「要不要连突袭一起打」和'
                                  '「去年打过吗」——去年打过的话突袭没有首通奖励，'
                                  '按用户的说法传 raid；**这条只影响排单难度，'
                                  '不代表某关没有突袭，也不许据此推断**。'
                                  'S 关和 EX 关一样都有普通+突袭两档')
    except Exception:
        pass
    return out


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
    elif res.get('mode') == 'unknown':
        # 「结城理模组」这类不是关卡名的词：这里查不到关是正常的，
        # 作业站有全文搜，得让模型换 find_ops 而不是回一句「没有作业」
        out['note'] = ('「%s」不是关卡代号，这里翻不出关卡。改用 find_ops 搜：'
                       '它会走作业站全文搜（标题/简介里带这个词的作业），'
                       '要锁定某一关就把关卡代号一起给它（「%s 4-3」）'
                       % (kw, kw))
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
        # 命中替换规则（sub，#92）的不算缺：写入 MAA 时会被练度等价的替补顶掉
        if o.get('owned') is False and not o.get('sub'):
            borrow.append(o.get('name'))
    return {
        'id': int(cid),
        'title': (content.get('doc') or {}).get('title') or '',
        'stage': content.get('stage_name') or '',
        'opers': [{'name': o.get('name'), 'owned': o.get('owned'),
                   'ok': o.get('ok'), 'elite': o.get('elite'),
                   'level': o.get('level'), 'sub': o.get('sub')} for o in opers],
        'notOwned': borrow,
        'note': 'owned=false 是要借的人；ok=false 是练度不够；'
                'sub 有值 = 该干员写入 MAA 时会被替换（如黍1顶塞1，#92）',
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
        [a for a in plan['accounts'] if a.get('ops')]),
        owners=[a.get('file') for a in plan['accounts'] if a.get('file')])
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
    raid = _variant_arg(args.get('raid'))
    res = _stage_rows(kw, acc)
    items, skipped, raid_missing = [], [], []
    seen_code, seen_key = set(), set()
    for packed in (res.get('stages') or []):
        st = packed.get('stage') or {}
        code = st.get('code')
        variant = st.get('variant') or 'normal'
        if code:
            seen_code.add(code)
        # 用户点名要突袭：只收突袭行。**不许拿普通行顶上**——「说要突袭、导入
        # 的却是普通 EX-8」就是这么来的（2026-11-23 用户报障）。
        if raid == 'raid' and variant != 'raid':
            continue
        if raid == 'normal' and variant == 'raid':
            continue
        # 同一关的普通/突袭两行只写一条；要 both 就两行都留
        key = (code, variant) if raid == 'both' else code
        if code and key in seen_key:
            continue
        best = next((o for o in (packed.get('ops') or [])
                     if o.get('mode') != 'blocked'), None)
        if not best:
            if raid in ('raid', 'both') and variant == 'raid':
                raid_missing.append(code)
            skipped.append(code)
            continue
        seen_key.add(key)
        items.append({'copilotId': best.get('copilotId'),
                      'stageCode': code, 'title': best.get('title'),
                      'variant': variant, 'name': code})
    if raid == 'raid':
        # 这些关有普通版、但压根没有突袭行 → 用户要的那一档排不出来
        raid_missing += [c for c in sorted(seen_code - {i['stageCode'] for i in items})
                         if c and c not in raid_missing]
    if not items:
        return {'error': '这份关键词下没有能打的作业', 'skipped': skipped,
                'raidMissing': raid_missing,
                'note': ('要求突袭，但这几关只有普通作业（或突袭作业缺干员），'
                         '没有偷偷用普通顶上' if raid_missing else '')}
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
                          maa_dir=(accounts.get_account(acc).get('maa') or {}).get('dir') or None,
                          account=acc)   # 带上账号：写盘时做干员替换（#92）
    out = {'started': True, 'taskId': tid, 'count': len(items),
           'difficulties': {i['stageCode']: ('突袭' if i.get('variant') == 'raid'
                                             else '普通') for i in items},
           'skipped': skipped,
           'note': '已提交写入 MAA，不要在这里等它跑完'}
    if raid_missing:
        out['raidMissing'] = raid_missing
        out['note'] += ('；这几关要的是突袭，但作业站只有普通（或突袭作业缺'
                        '干员），没有偷偷用普通顶上——照实告诉用户')
    return out


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
    """停挂机：点名了账号就只停那一路，没点名才全停

    2026-11-23 用户报障：「停止的话不能指定停止，会一起停其他任务」。他让停
    b 服那个号，结果两台模拟器一起断，官服正在打的关白烧。现在按账号停：
    本进程里的 MaaCore 实例（``stop_live``）+ 多开时的独立子进程
    （``stop_groups``）都按账号筛，别的路继续跑。
    """
    from . import skills
    want = str(args.get('account') or '').strip()
    files = []
    if want and want.lower() not in ('all', '全部', '所有', '*'):
        try:
            files = skills.match_accounts(want)
        except ValueError as e:
            # 「对上多个账号」是用户/模型给的信息不够，不是程序 bug：
            # 别让外层包成「内部错误」，直接把候选列表回给模型去问
            return {'error': str(e), 'need_info': 'account',
                    'candidates': [{'file': r['file'],
                                    'nickName': r.get('nickName') or ''}
                                   for r in accounts.list_accounts()
                                   if want.lower() in (r.get('nickName') or r['file']).lower()]}
    res = skills.stop_accounts(files)
    d = args.get('maa_dir') or None
    if not files:
        # 全停才动 MAA 本体（那是「跑 MAA 自己的任务队列」那条路）
        watch.stop_all()
        try:
            res['maaClosed'] = bool(maa.close(d))
        except Exception:
            res['maaClosed'] = False
    res['stopped'] = bool(res.get('stopped')) or bool(res.get('maaClosed'))
    if files:
        res['note'] = ('停了 %s 这一路（引擎 %d / 子进程 %d），别的账号没动。'
                       '没跑完的关不会自己续上，下次要重挂。'
                       % ('、'.join(files), res['engines'], res['processes']))
    else:
        res['note'] = '全停了。没跑完的关卡不会自己续上，下次要重挂。'
    return res


# 官服「账号管理器」里显示的条目是打码手机号：181****5505。MAA 切号就是拿
# AccountName 去 OCR 全匹配这一行，写昵称永远匹配不到 —— 表现是 select_account
# 死循环几十次、最后 "Account switch failed after restart"（2026-11-17 实测，
# 档案 moren 里 AccountName 存的是昵称，跑了 4 分钟没进去）。
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

    切号名的来源（规则只有一份，在 ``acctbind.resolve``）：
    1. 用户这次显式给的 ``names``（还会回写账号文件，下次不用再问）
    2. 账号绑的配置文件里「开始唤醒」的 AccountName（2026-11-22 用户要求：
       他在 MAA 里配过一次，就别再让他抄一遍）
    3. 账号文件里存过的 ``maa.accountName``（手填的 / 上次对话里报的）

    **只读用户的 MAA 配置，绝不写回**；也绝不拿昵称去凑切号名——官服认的是
    手机号，传昵称 MAA 在账号列表里死找 4 分钟超时，整轮挂机死在「登录」。
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
        # check() 自己会归一化（181 5505 / 181＊5505 / 5505 / 157****3635
        # 都能吃下），这里别再提前 norm 一遍——曾经两层各自处理，第二层收到
        # 的已经是半成品，星号数量被改坏后又被严格正则判成非法。
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
            ask = ('手机号后 4 位是多少？给这四位就行（如 5505），'
                   '前 3 位 + 后 4 位也行（如 157****3635）——'
                   'MAA 切号用的就是它'
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
                # 手填的值必须打上 manual：不然绑了配置文件的账号下次又被
                # 档案里的旧号码盖回去，用户改了等于没改
                acctbind.bind(c['file'], nm, c.get('channel'))
            except Exception:
                pass
        rows.append({'file': c['file'], 'name': c['name'],
                     'channel': c.get('channel') or '',
                     'accountName': nm, 'nameSource': source,
                     # 每个号用哪份 MAA / 哪台模拟器：多开时靠这两个字段分组
                     # （见 maaplan.plan_groups）。空串=用全局默认。
                     'maaDir': acctbind.dir_of(c['file']) or maa_dir or '',
                     'profile': acctbind.profile_of(c['file']),
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
    # 难度：用户说突袭就只排突袭行（以前这个工具压根没这个参数，永远取每关
    # 第一行=普通——2026-11-23「说突袭、导入的都是普通 EX-8」就是这条）
    raid = _variant_arg(args.get('raid'))
    raid_missing = []
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
        raid_all, raid_got = set(), set()
        kw_mode = ''
        for r in rows:
            try:
                snap = multiacct._snap_of(r['file'])
                res = copilot.search(kw, snap, pages=1, limit=8)
            except Exception as e:
                r['note'] = '搜作业失败：%s' % e
                continue
            kw_mode = kw_mode or (res.get('mode') or '')
            seen = set()
            for packed in (res.get('stages') or [])[:limit]:
                st = packed.get('stage') or {}
                code = st.get('code') or ''
                variant = st.get('variant') or 'normal'
                if code:
                    raid_all.add(code)
                # 用户点名要突袭就只排突袭行，**不拿普通行顶上**（「说突袭、
                # 导入的却是普通 EX-8」，2026-11-23 用户报障）
                if raid == 'raid' and variant != 'raid':
                    continue
                if raid == 'normal' and variant == 'raid':
                    continue
                if code and code in seen:
                    continue            # 同一关的普通/突袭两行只排一条
                best = next((o for o in (packed.get('ops') or [])
                             if o.get('mode') != 'blocked'), None)
                if not best:
                    continue
                seen.add(code)
                if variant == 'raid':
                    raid_got.add(code)
                r['ops'].append({'copilotId': best.get('copilotId'),
                                 'stageCode': code,
                                 'title': best.get('title') or '',
                                 'variant': variant})
            r['stages'] = [o['stageCode'] for o in r['ops']]
        usable = [r for r in rows if r['ops']]
        if not usable:
            return {'error': '这些账号在这批关卡里一份能打的作业都没有',
                    'hint': '要求突袭时还会更少——换成「普通」或 raid="both" 再试'}
        rows = usable
        if kw_mode == 'keyword':
            # 「结城理模组」这种关键词搜出来的作业横跨十来关（4-3 / VEC-01 /
            # H6-2…），照单全挂就是给用户排一堆他没说要打的关。这时必须让他
            # 先说清是哪一关——关键词 + 关卡代号一锁，基本必出模组作业。
            codes = sorted({o['stageCode'] for r in rows for o in (r['ops'] or [])})
            return {'error': '「%s」是关键词搜索结果，命中的作业横跨 %d 个关卡'
                             '（%s），不能直接排挂机'
                             % (kw, len(codes), '、'.join(codes[:6])),
                    'keywordStages': codes,
                    'hint': '先问用户要打哪一关，再把关卡代号写进关键词重新调'
                            '（例如「%s 4-3」），作业站会按「关键词 + 关卡」强制锁定'
                            % kw}
        if raid == 'raid' and raid_all - raid_got:
            raid_missing = sorted(raid_all - raid_got)
        else:
            raid_missing = []

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
    if need_ops:
        # 难度写清楚：不写这一行，助手很容易按自己刚才的说法回答用户
        diff = {('突袭' if o.get('variant') == 'raid' else '普通')
                for r in rows for o in (r['ops'] or [])}
        lines.append('难度：%s' % ('、'.join(sorted(diff)) or '（没排作业）'))
    if raid_missing:
        lines.append('注意：这几关要的是突袭，但作业站只有普通版，**没有排**：%s'
                     % '、'.join(raid_missing[:8]))
    lines.append('会直接驱动 MAA 核心跑（不用手动点开始），跑完自动切下一个账号。')

    payload = {'rows': rows, 'tasks': tl, 'formation': formation,
               'fightStages': fight_stages, 'profileHint': profile_hint,
               'start': start, 'maaDir': d, 'mode': mode,
               'raidMissing': raid_missing}
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

    tid = tasks.create('挂机（%d 个账号）' % len(rows),
                       owners=[r.get('file') for r in rows if r.get('file')])

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


def _guard(session, action, args, summary, force=False):
    """写操作的闸门：autoApply 关着就停下来等用户点确认

    ``force=True`` 用在高危动作上（黄票商店一次 258 张黄票）：**不看
    autoApply**，一定要用户亲手点一次。确认重入带 ``_confirmed``，调用方
    自己判断，别在这里再拦一遍。
    """
    if not force and settings.load().get('agent', {}).get('autoApply'):
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


def _tool_skill_difficulty(args, session=None):
    # 切档是纯点击，不耗理智、不改配置，和导航一样免确认
    return {'started': True, 'taskId': skills.run_skill('difficulty', args),
            'note': '切难度在后台跑，日志里有「当前 X → Y」和切完的复验结果。'}


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
    # stages 可能带 @raid/@both 后缀（逐关指定难度），预检只认纯关卡代号
    stages, _wants = sk._parse_stages(args.get('stages'))
    if not stages:
        return None
    file = ''
    try:
        file = sk._account_file(args.get('account') or '')
    except Exception:                       # noqa: BLE001 没账号就当匿名预检
        file = ''
    try:
        # 和 skill_fight 保持一致：默认不补前置，确认卡上列的就是真正要打的关
        auto = args.get('autoPrereq', False)
        mode = args.get('mode') or 'copy'
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
            'weekly': '周目编排', 'train': '培养干员',
            'sweep': '一键扫图'}.get(name, name)
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


def _tool_skill_sweep(args, session=None):
    return _skill_via_guard('sweep', args, session)


def _tool_step_need_confirm(st):
    """run_steps 里的某一步是不是「要用户先点头」的小工具 → ``(是否, 名字, 风险)``

    目前只有黄票商店（258 黄票换 38 抽，不可撤销）。名字认不出就返回 False，
    交给 skill 那边报错——这里只负责挑出高危步骤，不做校验。
    """
    from . import maatools
    if not isinstance(st, dict):
        return False, '', ''
    if str(st.get('skill') or st.get('step') or '').strip().lower() != 'tool':
        return False, '', ''
    try:
        _k, ent = maatools.resolve(st.get('name') or st.get('tool') or '',
                                   prefix=st.get('prefix') or None)
    except Exception:
        return False, '', ''
    return bool(ent.get('confirm')), ent.get('name') or '', ent.get('risk') or ''


def _tool_skill_tool(args, session=None):
    """MAA 小工具 / 商店

    普通项（小游戏 / 活动商店 / 绿票商店）点完直接后台跑；**黄票商店**先摆
    确认卡——这一张卡不受 autoApply 影响（``force=True``），因为它是花钱的：
    258 高级凭证换 38 抽，点错没法撤销。
    """
    from . import maatools
    args = dict(args or {})
    try:
        key, ent = maatools.resolve(args.get('name') or args.get('tool') or '',
                                    prefix=args.get('prefix') or None)
    except Exception:
        key, ent = '', None          # 名字不认识：原样丢给 skill，由它回可选值
    if ent and ent.get('confirm') and not args.get('_confirmed'):
        who = args.get('account') or '当前账号'
        summary = ('%s · %s\n%s\n确认后立刻开跑，不会再问第二次。'
                   % (ent.get('name'), who, ent.get('risk') or ''))
        blocked = _guard(session, 'skill_tool', dict(args, _confirmed=True),
                         summary, force=True)
        if blocked:
            return blocked
    if args.get('_confirmed'):
        clean = {k: v for k, v in args.items() if not str(k).startswith('_')}
        if ent and ent.get('confirm'):
            clean['confirm'] = True      # 用户刚点过那张卡
        return {'started': True, 'taskId': skills.run_skill('tool', clean),
                'note': '已在后台跑：日志里会写进了哪个商店、买了哪几样。'}
    return {'started': True, 'taskId': skills.run_skill('tool', args),
            'note': '已在后台跑：日志里会写进了哪个商店、买了哪几样。'}


def _tool_skill_claim(args, session=None):
    # 领奖是纯点击，不改任何配置、也不消耗理智，和唤醒一样免确认
    return {'started': True, 'taskId': skills.run_skill('claim', args),
            'note': '领奖在后台跑，日志里会写进了哪个奖励页、点没点中「一键领取」。'}


def _tool_run_steps(args, session=None):
    """按账号批量跑一组 skill（同一模拟器排队、不同模拟器并行）"""
    from . import maaplan

    steps = args.get('steps') or []
    if not steps:
        return {'error': 'steps 是空的',
                'hint': '每步写 {skill: "sweep", stage: "OS"} 这样；'
                        '可用：' + ' '.join(sorted(skills.SKILLS))}
    try:
        skills._norm_steps(steps)
    except ValueError as e:
        return {'error': str(e), 'valid': sorted(skills.SKILLS)}

    accounts = [str(x) for x in (args.get('accounts') or []) if str(x).strip()]
    names = {str(k): v for k, v in (args.get('names') or {}).items()}
    sw = args.get('switchAccount')
    if isinstance(sw, str):
        sw = sw.strip().lower() not in ('0', 'false', 'no', 'off')
    emus_raw = {str(k): str(v or '').strip()
                for k, v in (args.get('emulators') or {}).items() if str(v or '').strip()}
    d = multiacct._maa_dir({}) or (settings.load().get('maaDir') or '')
    rows, missing, err = _roster(accounts, names, d)
    if err:
        return {'error': err}
    if missing:
        return {'need_info': 'accountName', 'missing': missing, 'ready': len(rows),
                'note': '一次性把所有缺的账号问用户，补齐后带 names 再调一次'}
    if not rows:
        return {'error': '没有能跑的账号'}

    # 用户点名的模拟器：键可以是账号文件名，也可以是昵称（模型两种都爱写）
    by_name = {r['name']: r['file'] for r in rows}
    emus = {}
    for k, v in emus_raw.items():
        f = by_name.get(k) or next((r['file'] for r in rows
                                    if r['file'] == k or r['file'].startswith(k)), '')
        if f:
            emus[f] = v
    if emus:
        for r in rows:
            if emus.get(r['file']):
                r['address'] = emus[r['file']]

    groups = maaplan.plan_groups(rows, {'maaDir': d})
    desc = ' → '.join(skills.SKILLS[n][0].split('：')[0]
                      for n, _ in skills._norm_steps(steps))
    lines = ['%d 个账号 / %d 台模拟器' % (len(rows), len(groups)),
             '每步：' + desc]
    if len(rows) > 1 and sw is not False:
        lines.append('每个号开跑前先「开始唤醒」切号（用户说不用切号才关）')
    for key, g in groups:
        who = '、'.join(r['name'] for r in g['rows'])
        lines.append('· %s ← %s（%s）'
                     % (g['address'] or '默认模拟器', who,
                        '排队切号' if len(g['rows']) > 1 else '单独跑'))
    same = [g for _k, g in groups if len(g['rows']) > 1]
    devs = [x for x in skills.online_devices(d) if x]
    hint = ''
    if same and len(devs) > len(groups):
        hint = ('\n注：%d 个号被算成同一台模拟器，但这台机器上还有别的设备在线'
                '（%s）。要并行的话告诉我「哪个号用哪台」，我用 emulators 传进去。'
                % (sum(len(g['rows']) for g in same), '、'.join(devs)))

    # 批次里混着「花钱」的小工具（黄票商店）时，一律先摆确认卡，**不看
    # autoApply**；用户点了卡之后，那一两步才带上 confirm=true 放行。
    risky = [_tool_step_need_confirm(st) for st in steps]
    risky = [x for x in risky if x[0]]
    if risky:
        lines.append('⚠ ' + '；'.join('%s：%s' % (n, r) for _f, n, r in risky))
        steps = [dict(st, confirm=True) if _tool_step_need_confirm(st)[0] else st
                 for st in steps]
    summary = '\n'.join(lines) + hint

    payload = {'accounts': [r['file'] for r in rows], 'steps': steps,
               'names': {r['file']: r['accountName'] for r in rows},
               'emulators': emus, 'switchAccount': sw}
    blocked = _guard(session, 'run_steps', dict(payload, _confirmed=True), summary,
                     force=bool(risky))
    if blocked:
        return blocked
    tid = skills.run_batch(payload['accounts'], steps, names=payload['names'],
                           emulators=emus, switch=sw)
    out = {'started': True, 'taskId': tid, 'accounts': len(rows),
           'emulators': len(groups),
           'note': '已在后台跑：同一台模拟器的号排队切号，不同模拟器同时开跑。'
                   '每行日志带 [账号名]，整份日志还会落到 debug/tasks/<任务号>.log。'}
    if hint:
        out['hint'] = hint.strip()
    if devs:
        out['devices'] = devs
    return out


_HANDLERS = {
    'list_accounts': _tool_list_accounts,
    'set_account_name': _tool_set_account_name,
    'bind_emulator': _tool_bind_emulator,
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
    'skill_difficulty': _tool_skill_difficulty,
    'skill_signin': _tool_skill_signin,
    'skill_claim': _tool_skill_claim,
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
    'skill_sweep': _tool_skill_sweep,
    'skill_tool': _tool_skill_tool,
    'run_steps': _tool_run_steps,
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

**被问「你可以帮我干什么」时**，用人话列清单（别丢工具名），只列下面这些
真做了的，一条都不许编：

- 挂关卡：抄作业自动战斗（普通 / 突袭 / 两档都打）、纯刷理智、过剧情关
- 活动一条龙：唤醒 → 导航进活动（当期 OS 走终端页横幅）→ 普通/EX/S 逐段扫
  → 领奖励 → 搬商店（黑名单可配）
- 多账号：同一台模拟器排队切号，不同模拟器真并行；能按账号单独停
- 日常：开始唤醒、自动公招、基建换班、理智作战、信用收支、领取奖励
- 森空岛签到、培养干员（精英化 / 技能 / 专精）、MAA 小工具（活动小游戏 / 商店）
- 看状态：跑到第几关、卡在哪、为什么失败
- 配任务：新建 PRTS- 开头的独立档案，**用户自己的配置一个字段都不动**

列完补一句具体的（「现在开的是雪山降临 1101，要我把 OS 关卡挂上吗」），
别只丢一张清单就完事。

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
1. list_accounts —— **先看 accountName（已绑定的切号名）**：非空的账号
   直接沿用，**绝对不要再问一遍**（用户 2026-11-18 报过这个：设置里配过
   尾号，助手还在问）
2. build_run（accounts=[]、mode=copy、keyword=TL-5）
3. 返回 need_info 时：**一次性把所有缺的账号列出来问**。官服问
   「手机号后 4 位」（如 5505）——用户给「前 3 位 + 后 4 位」
   （157****3635）、全号、带横线都行；B 服问游戏内用户名。
   **不要让用户必须拼 `181****5505`**——格式是程序的事，给什么都能吃。
   拿到后带 names 再调一次
4. 返回 need_confirm 时，把确认卡片上的内容用自己的话复述要点

**「这个号尾号是 5505」/「官服号尾号 157****3635」**
set_account_name（account 尽量给上，按昵称或 file 认）。存进设置之后
list_accounts 的 accountName 就有值了，后面挂机不用再问。
只改我们自己的账号配置，MAA 里一个字都不动。

**「清一下日常」/「帮我收菜」**
build_run（mode=daily，不用 keyword）。这就是一键长草。

**「刷点 1-7 的材料」**
build_run（mode=fight、keyword=1-7）。纯理智作战，不抄作业。

**「这周活动帮我打一下」**
list_activities 确认哪个 → build_run（mode=copy 或 daily_copy，keyword=活动前缀）

**OS（雪山降临 1101）现在值得打的就这几件**（用户 2026-11-23 口径，别自己加戏）：
- 普通 OS-1 ~ OS-10
- EX-1 ~ EX-8（普通）
- **EX-8 单关补一次突袭**（只有 EX-8 要突袭，别的 EX 关没提就别排）
- S-1 ~ S-5（**普通**）
- **S 关突袭没有奖励，用户说「太变态了，刷那个没有奖励」——绝对不要主动
  给 S 关排突袭，也别在回话里建议他打 S 突袭**
- 收尾照旧：领奖励 + 搬商店（黑名单默认那四个）

用户只说「挂一下 OS」时，按上面这套排（S 用普通、EX 用普通、不排 S 突袭），
回话里把「EX-8 要不要补突袭」问一句就行。

**「把这 5 个号的 OS 活动挂一下（普通、突袭、S、领奖、搬商店）」**
1. list_accounts —— 看 `current`（用户在左侧点开的那个号）和各自的 `emulator`
2. 按「复刻突袭」那两条先问：要突袭吗？去年打过吗？（OS 是复刻活动）
3. run_steps，一次说清哪些号、做哪几步：
   ```
   steps = [
     {"skill":"sweep","stage":"OS","tiers":["base","ex","s"],"raid":"normal"},
     {"skill":"claim","prefix":"OS"},
     {"skill":"tool","name":"event_shop","prefix":"OS"}]
   ```
   同一台模拟器上的号会自动排队切号，不同模拟器自动多开并行。
4. 中间某一步失败不会拖垮别的号，但**哪个号哪一步失败要在回话里说清楚**

**「把活动奖励领了」** → skill_claim（带 prefix；游戏没开就传 wakeup=true）。
领完通常接着要搬商店：**主动问一句**「要顺手把活动商店也搬了吗」，
不用等他再说一遍。

**「帮我把这个号的奖励领了，再搬商店」**
→ run_steps(accounts=[这个号], steps=[{"skill":"claim","prefix":"OS"},
  {"skill":"tool","name":"event_shop","prefix":"OS"}])

**「现在跑到哪了 / 为什么失败」**
run_status。state 是 stalled 就说卡在哪个节点，failed 就说原因。
想看跑到第几个作业，用返回里的 stage / index / total。

**「停下来」** → stop_run
- 用户说清楚是哪个号（「停 b 服那个」「停示例昵称#0384」）**必须带 account**，
  只停那一路；不说才全停。停之前先用一句话说清你要停谁。
- 停完照实报：停了哪个号；**别把「停了一个号」说成「都停了」**。
  用户 2026-11-23 就是被这句坑过（他说停 b 服，结果两台都断，官服那轮
  正在打的关白烧）。工具返回里有 `accounts` / `engines` / `processes`，
  按它说。

## 挂关 skill（2026-11-17 新增）：把流程拆开一步一步来
八个 skill，各自独立跑后台、失败单独停：
- skill_wakeup：唤醒游戏到主界面（可按尾号切号）
- skill_navigate：导航到活动（老活动 / 当期 / EX 都行）。stage 优先传**中文
  活动名**（「孤星」），代号/活动 id 也行——内部统一换，别自己换算。
  **主线 9~14 章要切作战环境就带 difficulty**（一次调用到位）。
  当期活动（ENTRIES 里有的，如 OS / TL）走的是「终端页 → 活动横幅 → 地图上
  直接点分区节点」，**失败时不会退回老路径**（老 UI 早就点不到了）：报的是
  「当期活动导航没到位：…」。照实告诉用户是入口认不出、要重新标定导航，
  别建议他「用老办法再点一次」
- skill_difficulty：切主线第 9~14 章的「作战环境」——剧情体验 / 标准实战 /
  磨难险地（第 9 章只有前两档，10~14 章三档）。带 stage 就先导航过去再切；
  不带就按「界面已经在那一章的关卡列表上」处理。**「导航 + 切档」别调它**：
  给 skill_navigate 传 difficulty 一次做完，分两步助手常常漏第二步；
  挂机时同理写进 skill_fight 的 difficulty
- skill_skip_story：过剧情关（ST 关自动点开 + 跳剧情）
- skill_fight：自动战斗（编队 / 借助战 / 吃理智药）。**默认要补唤醒+导航**：
  用户点名了账号、又没说「不用切号 / 已经开着这个号」，就传 wakeup=true
  （先唤醒按档案切号 → 导航到这组关 → 开打；不切号就可能打到别人那个号上）。
  只有他明说「直接跑 / 已经在关卡列表 / 别切号」才直跑
  （assumeStageList=true 或 noSwitch=true）。
  **打不过会自动回关卡菜单、只换那一关的作业重挂**（没候选就同一份再试一次，
  前面打完的关不重打），日志报「跑到哪几关 / 卡在哪 / 为什么」
- skill_signin：森空岛签到
- skill_weekly：**周目编排**——ST 剧情自动过、主线按序批量打、EX 收尾
- skill_train：**培养干员**——升精英化 / 技能等级 / 专精，可一次排多个
- skill_sweep：**一键扫图**——普通关 → EX 普通 → EX 突袭 → S 关（S 只打普通，
  突袭没奖励），逐段重新导航 + 挂机。用户说「这个活动从头打到尾」「EX 和 S
  都清了」就用它，比 skill_weekly 更适合「只要战斗、不要剧情编排」的场景
- skill_tool：**MAA 小工具（牛杂）**——`name="wartime_press"` 战时新闻委员会
  （TL）、`name="os_rd"` 喀兰贸易技术研发部（OS）、`name="event_shop"` 活动商店。
  三项都自己导航到活动地图再点入口。只给「小游戏」这种泛称时**必须带 prefix**
  （OS / TL）——两个活动的小游戏是 MAA 里两条不同的任务，选错进去也是白跑。
  活动商店必须给黑名单（默认四个：数据增补条 / 数据增补 / 龙门币 / 家具零件），
  **空黑名单会把无限池也买掉**，别传空数组。用户说「把快关闭的 TL 商店清空，
  黑名单：数据增补条、数据增补、家具零件」就是
  `skill_tool(name="event_shop", prefix="TL", blacklist=["数据增补条","数据增补","家具零件"])`
  ——**「黑名单」是不要买的**，别理解成要买的清单。
  小游戏第一次进要过教程/引导，MAA 不会点教程对话：任务里 `stuck` / 超时的时候，
  明确告诉用户「先在模拟器里手动把教程过一遍，过完再让我挂一次」，不要说
  「任务失败请重试」这种空话
- skill_claim：**领取活动奖励**（OS 的「神谕谜题」页 → 一键领取）。纯点击，
  不用 MAA 任务；游戏没开就先 skill_wakeup 或传 wakeup=true

**多账号怎么跑（2026-11-22）**：
- **每个号的流程开始都是「开始唤醒」切号**（2026-11-25 用户口径）：走
  `run_steps` 程序会自动补第一步，你**不用**往 steps 里写 wakeup；一个号一个号
  调 skill 时，每个号的第一条必须是 `skill_wakeup(account=...)` 或
  fight/sweep 带 `wakeup=true`。只有用户明说「不用切号」才省掉。
- 一个号一段连续操作 → 单独调 skill
- **多个号做同一套事** → 用 `run_steps`，把步骤写成列表交给我，程序自己
  按模拟器分组：同一台模拟器排队切号，不同模拟器多开并行
- 用户说「5 个号都挂一下 OS 活动，普通/突袭/S/领奖/商店」就这么写：
  ```
  run_steps(accounts=[...], steps=[
    {"skill":"sweep","stage":"OS","tiers":["base","ex","s"],"raid":"normal"},
    {"skill":"claim","prefix":"OS"},
    {"skill":"tool","name":"event_shop","prefix":"OS"}])
  ```
  （raid 按「复刻突袭」那段先问用户；**别一个号一个号地调 skill**——
  那样前一个还在跑，后一个就抢同一台模拟器）
- **联排先按模拟器分组，导航各归各的**：看 list_accounts 的 `emulator`，
  同一台的号排成一串、不同台的并行；每个号的**导航必须是它这一组自己的
  第一步**（sweep / wakeup 自带，别再交叉写）。两台模拟器同时抢同一个活动
  入口会互相干扰，现象就是一直卡在导航、反复退回主页（2026-11-24 用户口径）。
  **一个号的步骤跑完先退回主页，再唤醒切下一个号**——直接在别人那个号的
  关卡列表上接着点，就会打到他的号上。
- list_accounts 的 `current` 是用户**此刻在左侧点开的那个号**：用户没点名
  是谁时就用它，别反问「你说哪个账号」；`emulator` 相同的号只能排队，
  不同的可以并行
- 每个号用的是**它自己绑的 MAA 目录和配置文件**（设置 → MAA 路径），
  不需要你传 maaDir
- **但要并行两台，光靠档案常常不够**：MAA 里好几份档案可能全写着
  `emulator-5554`，而用户其实开着两台。`list_accounts` 里看两个字段：
  `emulator` 是这个号实际会连的那台，`emulatorPinned` 是「用户钉过没有」。
  - **用户说「这个号用第二台 / 用 5556 / 别挤一台」→ 先调
    `bind_emulator(account=..., address="emulator-5556")`**。这一步把地址
    写进账号设置，以后每轮跑机都连这台——不做这一步，下一轮你就忘了，
    两个号又挤到同一台上（2026-11-23 用户报的正是这个）。
  - 绑定后再说 `run_steps(accounts=[...], steps=[...])` 就行，不用每次传
    `emulators`；临时改某一次也可以传，优先级最高。
  - 钉过的地址**绝不自动换成别的**：那台不在线时，不是换一台在线的顶上，
    而是**按它自己的实例序号把那台启动起来**（雷电用 `ldconsole adb --index
    N get-serialno` 反查，5556 就是实例 1）。所以用户说「帮我开第二台」时，
    直接按那个号跑机就行，前检会拉起 5556 而不是永远拉起 5554 那台
    （2026-11-23 用户报「只能启动 5554，启动不了 5556」）。反查不到是哪台
    实例的模拟器（MuMu 等）不猜，只提示用户自己开。没钉过、档案地址又过期
    时，只有**一台**在线才会顶上；两台都在线会直接拦下要用户指定——这是
    故意的，别绕过。
  - 同一台模拟器的号排队切号，不同的号多开并行（每组一个独立进程）
- **官服 / b 服是两个不同的包**：账号的渠道自动判定（b 服走
  `com.hypergryph.arknights.bilibili` + MaaCore 的 `Bilibili`），你**不用**
  也别自己切客户端。两个参数容易搞混，这里记清楚（2026-11-23 两次翻车）：
  - `AsstConnect` 第 4 个参数是**模拟器连接配置名**（`General` / `LDPlayer`），
    传客户端类型会报 `ConfigNotFound`（b 服账号仍按官服包启动）
  - 客户端类型只走**任务参数** `StartUp.client_type`（`Official` / `Bilibili`）
  - 日志里「连接 emulator-5556（b服）」这行就是实际生效的客户端；写错渠道
    说明账号文件的 `channelName` 不对，让用户在设置里改，别自己瞎试.
- 每组跑在**自己的进程**里（不是线程）：同一个进程里两个 MaaCore 会抢
  命名管道，第二台永远连不上（asst.log 里 `CreateOverlappablePipe failed,
  err 231`）——以前「说好并行，另一台一动不动」就是这个。现在多组=多进程，
  两台会真的同时动
- **正在跑一路时再起另一路，后起的那路会自动换独立进程**（`claim_inproc`
  抢不到就让位），所以用户可以放心「再加一个号」。以前两路挤在一个进程里，
  后来的那个会把**先跑的**任务链打断（只发 TaskChainStopped，日志写「已停止
  （你按了停止）」，其实没人按）——2026-11-23 用户报「多开另一个号，他把我
  前面的停了」就是它。日志里看到「这一路改用独立进程」是正常提示，不用解释
  成出错
- 日志每行带 `[账号名]`，右栏按账号分栏（切到哪个号只看那个号的日志）；
  完整日志另存在 `debug/tasks/<任务号>.log`，出问题让用户把这个文件发你

**什么时候用 skill 而不是 build_run**：
- 用户要「从唤醒开始，剧情关也帮我过，一口气打完」→ skill_weekly。
  MN 这种：ST-1 → MN-1~7 → ST-2 → MN-8 → EX-1~8，skill_weekly 自己排，
  遇 ST 自动过剧情、连续战斗关合并成批。build_run 遇到剧情关会卡死，用不了。
- 只想导航到某活动看看 / 手动接管 → skill_navigate
- 游戏没开、卡登录页 → skill_wakeup
- **单独打一组关**（用户明说「我已经在关卡列表」/「别切号，直接开始」）→
  skill_fight 并传 `assumeStageList=true`（或 `noSwitch=true`），不要补导航。
- **除了上面两句例外，`fight` 一律先唤醒 + 导航**：`run_steps` 里程序会
  自动补，但**你直接调 skill_fight 时得自己传 wakeup=true**——不传就是直跑，
  界面停在哪就在哪开打。因为上一个号可能停在关卡列表、主页、基建或结算页，
  界面状态无从判断，界面上的号也可能根本不是用户说的那个
  （2026-11-22 / 2026-11-24 用户口径）。
- **把一个活动的普通关、EX（含突袭）、S 全清一遍** → skill_sweep。它自己
  分段导航、每关失败换作业重挂，不用一段段手动调 skill_navigate + skill_fight
- 签到 → skill_signin
- **培养干员**（升精二 / 技能 7 / 专精几技能）→ skill_train
- **小游戏 / 活动商店**（MAA 牛杂里那两项）→ skill_tool，别自己写 Custom 任务

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

## 主线（主题曲）怎么挂

主线（关卡号 `15-3` / `0-11` / 突袭 `H15-1`）和老活动不一样，**打法分两套**：

- **第 0~8 章、15~17 章**：只有普通 / 突袭。导航到关卡列表 → 排作业集就是
  全部，没有难度可选。用户说突袭就 `raid="raid"`，没说就是普通。
- **第 9~14 章**：多了「作战环境」三档 —— 剧情体验环境 / 标准实战环境 /
  磨难险地环境。**第 9 章只有剧情、标准两档；第 10~14 章才三档**。
  这三档是**同一个关卡列表的不同档**（磨难那档在游戏里是另一批关卡），所以
  **导航到那一章的关卡列表之后必须先选档，再排作业集**。界面停在别的档上，
  打下去就是另一档的关。

所以第 9~14 章调 skill_fight 时，难度写进 `difficulty`：
```
{"stages": ["10-1","10-2","10-3"], "difficulty": "hard", "wakeup": true}
```
程序会在导航到位后自动切档，日志里写「作战环境：标准实战环境 → 磨难险地环境」；
切不过去就直接报错（不会闷头打错档）。用户没说难度时按**标准实战环境**传
`difficulty:"normal"`，并在回复里说一句「你没说难度，我按标准实战环境打」。

**只说「导航到第 13 章、切磨难」时，一次调用做完**：
`skill_navigate(stage="13-1", difficulty="hard")` —— 导航到位后自己切档。
**不要**「先 skill_wakeup、再 skill_navigate、再 skill_difficulty」拆成三个调用：
用户 2026-11-25 实测，拆开发出去的两个任务同时动同一台模拟器，切难度那一路在
MAA 还在登录时查游戏进程、查到空就报「游戏没在跑」，导航一次都没做；同一时段
MAA 的 StartUp 三次「回到主界面」也全认错（两路一起点，界面被点乱）。
同一个账号的 skill 是**串行**的（后发的会排队等前面的跑完，日志里会写
「这个账号还有一路在跑…等它跑完」），所以能一次调用解决就别拆，拆了只会排队。

**游戏没开也别先喊唤醒**：skill_navigate / skill_difficulty 一进场发现游戏进程
不在，会自己走一遍唤醒（有切号名 = MAA 开始唤醒，没有 = 重启客户端），再接着
导航/切档。用户 2026-11-25 原话：「adb 正常后就应该启动 maa 的自动唤醒了，而不是
干等」。只有用户要**切号换号**（换另一个账号登录）时才单独调 skill_wakeup。

只想换档、不重排作业（「先切到磨难我看看」）就调 `skill_difficulty`。
0~8 / 15~17 章没有这条栏，别传 difficulty、也别调 skill_difficulty。

## 硬规矩
- **一次任务涉及两个及以上账号 → 每个号的流程开始必须「开始唤醒」切号**
  （2026-11-25 用户口径）。上一个号可能停在关卡列表 / 主页 / 基建 / 结算页，
  那台模拟器上登的也未必是你要的号——不唤醒就接着点，打到的是别人的号。
  - 走 `run_steps`：程序自动补第一步 wakeup（第一步是 sweep / weekly / train
    或带 wakeup 的 fight 就不重复补），**别自己再往 steps 里加 wakeup**，
    也别传 `switchAccount=false`（用户没说不用切号就不要关）。
  - 一个号一个号地调 skill（只在用户要求「先跑这个号我看看」时）：每个号的
    第一条都必须 `skill_wakeup(account=...)`，或 fight / sweep 传 `wakeup=true`。
    **刚唤醒过一次不等于一直有效**——中间换了号就失效了。
  - 单账号不强制：界面本来就是那个号，别为了「保险」白重启一遍客户端
    （2026-11-17 用户吐槽过单号被强行唤醒）。
- **点名了账号就默认要切号**：用户指定了某个号（「挂一下 5505 那个」），
  只要没明说「不用切号 / 别换号 / 已经开着这个号」，**一律先唤醒切号**
  （skill_fight 传 wakeup=true，或先 skill_wakeup 再打）。**别把界面上正在
  跑的那个号当成他说的号**（2026-11-24 用户口径）。
- **作业前面默认要有导航**：用户没明说「直接跑 / 别导航 / 已经在关卡列表」
  时，排作业前必须有一道导航（skill_navigate，或 fight / sweep 的
  wakeup=true 全套；run_steps 里程序会自己补）。**没有导航直接排 Copilot
  就是在当前界面上瞎点，必卡死。**
- **打关先看预检，别硬挂**：skill_fight 开打前会按顺序补前置关、拿作业要求
  比对这号练度。返回里有 `precheck` 且列了「练度不足 / 缺干员 / 前置未通」时，
  **必须先把「哪几关打不了、为什么」原样告诉用户**（练度不够要点出差哪个干员），
  再说能打的几关已在后台跑。一关都打不了时工具会直接返回 error，别改口说"开打了"。
- **stages 只写用户点名的关，一个都不许自己加**：用户说「挂 S-3、S-4」就传
  `["TL-S-3@raid","TL-S-4@both"]`，**不要自己补 S-1、S-2 这种前置**（用户
  2026-11-19 实测：「我让他挂的关卡和他给我挂的关卡不一致」）。补前置是
  `autoPrereq: true` 才会做的事，默认关；要补也在回话里先说清补了哪几关。
- **别把没解锁的关排进队列**：前一关没通，后面的关在地图上是灰的。用户点名
  要 S3 而 S1/S2 没打时，日志会写「不补前置：S3 要先通前面的关」，把这句话
  转述给用户，问他「要不要一起补」，他同意再传 `autoPrereq: true`。
- **活动代号一律查，不许凭记忆换算**：用户说「孤星」就搜 search_stages("孤星")
  或直接把中文名交给 skill_navigate，**不要自己猜成 SV**（孤星=CW，
  覆潮之下=SV，愚人号=SN，不义之财=CV）。猜错一次就是几轮白跑。
- 中文活动名 → 代号走 search_stages；连活动名都拿不准就先 list_activities
  （返回正式名 + 代号前缀）让用户挑
- 写 MAA 之前必须让用户看到「给谁、跑哪些任务、挂哪几关」，等他确认
- skill_fight / skill_skip_story / skill_weekly / skill_train 同样要先确认再跑
  （工具会自动摆确认卡）
- 多账号缺切号名时，明确说「这个信息森空岛拿不到，得你告诉我」；
  官服问**手机号后 4 位**（如 5505）就行，用户主动给「前 3 位 + 后 4 位」
  （157****3635）也照收，不要要求他数星号。
  **问之前先看 list_accounts 的 accountName**：非空就是已经绑好的，
  直接用，别重复问（用户设置里配过的东西，助手再问一遍很烦）。
  用户报出尾号后调 set_account_name 存进设置，下次不用再问。
- 多账号是**串行**的（一个模拟器同时只能登一个号），一个跑完自动切下一个
- **模拟器不用你操心**：每个 skill 开跑前会自动确认模拟器在不在、没开就替用户
  启动（路径从 MAA 配置里读）。任务报「环境没准备好」时，把日志那句话原样转给
  用户让他手动开，别自己反复重试、也别去改 MAA 的连接设置
- **突袭不是磨难**：EX/活动关的「突袭」和主线的「磨难/险地」是两回事（用户
  2026-11-19 明确）。**难度按用户原话，一个字都不许替他改**：代码里
  `raid='auto'`（不传）= **只打普通**，所以用户说「打突袭」时必须显式写
  `raid="raid"` 或把那一关写成 `SV-EX-1@raid`，否则排下去的真是普通——
  2026-11-23 用户报「说突袭，两个号导入的都是普通 EX-8」就是这么来的（嘴上
  突袭、参数没传）。反过来，用户说「普通」也绝不能升成突袭。
  普通还没通关、要一次打通时，把那一关写成 ``SV-EX-1@both``。
  同一次计划里可以逐关分开写：``["SV-EX-1@raid", "SV-EX-6@both"]``。
  磨难/险地是主线第 9~14 章的「作战环境」，**不是突袭、也不用 @ 后缀**：
  它是另一批关卡，要用 skill_fight 的 `difficulty`（或 skill_difficulty）切档。
  **不要**跟用户说「突袭打不了」——作业站有突袭作业就能排；真搜不到突袭
  作业时日志里会写「这关没有突袭作业，只打普通」，**照实转告用户**，别
  顺着自己刚才说的话继续吹。
- **复刻活动的 EX / S：先把突袭问清楚**（用户 2026-11-22 要求）：list_activities
  里 `reprint: true`（现在 OS = 雪山降临1101）或活动导航标了复刻的，用户说要刷
  它的 EX / S 时，**先问两句**：「要不要连突袭一起打」「去年这个活动打过吗」。
  去年打过 → 突袭的首通奖励已经领过，今年再打只多花理智，**默认不加**；用户说
  不用突袭，就逐关写 `@normal`（或 raid=normal），排进作业集的别出现突袭行。
  用户明确要打，再按 `@raid` / `@both` 排。「去年打过」只影响奖励，不影响能不能打。
- **换作业要真的换**：用户说「用 41624 那份」「换成令的单核」时，先 find_ops
  拿到 copilotId，再调 skill_fight 时用 ops=[{stage, copilotId}] 钉住。只把
  文案改成"已换"是没用的——不传 ops 就会重新搜索，排序又挑回原来那份。
- **翻车会自动重挂，不用你重排**：skill_fight / skill_sweep 每关都备了
  2~3 份作业，某关打不过会换下一份，**从卡住的那关往后重挂**（前面的关不再打）。
  失败只认 MAA 的回调（任务失败 / 结算不是三星），程序不自己猜；确认翻车后
  先点一下屏幕（MAA ClickCorner），等界面回到活动关卡界面再排新作业
  （用户 2026-11-22 实测：失败后点任意位置都会立刻回到关卡界面，
  **不需要重新唤醒/登录**，也不需要重走导航）。
  日志里会出现「换作业重挂：X #a → #b」「翻车：X —— 原因」。
- **S 关（散点关卡）自动走另一条路**（2026-11-23）：OS 的 S 关浮在雪山地图上
  （积雪清理系统），MAA 的关卡识别在关卡列表里 OCR，认不出这种界面。skill_fight
  认到 `XX-S-1` 这种形状且活动有标定，就会自己点格子 → 点「开始行动」/「开始
  突袭」→ 进编队页，再用单作业模式一关一关打。**你不用提醒用户「MAA 认不出
  S 关」，也不用让他手动点**；他排 `stages=['OS-S-1@normal', …]` 就照常打。
  S 关**同样有普通和突袭两档**，用户要突袭就传 `raid` 或逐关 `@raid`。
  这条路单作业模式**不判三星**（MaaCore 打完只发任务完成），日志会写「打完了
  （单作业模式不判三星）」，所以别说「几关三星通过」。

- **难度只能照用户原话，不许替他升级、也不许替他下结论**（2026-11-23 用户
  投诉「出幻觉了还不听命令了」）：用户说「帮我挂 OS-S 关 1 到 5 普通」，那一轮
  助手干了两件错事——① 对话里编了一句「OS 是复刻活动，S 关只有普通难度，
  没有突袭」（S 关普通/突袭都有）；② 排单时没传 `raid`，程序当时默认
  auto→突袭，于是 5 关全按突袭排，第二关还因为突袭作业缺干员卡了 20 分钟。
  - 用户说「普通」→ **必须显式传 `raid: 'normal'`**；说「突袭」→ `'raid'`；
    说「都打」→ `'both'`。只说了关卡没提难度才省略（默认只打普通）。
  - **不许**在回复里断言「某关有没有突袭」「复刻活动所以没有突袭」这类话。
    难度以实际排单为准，日志里有一行「难度：普通 OS-S-1、OS-S-2 …」，照着
    念。要确认就先调 find_ops / list_activities 看数据，别推理。
  - 排单前把「**哪几关、什么难度、哪个号**」跟用户的话对一遍；对不上就别开跑。

- **逐关成败不要报数**（2026-11-22 用户口径）：MAA 在抄作业时**不回报每关
  成功/失败**（asst.log 里没有任何 StageDrops 回调），程序也就不知道「成了
  几关」。只能说「跑到了哪几关 + 任务链正常结束还是卡在哪关 + 原因」，
  **绝不能说「一关都没挂」「成功 0 关」**——那是假消息。
- **关卡名单只能用用户点名的那些**（2026-11-19 用户实测吃亏）：用户说
  「SV-EX-1 到 SV-EX-6」就排这 6 关，**不许改编号、不许跳掉一关再往后补
  一关凑数**（那次实际排成 SV-EX-2 ~ SV-EX-7，白烧一轮理智）。跳过要说明
  「EX-1 我按你说的跳过了」，不要偷偷把 EX-7 加进来。
- **「最新活动 / 当期活动」**：先 list_activities，看返回的 ``current``（带
  ``until`` 有效期和 stage 代号）——直接用它，EX 关是 ``前缀-EX-1``、S 关是
  ``前缀-S-1``。current 为空（活动都过期了）就如实说「当期活动还没标定导航，
  把截图和步骤发我」。
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
        # 取消时那个 tool_call 还没有响应：补一条，否则历史断了，下一句话
        # 一发出去就被服务端 400 拒（用户 2026-11-19 连吃三次的根因之一）
        call = s.pending_call
        if call:
            s.messages.append({'role': 'tool',
                               'tool_call_id': call.get('id') or '',
                               'content': '（用户取消了这一步）'})
        s.pending = None
        s.pending_call = None
        s.push('say', text='（已取消）')
    return {'ok': True}


def _trim(s):
    if len(s.messages) > KEEP_MESSAGES:
        # 保留第一条 user 和最近若干条，避免上下文无限长。**切点不能落在
        # 工具调用中间**：只按条数切会留下以 role=tool 开头的半截历史，
        # 服务端直接 400（`tool` 前面必须有一条带 tool_calls 的 assistant）。
        head = s.messages[:1]
        tail = list(s.messages[-(KEEP_MESSAGES - 1):])
        while tail and tail[0].get('role') == 'tool':
            tail.pop(0)
        s.messages = head + tail


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
            for i, call in enumerate(calls):
                stop = _do_call(s, call) or stop
                if stop:
                    # 这批里排在后面的调用不会执行：补占位 tool 消息。少了它们，
                    # 历史里 assistant.tool_calls 就缺响应，下一轮请求直接 400
                    for rest in calls[i + 1:]:
                        s.messages.append({
                            'role': 'tool',
                            'tool_call_id': rest.get('id') or '',
                            'content': '（上一步在等用户确认，这一步没有执行）'})
                    break
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
    try:
        res = run_tool(name, args, s)
    except Exception as e:              # noqa: BLE001 工具抛异常也必须回一条 tool
        # 不回的话 assistant.tool_calls 缺响应，下一轮请求被服务端 400 拒掉
        res = {'error': '%s: %s' % (type(e).__name__, e)}
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
    call0 = s.pending_call or {'id': 'confirm'}

    def reply(text, res=None):
        s.messages.append({'role': 'tool',
                           'tool_call_id': call0.get('id') or '',
                           'content': text})
        if res is not None:
            s.push('tool_result', name=(s.pending or {}).get('action') or 'confirm',
                   text=json.dumps(res, ensure_ascii=False)[:1200])

    try:
        pend = s.pending or {}
        action, args = pend.get('action'), pend.get('args') or {}
        s.push('say', text='好，开始执行…')
        fn = _WRITE_HANDLERS.get(action)
        if not fn:
            s.push('error', text='不认识的操作：%s' % action)
            reply('不认识的操作：%s' % action)
            s.pending = s.pending_call = None
            s.running = False
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
        s.pending_call = None
        reply(json.dumps(res, ensure_ascii=False)[:6000], res)
        _loop(s)
    except Exception as e:
        # 执行失败也要回 tool：否则 assistant.tool_calls 悬空，下一轮 400
        s.error = '%s: %s' % (type(e).__name__, e)
        reply(s.error)
        s.pending = s.pending_call = None
        s.push('error', text=s.error)
        s.running = False


def status():
    """设置页/面板判断能不能用"""
    cfg = config()
    return {'configured': bool(cfg['hasKey'] and cfg['model'] and cfg['baseUrl']),
            **cfg, 'sessions': len(_sessions)}
