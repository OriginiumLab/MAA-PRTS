# -*- coding: utf-8 -*-
"""HTTP API：给前端页面用的 JSON 接口"""
from __future__ import annotations

import json
import os
import traceback
import urllib.parse

from . import (accounts, appupdate, copilot, maa, operbox, packsync, paradox,
               resources, settings, sklogin, tasks, version)

WEB = settings.static_dir()

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
}


def _account_for(file):
    if not file:
        last = settings.load().get('lastAccount')
        rows = accounts.list_accounts()
        if last and any(r['file'] == last for r in rows):
            return last
        if rows:
            return rows[0]['file']
        raise RuntimeError('还没有账号，先用森空岛 token 添加一个')
    return file


# ---------------------------------------------------------------- 路由
def bootstrap(_q, _body):
    st = settings.load()
    accs = accounts.list_accounts()
    # 状态卡按「生效账号」（上次账号 → 列表首个）绑定的 MAA 展示，
    # 账号没绑定才回退全局默认。首启 settings 里还没有 lastAccount，
    # 也要看首个账号的绑定——否则账号明明配好了 MAA 仍会误判 needsMaa
    # 弹出首次引导
    last = _account_for_last(st, accs)
    status = maa.status(_maa_dir_for(last))
    acts = resources.activity_list(18)
    sv = _settings_view(st, accs)
    return {
        # 全量 settings 视图（均非敏感字段）：不再维护键清单白名单——
        # 清单漏一个键（如 opBlacklist），前端以这份子集为基底整键保存
        # 就会把磁盘上已有的黑名单覆盖清空（#45）
        'settings': sv,
        'accounts': accs,
        'lastAccount': last,
        'maa': status,
        'activities': acts,
        'paths': {
            'root': settings.project_root(),
            'accounts': settings.accounts_dir(),
            'tokens': settings.tokens_file(),
            'config': settings.CONFIG_DIR,
        },
        'pack': resources.pack_info(),
        'gamedata': version.check(acts),
        'setup': {
            # 首次启动引导：没账号 = 还没填 token；MAA 路径无效 = 还没填根目录
            'needsToken': not accs,
            'needsMaa': not status.get('exeExists'),
        },
        'version': version.app_version(),
    }


def _account_for_last(st, accs):
    last = st.get('lastAccount')
    if last and any(a['file'] == last for a in accs):
        return last
    return accs[0]['file'] if accs else ''


def _maa_dir_for(file):
    """账号绑定的 MAA 根目录（账号 json 的 maa.dir）；
    没绑定、账号读不到就回退全局默认 settings.maaDir。"""
    if file:
        try:
            snap = accounts.get_account(file)
            d = settings._norm_dir((snap.get('maa') or {}).get('dir') or '')
            if d:
                return d
        except Exception:
            pass
    return settings.load()['maaDir']


def _settings_view(st, accs):
    """settings 视图：maaDirs = 保存历史 ∪ 各账号绑定的目录。
    「已存过」的真实语义是「用过的 MAA」——老版本只存历史时会漏掉
    已绑定进账号的目录，这里纯读合并（顺带路径归一），不写盘。"""
    dirs, seen = [], set()
    for d in list(st.get('maaDirs') or []) + [a.get('maaDir') or '' for a in accs]:
        d = settings._norm_dir(d)
        if d and os.path.normcase(d) not in seen:
            seen.add(os.path.normcase(d))
            dirs.append(d)
    out = dict(st)
    out['maaDirs'] = dirs
    return out


def api_settings(_q, body):
    patch = body or {}
    st = settings.save(patch) if patch else settings.load()
    accs = accounts.list_accounts()
    # 与 bootstrap 同口径：按生效账号（上次 → 首个）绑定的 MAA 出状态
    status = maa.status(_maa_dir_for(_account_for_last(st, accs)))
    return {'settings': _settings_view(st, accs), 'maa': status}


def api_accounts(_q, _body):
    return {'accounts': accounts.list_accounts()}


def api_account(q, _body):
    file = _account_for(q.get('file'))
    return accounts.account_detail(file)


def api_account_maa(_q, body):
    body = body or {}
    patch = dict(body.get('patch') or {})
    if 'dir' in patch:
        # 路径归一后落盘；绑定的新 MAA 同时并入「已存过」历史
        patch['dir'] = settings._norm_dir(patch.get('dir') or '')
        if patch['dir']:
            settings.remember_maa_dir(patch['dir'])
    return {'maa': accounts.save_maa_meta(body['file'], patch)}


def api_account_pref(_q, body):
    """账号级用户偏好写回账号 json。body: {"file": ..., "preferAlgo": rating/fewer/single}"""
    body = body or {}
    algo = (body.get('preferAlgo') or '').strip()
    if algo not in copilot.ALGOS:
        raise ValueError('未知算法：%s' % (algo or '(空)'))
    return {'pref': accounts.save_pref(_account_for(body.get('file')), {'preferAlgo': algo})}


def api_account_remove(_q, body):
    return {'removed': accounts.remove_account((body or {}).get('file'))}


def api_account_import_maa(_q, body):
    """从账号绑定的 MAA 目录导入干员识别结果（R2/R3）

    body: {"file": "<账号文件名>"}（可省略，按上次账号→列表首个解析）。
    同步任务，无 taskId；结果摘要（条数/来源/警告）直接返回给前端。
    """
    body = body or {}
    file = _account_for(body.get('file'))
    d = _maa_dir_for(file)
    if not d:
        raise ValueError('该账号没有绑定 MAA 目录，也没有设置全局默认')
    return operbox.import_into(file, d)


def api_account_create_from_maa(_q, body):
    """不用森空岛 token：用 MAA 目录的干员识别结果直接建本地账号

    body: {"dir": "<MAA 根目录>", "alias": "<备注，可选>"}。
    同步任务，无 taskId；建号摘要（文件名/条数/警告）直接返回给前端。
    """
    body = body or {}
    d = settings._norm_dir((body.get('dir') or '').strip())
    if not d:
        raise ValueError('缺少 MAA 目录参数')
    settings.remember_maa_dir(d)
    return operbox.create_from_dir(d, (body.get('alias') or '').strip())


def api_refresh(_q, body):
    body = body or {}
    token = (body.get('token') or '').strip() or None
    tid = accounts.start_refresh(token=token, alias=(body.get('alias') or '').strip() or None,
                                 with_skins=bool(body.get('withSkins')))
    return {'taskId': tid}


def api_search(_q, body):
    body = body or {}
    kw = (body.get('q') or '').strip()
    if not kw:
        raise ValueError('请输入关卡代号（如 TL-10）、活动（act55side / 残照终尽）或关卡系列（TR-）')
    file = _account_for(body.get('file'))
    snap = accounts.get_account(file)
    res = copilot.search(kw, snap, pages=int(body.get('pages') or 2),
                         min_rating=int(body.get('minRating') or 0),
                         limit=int(body.get('limit') or 12))
    res['file'] = file
    return res


def api_activities(_q, _body):
    return {'activities': resources.activity_list(24)}


def api_gamedata(_q, _body):
    """资源包版本状态（含是否有待提示的更新）"""
    return version.check(resources.activity_list(18))


def api_gamedata_ack(_q, _body):
    """用户点「知道了」之后清掉更新提示"""
    return version.ack(resources.activity_list(18))


def api_pack_status(q, _body):
    """资源包有没有新版本（只查不下载；force=1 时给更长的超时）"""
    force = str((q or {}).get('force') or '').lower() in ('1', 'true', 'yes')
    return packsync.status(timeout=8 if force else 4)


def api_pack_update(_q, _body):
    """下载并安装新的资源包（换完立即重算「新活动」提示）"""
    r = packsync.apply()
    acts = resources.activity_list(18)
    return {'result': r, 'pack': resources.pack_info(),
            'gamedata': version.check(acts)}


def api_app_status(q, _body):
    """程序本体有没有新版本（只查不下载；force=1 跳过 6 小时节流）"""
    force = str((q or {}).get('force') or '').lower() in ('1', 'true', 'yes')
    return appupdate.status(force=force)


def api_proxy_probe(_q, body):
    """设置页「保存」后的代理检测：端口开着吗 + 经它摸得到 GitHub 吗

    body: {"proxy": "http://127.0.0.1:7890"}（前端拼好的完整串，未落盘）。
    探测结果不改变保存行为——代理不可用时 net.open 会自动回退直连。
    """
    from . import net
    p = settings._norm_proxy(str((body or {}).get('proxy') or ''))
    if not p:
        raise ValueError('先填代理地址和端口')
    return net.check(p)


def api_chars_suggest(q, _body):
    """干员联想：拼音首字母（yh→银灰）或名字片段"""
    return {'items': resources.suggest_chars((q or {}).get('q') or '')}


def api_author_suggest(q, _body):
    """按名字反查作业站作者（document 全文搜 + uploader 聚合）"""
    return {'items': copilot.suggest_authors((q or {}).get('q') or '')}


def api_app_download(_q, _body):
    """下载安装包并校验（不替换，等用户点「重启并安装」）"""
    return appupdate.download()


def api_app_install(_q, _body):
    """解压 + 拉起安装脚本 + 让程序退出；前端会看到窗口自己关掉"""
    return appupdate.install()


def api_app_open(q, _body):
    """用系统浏览器打开 Release 页（前端跨域拿不到）"""
    return appupdate.open_page((q or {}).get('url') or '')


def api_op(q, _body):
    cid = q.get('id')
    if not cid:
        raise ValueError('缺少作业 id')
    d = copilot.operation_detail(cid)
    content = d['content']
    # 带上 file 就按该账号的练度数据决定要不要展示技能/模组要求：
    # MAA 导入的账号没有技能数据，这些维度不参与判定，也不该出现在详情里
    owned = None
    file = (q.get('file') or '').strip()
    if file:
        try:
            owned = copilot.build_owned(accounts.get_account(file))
        except Exception:
            owned = None
    return {
        'id': int(cid),
        'content': content,
        'maaCode': 'maa://%s' % cid,
        'title': (content.get('doc') or {}).get('title'),
        'opers': copilot.detail_opers(content, owned),
        'actions': len(content.get('actions') or []),
        'stageName': content.get('stage_name'),
    }


def api_maa_status(q, _body):
    """带 file 参数时返回该账号绑定的 MAA 的状态，否则全局默认"""
    return maa.status(_maa_dir_for(q.get('file')))


def api_maa_running(_q, _body):
    """正在运行的 MAA 进程（首次引导从进程反查安装目录）"""
    return {'list': maa.list_running()}


def api_skland_login(_q, _body):
    """打开内置森空岛登录窗口，登录成功自动抓 token"""
    return sklogin.start()


def api_skland_login_status(_q, _body):
    return sklogin.status()


def api_skland_login_cancel(_q, _body):
    return sklogin.cancel()


def api_maa_probe(q, _body):
    """检测一个目录是不是 MAA 根目录（首次引导 / 设置里用）"""
    d = (q.get('dir') or '').strip()
    if not d:
        raise ValueError('缺少目录参数')
    exe = os.path.join(d, 'MAA.exe')
    cfg_path = os.path.join(d, 'config', 'gui.new.json')
    profiles = []
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            profiles = list((cfg.get('Configurations') or {}).keys())
        except Exception:
            pass
    ob = operbox.probe(d)
    return {
        'dir': d,
        'exeExists': os.path.isfile(exe),
        'configExists': os.path.isfile(cfg_path),
        'profiles': profiles,
        'operbox': ob['exists'],
        'operboxCount': ob['count'],
        'operboxSyncTime': ob['syncTime'],
    }


def api_maa_close(q, body):
    body = body or {}
    d = _maa_dir_for(q.get('file') or body.get('file'))
    return {'closed': maa.close(d), 'running': bool(maa.running_in(d))}


def api_maa_open(q, body):
    body = body or {}
    return {'started': maa.launch(_maa_dir_for(q.get('file') or body.get('file')))}


def api_maa_apply(_q, body):
    body = body or {}
    items = body.get('items') or []
    if not items:
        raise ValueError('作业集是空的')
    tid = maa.start_apply(items, profile=body.get('profile'),
                          mode=body.get('mode') or 'replace',
                          auto_start=body.get('autoStart', True),
                          maa_dir=_maa_dir_for(body.get('file')))
    return {'taskId': tid}


def api_maa_clear(q, body):
    body = body or {}
    return {'cleared': maa.clear_tasklist(body.get('profile'),
                                          maa_dir=_maa_dir_for(q.get('file') or body.get('file')))}


def api_maa_tasklist(q, _body):
    return maa.tasklist(q.get('profile'), maa_dir=_maa_dir_for(q.get('file')))


def api_paradox_plan(q, body):
    """当前账号的悖论模拟清单（练度推算，不打任何图片主意）"""
    body = body or {}
    file = _account_for(q.get('file') or body.get('file'))
    snap = accounts.get_account(file)
    out = paradox.plan(snap)
    out['file'] = file
    return out


def api_paradox_pick(_q, body):
    """按选中的关卡号批量取作业（每关一份最优）"""
    body = body or {}
    file = _account_for(body.get('file'))
    stages = body.get('stages') or []
    if not stages:
        raise ValueError('没有选中任何悖论关卡')
    snap = accounts.get_account(file)
    res = paradox.pick(stages, snap)
    res['file'] = file
    return res


def api_paradox_mark(_q, body):
    """记下这些悖论已经排进过 MAA，下次默认不勾"""
    body = body or {}
    file = _account_for(body.get('file'))
    snap = accounts.get_account(file)
    imp = paradox.mark_imported(snap, body.get('stages') or [])
    accounts.save_account(file, snap)
    return {'file': file, 'imported': len(imp)}


def api_task(q, _body):
    tid = q.get('id')
    t = tasks.get(tid, int(q.get('since') or 0))
    if not t:
        raise FileNotFoundError('任务不存在或已过期')
    return t


def api_export(_q, body):
    """导出作业站「本地导入」用的干员 JSON"""
    body = body or {}
    file = _account_for(body.get('file'))
    snap = accounts.get_account(file)
    data = []
    for c in snap.get('chars') or []:
        if not c.get('charId') or not c.get('level'):
            continue
        equips = []
        for m in c.get('modules') or []:
            t = copilot._module_type(m.get('id'))
            if t:
                equips.append({'type': t, 'level': int(m.get('level') or 0)})
        data.append({
            'id': c['charId'], 'name': c.get('name'), 'rarity': int(c.get('rarity') or 0),
            'evolvePhase': int(c.get('elite') or 0), 'level': int(c.get('level') or 0),
            'mainSkillLevel': int(c.get('skillLevel') or 0),
            'skills': [{'level': int(s.get('mastery') or 0)} for s in (c.get('skills') or [])],
            'equips': equips,
        })
    out_dir = os.path.join(settings.accounts_dir(), '作业站')
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(file)[0]
    path = os.path.join(out_dir, '作业站干员_%s.json' % stem)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'code': 200, 'message': 'ok', 'data': data}, f, ensure_ascii=False, indent=2)
    return {'path': path, 'count': len(data)}


ROUTES = {
    '/api/bootstrap': bootstrap,
    '/api/settings': api_settings,
    '/api/accounts': api_accounts,
    '/api/account': api_account,
    '/api/account/maa': api_account_maa,
    '/api/account/pref': api_account_pref,
    '/api/account/remove': api_account_remove,
    '/api/account/refresh': api_refresh,
    '/api/account/import-maa': api_account_import_maa,
    '/api/account/create-from-maa': api_account_create_from_maa,
    '/api/search': api_search,
    '/api/activities': api_activities,
    '/api/gamedata': api_gamedata,
    '/api/gamedata/ack': api_gamedata_ack,
    '/api/proxy/probe': api_proxy_probe,
    '/api/chars/suggest': api_chars_suggest,
    '/api/author/suggest': api_author_suggest,
    '/api/pack/status': api_pack_status,
    '/api/pack/update': api_pack_update,
    '/api/app/status': api_app_status,
    '/api/app/download': api_app_download,
    '/api/app/install': api_app_install,
    '/api/app/open': api_app_open,
    '/api/op': api_op,
    '/api/maa/status': api_maa_status,
    '/api/maa/running': api_maa_running,
    '/api/maa/probe': api_maa_probe,
    '/api/skland/login': api_skland_login,
    '/api/skland/login/status': api_skland_login_status,
    '/api/skland/login/cancel': api_skland_login_cancel,
    '/api/maa/close': api_maa_close,
    '/api/maa/open': api_maa_open,
    '/api/maa/apply': api_maa_apply,
    '/api/maa/clear': api_maa_clear,
    '/api/maa/tasklist': api_maa_tasklist,
    '/api/task': api_task,
    '/api/export': api_export,
    '/api/paradox/plan': api_paradox_plan,
    '/api/paradox/pick': api_paradox_pick,
    '/api/paradox/mark': api_paradox_mark,
}
