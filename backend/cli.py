# -*- coding: utf-8 -*-
"""M5 命令行：把内部能力暴露给外部 harness（Claude Code / WorkBuddy / 脚本）

用法::

    python -m backend.cli accounts
    python -m backend.cli stages TL-
    python -m backend.cli ops TL-5 --account 主号.json
    python -m backend.cli progress
    python -m backend.cli status
    python -m backend.cli runs
    python -m backend.cli plan TL- --kind activity
    python -m backend.cli watch
    python -m backend.cli doctor

打包后同一个入口是 ``MAA-PRTS.exe --cli <子命令>``。

约定（照 MaaPilot 那套来，agent 读起来最省事）：

- 输出永远是**一个 JSON 对象**：``{ok, command, data, error}``
- 退出码：0 成功 / 1 失败 / 2 参数错 / 3 需要用户决定
- ``error.hint`` 直接写下一步该干什么，agent 不用猜

为什么不引 maa-cli：我们走的是「写配置 + 启动 MAA.exe」这条路，再引一套
平行的命令行驱动（MaaCore）会多一个 20–30MB 的维护面，而收益只有「无头
运行」——我们的用户本来就是开着 MAA 用的。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # backend/，与 main.py 一致
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import (accounts, agent, asstlog, maa, multiacct, progress,  # noqa: E402
                  resources, settings, watch)

EXIT_OK, EXIT_FAIL, EXIT_ARG, EXIT_DECIDE = 0, 1, 2, 3


class Fail(Exception):
    def __init__(self, message, code=EXIT_FAIL, hint=''):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _out(cmd, data=None, error=None):
    body = {'ok': error is None, 'command': cmd, 'data': data or {}}
    if error:
        body['error'] = error
    sys.stdout.write(json.dumps(body, ensure_ascii=False, indent=2) + '\n')
    sys.stdout.flush()


def _arg(argv, name, default=None):
    """取 --name value 或 --name=value"""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
        return default
    for a in argv:
        if a.startswith(name + '='):
            return a.split('=', 1)[1]
    return default


def _flag(argv, name):
    return name in argv


# 带取值的选项。算「位置参数」时必须跳过它们的值，否则
# ``ops --account 主号.json TL-5`` 会把账号名当成关卡关键词。
_VALUE_OPTS = ('--account', '--accounts', '--kind', '--limit', '--maa-dir',
               '--mode', '--names', '--stages')


def _positionals(argv):
    """argv 里真正的位置参数（跳过 --opt value 和 --opt=value）"""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in _VALUE_OPTS:
            skip = True
            continue
        if a.startswith('-'):
            continue
        out.append(a)
    return out


def _account(file):
    file = file or settings.load().get('lastAccount') or ''
    if file:
        return file
    rows = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    if not rows:
        raise Fail('还没有账号', EXIT_DECIDE, '先在界面里用森空岛 token 添加一个账号')
    return rows[0]['file']


# ---------------------------------------------------------------- 子命令
def cmd_accounts(argv):
    rows = []
    for c in accounts.list_accounts():
        p = c.get('progress') or {}
        rows.append({
            'file': c['file'],
            'name': c.get('alias') or c.get('nickName') or c['file'],
            'game': c.get('game') or 'arknights',
            'channel': c.get('channelName'), 'level': c.get('level'),
            'maaProfile': c.get('maaProfile') or '',
            'progress': progress.summary(p) if p else '',
        })
    return {'accounts': rows, 'count': len(rows)}


def cmd_stages(argv):
    pos = _positionals(argv)
    kw = pos[0] if pos else _arg(argv, '--keyword', '')
    if not kw:
        raise Fail('缺少关卡关键词', EXIT_ARG, '例如：stages TL- 或 stages 15-3')
    res = resources.resolve(kw)
    stages = [{'code': s['code'], 'stageId': s['stageId'],
               'name': s.get('name') or '', 'story': bool(s.get('story')),
               'difficulty': s.get('difficultyCN') or ''}
              for s in (res.get('stages') or [])[:40]]
    out = {'mode': res.get('mode'), 'title': res.get('title'), 'stages': stages}
    st = resources.stories_between(res.get('stages') or []) \
        if hasattr(resources, 'stories_between') else []
    if st:
        out['storyStages'] = [{'code': x['code'], 'name': x.get('name')} for x in st[:10]]
    if not stages and res.get('mode') != 'story':
        raise Fail('这个关键词没有对到关卡', EXIT_DECIDE,
                   '换个说法：关卡代号（TL-5）、章号（15）、活动代号（act55side）')
    return out


def cmd_ops(argv):
    from core import copilot
    pos = _positionals(argv)
    kw = pos[0] if pos else _arg(argv, '--keyword', '')
    if not kw:
        raise Fail('缺少关卡关键词', EXIT_ARG)
    file = _account(_arg(argv, '--account'))
    limit = int(_arg(argv, '--limit', '3'))
    snap = accounts.get_account(file)
    res = copilot.search(kw, snap, pages=1, limit=limit)
    rows = []
    for packed in (res.get('stages') or [])[:15]:
        st = packed.get('stage') or {}
        rows.append({
            'stage': st.get('code'), 'stageId': st.get('stageId'),
            'difficulty': st.get('difficultyCN') or '',
            'variant': st.get('variant'),
            'ops': [{'copilotId': o.get('copilotId'), 'rating': o.get('rating'),
                     'mode': o.get('mode'), 'title': (o.get('title') or '')[:50]}
                    for o in (packed.get('ops') or [])],
        })
    return {'account': file, 'keyword': kw, 'rows': rows}


def cmd_op(argv):
    from core import copilot
    cid = next((a for a in argv if a.isdigit()), '')
    if not cid:
        raise Fail('缺少作业 id', EXIT_ARG, 'ops 命令返回的 copilotId')
    file = _account(_arg(argv, '--account'))
    d = copilot.operation_detail(int(cid))
    content = d.get('content') or {}
    owned = None
    try:
        owned = copilot.build_owned(accounts.get_account(file))
    except Exception:
        pass
    opers = copilot.detail_opers(content, owned)
    return {
        'id': int(cid),
        'title': (content.get('doc') or {}).get('title') or '',
        'stage': content.get('stage_name') or '',
        'opers': [{'name': o.get('name'), 'owned': o.get('owned'),
                   'ok': o.get('ok'), 'elite': o.get('elite'),
                   'level': o.get('level')} for o in opers],
        'notOwned': [o.get('name') for o in opers if o.get('owned') is False],
    }


def cmd_progress(argv):
    file = _account(_arg(argv, '--account'))
    snap = accounts.get_account(file)
    prog = snap.get('progress') or {}
    if not prog:
        raise Fail('这个账号还没有进度数据', EXIT_DECIDE,
                   '在界面上点一次「同步全部账号」（需要森空岛 token）')
    return {'account': file, 'summary': progress.summary(prog),
            'main': {k: v for k, v in (prog.get('main') or {}).items()
                     if k != 'stages'},
            'campaignUnfinished': [c.get('name') for c in (prog.get('campaign') or [])
                                   if not c.get('full')],
            'activities': [{'name': a.get('name'), 'left': a.get('left')}
                           for a in (prog.get('activities') or []) if a.get('left')][:10]}


def cmd_status(argv):
    d = _arg(argv, '--maa-dir') or _default_maa_dir()
    snap = watch.snapshot(d)
    return {'maaDir': d, 'state': snap.get('state'), 'stage': snap.get('stage'),
            'index': snap.get('index'), 'node': snap.get('node'),
            'retry': snap.get('retry'), 'retryTotal': snap.get('retryTotal'),
            'explain': asstlog.explain(snap)}


def cmd_watch(argv):
    """读一次实时状态（不常驻）。外部 harness 自己定时调这个就行。"""
    d = _arg(argv, '--maa-dir') or _default_maa_dir()
    w = asstlog.Watcher(d or None, live=True)
    snap = w.poll()
    return {'maaDir': d, **{k: snap[k] for k in
                            ('state', 'stage', 'index', 'node', 'retry',
                             'retryTotal', 'since', 'error')},
            'explain': asstlog.explain(snap)}


def cmd_runs(argv):
    d = _arg(argv, '--maa-dir') or _default_maa_dir()
    rows = asstlog.failures(d, limit=int(_arg(argv, '--limit', '30')))
    return {'maaDir': d, 'runs': rows,
            'unfinished': [r for r in rows if not r.get('ok')]}


def cmd_plan(argv):
    pos = _positionals(argv)
    kw = pos[0] if pos else ''
    stages = _arg(argv, '--stages', '')
    # --stages 两种写法都见过：当关卡关键词（docs 里的示例）和当数量上限。
    # 数字＝上限，不是数字＝关键词；这样 ``plan --stages TL-5`` 不会再
    # 撞 int('TL-5') 报「invalid literal」。
    if stages and not stages.isdigit():
        kw = kw or stages
    if not kw:
        kw = _arg(argv, '--keyword', '')
    if not kw:
        raise Fail('缺少关卡关键词', EXIT_ARG,
                   '例如：plan TL-5，或 plan --stages TL-5 --limit 3')
    names = {}
    nf = _arg(argv, '--names')
    if nf and os.path.isfile(nf):
        with open(nf, encoding='utf-8') as f:
            names = json.load(f)
    accs = [a for a in (_arg(argv, '--accounts') or '').split(',') if a.strip()]
    limit = _arg(argv, '--limit', '') or (stages if stages.isdigit() else '') or '8'
    p = multiacct.plan({
        'keyword': kw,
        'kind': _arg(argv, '--kind', ''),
        'accounts': accs,
        'names': names,
        'limitStages': int(limit),
        'maaDir': _arg(argv, '--maa-dir') or '',
        'mode': _arg(argv, '--mode', ''),
    })
    if p.get('missing'):
        # 缺信息 = 需要用户回答，不是失败
        return p
    if not p.get('ok'):
        raise Fail('没有排出可执行的方案', EXIT_DECIDE,
                   '；'.join(p.get('warnings') or ['换个关卡关键词试试']))
    return p


def cmd_apply(argv):
    """执行 plan 生成的方案：apply plan.json [--no-start]"""
    path = next((a for a in argv if not a.startswith('-')), '')
    if not path or not os.path.isfile(path):
        raise Fail('需要方案文件', EXIT_ARG,
                   '先跑 plan 命令，把它的输出存成 json 再传进来')
    with open(path, encoding='utf-8') as f:
        p = json.load(f)
    p = p.get('data') or p
    if not p.get('accounts'):
        raise Fail('方案是空的', EXIT_ARG)
    res = multiacct.apply(p)
    if not _flag(argv, '--no-start'):
        rep = multiacct.run(p)
        return {'write': res, 'report': rep}
    return {'write': res}


def cmd_ask(argv):
    """一次性问助手一句话（非交互，给 harness 和脚本用）"""
    text = ' '.join(a for a in argv if not a.startswith('-')).strip()
    if not text:
        raise Fail('缺少要问的内容', EXIT_ARG, '例如：ask "我还差哪些关"')
    if not agent.status().get('configured'):
        raise Fail('助手还没配好', EXIT_DECIDE,
                   '在界面「设置 → 内置助手」里填 Base URL / 模型名 / API Key')
    s = agent.session()
    agent.chat(s.id, text)
    deadline = __import__('time').time() + 180
    while s.running and __import__('time').time() < deadline:
        __import__('time').sleep(0.4)
    says = [e.get('text') for e in s.events if e.get('kind') == 'say']
    return {'session': s.id, 'answer': '\n'.join(x for x in says if x),
            'events': [{'kind': e.get('kind'), 'text': (e.get('text') or '')[:200]}
                       for e in s.events if e.get('kind') in ('tool', 'error')],
            'pending': s.pending}


def _default_maa_dir():
    """MAA 目录：命令行参数 → 全局默认 → 第一个账号绑的（跟 GUI 一致）"""
    d = settings.load().get('maaDir') or ''
    if d:
        return d
    for c in accounts.list_accounts():
        d = c.get('maaDir') or ''
        if d:
            return d
    return ''


def cmd_doctor(argv):
    """环境自检：MAA 目录、配置、日志、账号、助手，一项一行"""
    st = settings.load()
    d = _arg(argv, '--maa-dir') or _default_maa_dir()
    items = []

    def add(name, ok, detail, hint=''):
        items.append({'item': name, 'ok': bool(ok), 'detail': detail, 'hint': hint})

    add('MAA 目录', bool(d) and os.path.isdir(d), d or '（未设置）',
        '设置 → MAA 目录，选 MAA.exe 所在文件夹')
    cfg_ok = bool(d) and os.path.isfile(os.path.join(d, 'config', 'gui.new.json'))
    add('MAA 配置', cfg_ok, 'config/gui.new.json',
        'MAA 至少启动过一次并保存过配置才会生成')
    logs = asstlog.log_paths(d)
    add('运行日志', bool(logs['asst']) and os.path.isfile(logs['asst']),
        logs['asst'] or '（无）', 'MAA 跑过一次任务就有')
    accs = [c for c in accounts.list_accounts()
            if (c.get('game') or 'arknights') == 'arknights']
    add('账号', bool(accs), '%d 个明日方舟账号' % len(accs),
        '用森空岛 token 添加，或从 MAA 导入练度')
    withprog = sum(1 for c in accs if c.get('progress'))
    add('进度数据', bool(withprog), '%d/%d 个账号有进度' % (withprog, len(accs)),
        '点「同步全部账号」拉森空岛进度')
    a = agent.status()
    add('内置助手', a['configured'],
        '模型 %s' % (a.get('model') or '（未填）'),
        '设置 → 内置助手，填 Base URL / 模型名 / API Key')
    if cfg_ok:
        try:
            p = maa.profiles(d)
            add('配置档案', bool(p['profiles']), '%d 个：%s' % (
                len(p['profiles']), '、'.join(x['name'] for x in p['profiles'][:6])),
                'MAA 里至少要有 Default 档案')
        except Exception as e:
            add('配置档案', False, str(e), '检查 MAA 目录')
    bad = [x for x in items if not x['ok']]
    return {'ok': not bad, 'items': items,
            'ready': not bad,
            'hint': '' if not bad else '有 %d 项没就绪，按 hint 逐项处理' % len(bad)}


COMMANDS = {
    'accounts': (cmd_accounts, '列出所有账号'),
    'stages': (cmd_stages, '关卡关键词 → 具体关卡（stages TL-）'),
    'ops': (cmd_ops, '查这些关卡有哪些作业（ops TL-5 --account x.json）'),
    'op': (cmd_op, '看一份作业缺什么（op 12345）'),
    'progress': (cmd_progress, '账号的游戏进度'),
    'status': (cmd_status, 'MAA 现在跑到哪了'),
    'watch': (cmd_watch, '读一次实时状态'),
    'runs': (cmd_runs, '最近几次跑机记录（哪几关没跑完）'),
    'plan': (cmd_plan, '排多账号方案（plan TL- --kind activity）'),
    'apply': (cmd_apply, '执行方案（apply plan.json）'),
    'ask': (cmd_ask, '问内置助手一句话'),
    'doctor': (cmd_doctor, '环境自检'),
}


def usage():
    lines = ['MAA-PRTS 命令行', '',
             '用法：python -m backend.cli <命令> [参数]', '']
    for k, (_, desc) in COMMANDS.items():
        lines.append('  %-10s %s' % (k, desc))
    lines += ['', '所有命令输出 JSON：{ok, command, data, error}',
              '退出码 0 成功 / 1 失败 / 2 参数错 / 3 需要用户决定']
    return '\n'.join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(usage())
        return EXIT_OK
    cmd = argv[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        _out(cmd, error={'message': '没有这个命令：%s' % cmd,
                         'hint': '可用命令见 --help'})
        return EXIT_ARG
    try:
        data = fn[0](argv[1:])
        _out(cmd, data)
        # plan 缺信息时用 3 退出，让上层知道「该问用户了」
        if cmd == 'plan' and isinstance(data, dict) and data.get('missing'):
            return EXIT_DECIDE
        return EXIT_OK
    except Fail as e:
        _out(cmd, error={'message': str(e), 'hint': e.hint})
        return e.code
    except Exception as e:
        _out(cmd, error={'message': '%s: %s' % (type(e).__name__, e),
                         'hint': '这是意外错误，把这条消息发给开发者'})
        return EXIT_FAIL


if __name__ == '__main__':
    sys.exit(main())
