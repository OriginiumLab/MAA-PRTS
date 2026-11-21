# -*- coding: utf-8 -*-
"""HTTP API：给前端页面用的 JSON 接口"""
from __future__ import annotations

import json
import os
import traceback
import urllib.parse

from . import (accounts, agent, appupdate, asstlog, copilot, gacha, garrison,
               maa, maaplan, multiacct, operbox, packsync, paradox, resources,
               scheduler, settings, signin, skills, sklogin, tasks, updater,
               version, watch)

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
            'logs': settings.DEBUG_DIR,
            'appLog': settings.LOG_PATH,
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


def _maa_dir_any(file=None):
    """比 _maa_dir_for 多兜一层：全局没设就用「上次账号」绑定的那个

    只读接口（观测、档案列表）在用户没配全局默认、只给账号绑了目录时
    也要能工作——不然新用户一打开助手/状态条全是空的。
    """
    d = _maa_dir_for(file)
    if d:
        return d
    for cand in (settings.load().get('lastAccount'), None):
        if cand is None:
            break
        d = _maa_dir_for(cand)
        if d:
            return d
    return ''


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
    if 'profile' in patch:
        # 配置文件（MAA 的 Configuration）：只记名字，**不读写用户的档案内容**。
        # 切号名优先按这份档案里「开始唤醒」的 AccountName 来（acctbind.resolve）。
        patch['profile'] = str(patch.get('profile') or '').strip()[:60]
    if 'accountNameFrom' in patch:
        v = str(patch.get('accountNameFrom') or '').strip().lower()
        patch['accountNameFrom'] = v if v in ('manual', 'profile') else ''
    if 'accountName' in patch:
        # 切号名统一走 acctname：用户给「5505」「181 5505」「181****5505」
        # 「157****3635」都能吃下，官服填了昵称则当场拦下——这一格填错的
        # 代价是 MAA 在账号列表里死找、4 分钟超时后整轮挂机废掉。
        # 留空是合法状态（还没填 / 想清掉），不能当成「填错」拦下。
        from . import acctname
        raw = (patch.get('accountName') or '').strip()
        if raw:
            snap = accounts.get_account(body['file'])
            channel = (snap.get('account') or {}).get('channelName') or ''
            name, why = acctname.check(raw, channel)
            if why:
                raise ValueError('切号名不对：%s' % why)
            patch['accountName'] = name
            # 没明说来源的一律按手填：老调用方（CLI/脚本）给的值不能被
            # 绑定的配置文件悄悄盖回去
            patch.setdefault('accountNameFrom', 'manual')
        else:
            patch['accountName'] = ''
    return {'maa': accounts.save_maa_meta(body['file'], patch)}


def api_account_token(_q, body):
    """设置 → 账号管理：读某个账号的 token（明文，仅在本地窗口里展示）

    明文只走「本机 HTTP + 桌面窗口」这条链路，不落日志、不进 bootstrap。
    界面默认只显示尾 4 位，用户点「显示」才把明文填进输入框。
    """
    body = body or {}
    file = _account_for(body.get('file'))
    st = accounts.token_state(file)
    return {
        'file': file,
        'has': st['has'],
        'broken': st['broken'],
        'error': st['error'],
        'token': st['token'],
        'mask': updater.mask(st['token']) if st['token'] else '',
    }


def api_account_token_check(_q, body):
    """token 还能不能用（换 grant 校验）；返回 ok / 原因

    只有「确认失效」才让界面解锁修改框：网络不通、森空岛抽风都返回
    ok=None（未知），不然一断网就把所有账号的 token 都变成可改状态。
    """
    body = body or {}
    file = _account_for(body.get('file'))
    token = accounts.token_for(file)
    if not token:
        st = accounts.token_state(file)
        return {'ok': False, 'error': st['error'] or '这个账号没有存 token'}
    ok, why = sklogin.verify_token(token)
    return {'ok': bool(ok), 'error': '' if ok else (why or 'token 已失效')}


def api_account_token_set(_q, body):
    """换 token：先验证 + 核对 uid，确认是同一个号再写回账号文件

    不核 uid 的话，用户粘错一个别的号的 token 会静默把凭证换掉——下次
    同步直接多出一个账号、原账号还是失效状态，很难查。
    """
    body = body or {}
    file = _account_for(body.get('file'))
    token = (body.get('token') or '').strip()
    if not token:
        raise ValueError('token 不能为空')
    snap = accounts.get_account(file)
    mine = str((snap.get('account') or {}).get('uid') or '')
    game = (snap.get('account') or {}).get('game') or 'arknights'
    try:
        from . import skland
        sk = skland.Skland(token)
        uids = {str(b.get('uid')) for b in sk.bindings() if not b.get('isDelete')}
    except Exception as e:
        msg = str(e)
        if any(code in msg for code in ('400', '401', '403')):
            msg = '这个 token 无效或已过期（森空岛拒绝了它）'
        raise ValueError('token 验证失败：%s' % msg)
    if game == 'endfield':
        # 终末地没有 arknights binding：角色 id / 账号 uid 任一能对上就算同一个号
        try:
            for r in gacha.endfield_roles(token):
                uids.add(str(r.get('uid')))
                uids.add(str(r.get('roleId')))
        except Exception:
            pass
    if mine and mine not in uids:
        raise ValueError('这个 token 绑的不是当前账号（uid %s 不在它的角色列表里），'
                         '要加新号请用左上角「添加账号」' % mine)
    accounts.set_token(file, token)
    return {'file': file, 'mask': updater.mask(token), 'ok': True}


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

    body: {"file": "<账号文件名>", "force": true}（file 可省略，按上次账号→
    列表首个解析）。force 只给「账号页手动导入」用——用户在弹窗里点了确认，
    才算同意用识别结果覆盖森空岛同步的练度；向导里顺手导入不带 force，
    会拿到 skippedBy=token 而不动数据。
    同步任务，无 taskId；结果摘要（条数/来源/警告）直接返回给前端。
    """
    body = body or {}
    file = _account_for(body.get('file'))
    d = _maa_dir_for(file)
    if not d:
        raise ValueError('该账号没有绑定 MAA 目录，也没有设置全局默认')
    force = bool(body.get('force'))
    return operbox.import_into(file, d, force=force)


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
        # 没选账号 / 账号没练度数据时 owned 是 None，前端就不标「缺谁」，
        # 否则整份作业都会被算成缺卡
        'compared': owned is not None,
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
    """检测一个目录是不是 MAA / MAAEND 根目录（首次引导 / 设置里用）

    kind=endfield 时按 MAAEND 的主程序名去找：终末地那套是另一个程序，
    主程序可能叫 MaaEnd.exe 或 mxu.exe（三月七脚本里两种都出现过）。这一步
    只是把路径先管起来，MAAEND 的任务链还没接。
    """
    d = (q.get('dir') or '').strip()
    if not d:
        raise ValueError('缺少目录参数')
    kind = (q.get('kind') or 'arknights').strip().lower()
    names = (['MaaEnd.exe', 'MAAEND.exe', 'mxu.exe', 'MAA.exe']
             if kind == 'endfield' else ['MAA.exe'])
    exe_name = next((n for n in names if os.path.isfile(os.path.join(d, n))), '')
    exe = os.path.join(d, exe_name or names[0])
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
        'exeExists': bool(exe_name),
        'exeName': exe_name,
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


def api_copilot_prepare(_q, body):
    """加入作业集时的隐式预下载：把「下载 + 修正」挪到加入那一刻后台做，
    点「写入 MAA」时直接取现成的（没预到的现场兜底）"""
    return {'queued': copilot.prepare((body or {}).get('ids') or [])}


def api_maa_apply(_q, body):
    body = body or {}
    items = body.get('items') or []
    if not items:
        raise ValueError('作业集是空的')
    tid = maa.start_apply(items, profile=body.get('profile'),
                          mode=body.get('mode') or 'replace',
                          auto_start=body.get('autoStart', True),
                          maa_dir=_maa_dir_for(body.get('file')),
                          # 带上账号：写盘时按该账号练度做干员替换（#92）
                          account=str(body.get('file') or '').strip() or None)
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


def _gacha_ctx(file):
    """抽卡要用到的三样东西：账号快照、游戏/uid 元信息、解密后的 token"""
    snap = accounts.get_account(file)
    a = snap.get('account') or {}
    meta = {
        'game': a.get('game') or 'arknights',
        'uid': str(a.get('uid') or ''),
        'nickName': a.get('nickName') or os.path.splitext(file)[0],
    }
    return snap, meta, accounts.token_for(file)


def api_gacha(q, _body):
    """抽卡统计视图（只读缓存，不发网络请求）"""
    file = _account_for(q.get('file'))
    _snap, meta, token = _gacha_ctx(file)
    return gacha.view(file, token, meta)


def api_gacha_sync(_q, body):
    """同步抽卡记录（后台任务，前端轮询 /api/task）"""
    file = _account_for((body or {}).get('file'))
    _snap, meta, token = _gacha_ctx(file)
    if not token:
        raise ValueError('该账号没有森空岛 token（MAA 导入的账号读不到抽卡记录），'
                         '请用「添加账号 → 森空岛 token」重新同步一次')
    return {'taskId': gacha.start_sync(file, token, meta)}


def api_signin_run(_q, body):
    """森空岛签到（后台任务，日志在前端面板里轮询）

    body: {"file": "<账号文件>"} —— 空 / 不传 = 全部账号（明日方舟 + 终末地）
    """
    file = ((body or {}).get('file') or '').strip()
    if file:
        file = _account_for(file)
    return {'taskId': signin.start(file)}


def api_paradox_mark(_q, body):
    """记下这些悖论已经排进过 MAA，下次默认不勾"""
    body = body or {}
    file = _account_for(body.get('file'))
    imp = {}

    def _mut(snap):
        imp.update(paradox.mark_imported(snap, body.get('stages') or []))

    accounts.update(file, _mut)   # 锁内读-改-写，不与后台同步丢更新（#47）
    return {'file': file, 'imported': len(imp)}


# ---------------------------------------------------------------- 卫戍协议
def api_garrison_status(q, _body):
    """速刷面板状态：环境（Node/核心）+ 从 MAA 配置抄的默认值 + 运行态

    会懒启动该账号的速刷后端（首次打开面板慢几秒是正常的）。
    核心缺失时附带下载进度（needsDownload / download），前端自动开始下载。
    """
    return garrison.status(_account_for(q.get('file')))


def api_garrison_download(_q, _body):
    """开始/继续核心包下载（幂等），返回当前下载状态"""
    return garrison.start_download()


def api_garrison_opts(_q, body):
    """保存用户改过的速刷选项（按账号持久化，只存与 MAA 默认不同的键）"""
    body = body or {}
    return garrison.save_opts(_account_for(body.get('file')), body.get('options') or {})


def _garrison_options(q, body):
    """test/start/resume 共用：账号 + 前端提交的连接选项"""
    body = body or {}
    file = _account_for(q.get('file') or body.get('file'))
    return file, body.get('options') or {}


def api_garrison_test(q, body):
    """测试连接（截图 + OCR + 活动页自检，只读不点击）"""
    file, opts = _garrison_options(q, body)
    return garrison.test(file, opts)


def api_garrison_start(q, body):
    """开始速刷（游戏先停在卫戍协议活动主界面）"""
    file, opts = _garrison_options(q, body)
    return garrison.start(file, opts)


def api_garrison_resume(q, body):
    """从该账号的上次检查点恢复"""
    file, opts = _garrison_options(q, body)
    return garrison.resume(file, opts)


def api_garrison_stop(q, body):
    body = body or {}
    return garrison.stop(_account_for(q.get('file') or body.get('file')))


def api_task(q, _body):
    tid = q.get('id')
    t = tasks.get(tid, int(q.get('since') or 0))
    if not t:
        raise FileNotFoundError('任务不存在或已过期')
    return t


# ---------------------------------------------------------------- 观测（M2/M3）
def api_watch_status(q, _body):
    """MAA 现在跑到哪了。没开监控也能给个实时读数（前端一打开就要显示）"""
    d = q.get('maaDir') or _maa_dir_any(q.get('file'))
    return watch.snapshot(d)


def api_watch_start(_q, body):
    body = body or {}
    d = body.get('maaDir') or _maa_dir_any(body.get('file'))
    w = watch.start(d, profile=body.get('profile'),
                    auto_resume=body.get('autoResume', True),
                    on_log=(lambda m: None))
    return {'watching': w.alive(), 'maaDir': d,
            'autoResume': w.auto_resume}


def api_watch_stop(_q, body):
    body = body or {}
    d = body.get('maaDir') or _maa_dir_any(body.get('file'))
    return {'stopped': watch.stop(d)}


def api_runs(q, _body):
    """最近几次跑机记录：跑了哪几关、哪关没跑完（看历史日志，不动实时游标）"""
    d = q.get('maaDir') or _maa_dir_any(q.get('file'))
    return {'runs': asstlog.failures(d, limit=int(q.get('limit') or 30))}


def api_profiles(q, _body):
    """MAA 有哪些配置档案、各自绑的账号名（多账号的底座）"""
    return maa.profiles(q.get('maaDir') or _maa_dir_any(q.get('file')))


# ---------------------------------------------------------------- 多账号
def api_multi_candidates(_q, _body):
    """左侧能挂机的账号（只含明日方舟，终末地不掺和）"""
    return {'accounts': multiacct.candidates()}


def api_multi_plan(_q, body):
    body = body or {}
    return multiacct.plan({
        'accounts': body.get('accounts') or [],
        'names': body.get('names') or {},
        'keyword': body.get('keyword') or '',
        'kind': body.get('kind') or '',
        'limitStages': body.get('limitStages') or 8,
        'maaDir': _maa_dir_any(body.get('file')),
        'mode': body.get('mode') or '',
    })


def api_multi_apply(_q, body):
    body = body or {}
    plan_data = body.get('plan') or {}
    rows = [a for a in (plan_data.get('accounts') or []) if a.get('ops')]
    if not rows:
        raise ValueError('方案是空的（没有可执行的作业）')
    d = _maa_dir_any(body.get('file'))
    plan_data['maaDir'] = d
    tid = tasks.create('多账号挂机（%d 个账号）' % len(rows))
    start = body.get('start', True)
    tl = plan_data.get('tasks') or (
        ['startup', 'copilot', 'mall', 'award'] if start else ['startup'])

    def go():
        try:
            if not start:
                # 只把档案配好、作业落盘，不开跑
                res = multiacct.apply(plan_data, tid)
                tasks.finish(tid, result=res)
                return
            # 跑：直连 MaaCore（GUI 的任务队列不认 Copilot，写配置跑不起来）
            res = maaplan.run_plan(rows, {
                'tasks': tl,
                'stages': plan_data.get('fightStages') or [],
                'copilot': {'formation': bool(plan_data.get('formation', True))},
                'maaDir': d,
            }, tid)
            tasks.finish(tid, result=res)
        except Exception as e:
            import traceback as _tb
            tasks.finish(tid, error='%s: %s' % (type(e).__name__, e),
                         result={'traceback': _tb.format_exc()})
    import threading as _th
    _th.Thread(target=go, daemon=True, name='multi-apply').start()
    return {'taskId': tid}


# ---------------------------------------------------------------- 任务编排
def api_maa_capabilities(q, _body):
    """这台 MAA 能配哪些任务 + 预设（前端配置面板的选项来源）"""
    return maaplan.capabilities(q.get('maaDir') or _maa_dir_any(q.get('file')))


def api_maa_profile(q, _body):
    """看某个档案现在配了什么"""
    return maaplan.describe(q.get('profile'),
                            q.get('maaDir') or _maa_dir_any(q.get('file')))


def api_maa_build(_q, body):
    """按 spec 建一份 PRTS- 档案（不启动），返回档案名

    想直接开跑用 ``/api/multi/apply``（直连 MaaCore）；这个接口只写配置，
    给「先在 MAA 里看看配得对不对」用。
    """
    body = body or {}
    spec = dict(body)
    spec['maaDir'] = _maa_dir_any(body.get('file'))
    return maaplan.ensure_profile(spec, spec['maaDir'])


# ---------------------------------------------------------------- 内置助手（M4）
def api_agent_status(_q, _body):
    return agent.status()


def api_agent_config(_q, body):
    """读 / 写助手配置。apiKey 只进不出——回给前端的是 hasKey 布尔值"""
    body = body or {}
    if body:
        return agent.save_config(body)
    return agent.config()


def api_chat(_q, body):
    body = body or {}
    return agent.chat(body.get('session'), body.get('text'))


def api_chat_poll(q, _body):
    s = agent.session(q.get('session'), create=False)
    if not s:
        return {'id': '', 'events': [], 'next': 0, 'running': False}
    return s.view(q.get('since') or 0)


def api_chat_confirm(_q, body):
    body = body or {}
    return agent.confirm(body.get('session'))


def api_chat_cancel(_q, body):
    body = body or {}
    return agent.cancel(body.get('session'))


def api_chat_reset(_q, body):
    body = body or {}
    return agent.reset(body.get('session')) or {}


def api_skill_run(_q, body):
    """跑一个 skill（唤醒/导航/战斗/剧情/签到/周目），返回 taskId"""
    body = body or {}
    tid = skills.run_skill((body.get('skill') or '').strip(), body.get('params') or {})
    return {'taskId': tid}


def api_skill_list(_q, _body):
    return {'skills': [{'name': k, 'desc': v[0]} for k, v in sorted(skills.SKILLS.items())]}


def api_skill_try(_q, body):
    """联调测试：让大模型解析一句话，看它选哪个 skill、参数对不对

    body: {text, skill?, run?}  skill 留空 = 全工具任选；run=true 直接跑
    """
    body = body or {}
    return agent.try_skill(body.get('text') or '', body.get('skill') or '',
                           bool(body.get('run')))


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


def api_schedule_status(q, _body):
    return scheduler.status(q.get('since') or 0)


def api_schedule_save(_q, body):
    return scheduler.save(body or {})


def api_schedule_run(_q, body):
    return scheduler.run_now((body or {}).get('id') or '')


def api_schedule_stop(_q, _body):
    return scheduler.stop_current()


ROUTES = {
    '/api/bootstrap': bootstrap,
    '/api/settings': api_settings,
    '/api/accounts': api_accounts,
    '/api/account': api_account,
    '/api/account/maa': api_account_maa,
    '/api/account/token': api_account_token,
    '/api/account/token/check': api_account_token_check,
    '/api/account/token/set': api_account_token_set,
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
    '/api/copilot/prepare': api_copilot_prepare,
    '/api/maa/clear': api_maa_clear,
    '/api/maa/tasklist': api_maa_tasklist,
    '/api/task': api_task,
    '/api/export': api_export,
    '/api/paradox/plan': api_paradox_plan,
    '/api/paradox/pick': api_paradox_pick,
    '/api/paradox/mark': api_paradox_mark,
    '/api/gacha': api_gacha,
    '/api/gacha/sync': api_gacha_sync,
    '/api/signin/run': api_signin_run,
    # 卫戍协议·策略刻章速刷
    '/api/garrison/status': api_garrison_status,
    '/api/garrison/download/start': api_garrison_download,
    '/api/garrison/opts': api_garrison_opts,
    '/api/garrison/test': api_garrison_test,
    '/api/garrison/start': api_garrison_start,
    '/api/garrison/resume': api_garrison_resume,
    '/api/garrison/stop': api_garrison_stop,
    # 观测层（M2/M3）
    '/api/watch/status': api_watch_status,
    '/api/watch/start': api_watch_start,
    '/api/watch/stop': api_watch_stop,
    '/api/runs': api_runs,
    '/api/maa/profiles': api_profiles,
    # 多账号
    '/api/multi/candidates': api_multi_candidates,
    '/api/multi/plan': api_multi_plan,
    '/api/multi/apply': api_multi_apply,
    # 任务编排（一键长草 / 抄作业 / 小活动）
    '/api/maa/capabilities': api_maa_capabilities,
    '/api/maa/profile': api_maa_profile,
    '/api/maa/build': api_maa_build,
    # 定时任务（时间触发 / 链式启动）
    '/api/schedule/status': api_schedule_status,
    '/api/schedule/save': api_schedule_save,
    '/api/schedule/run': api_schedule_run,
    '/api/schedule/stop': api_schedule_stop,
    # 内置助手（M4）
    '/api/agent/status': api_agent_status,
    '/api/agent/config': api_agent_config,
    '/api/chat': api_chat,
    '/api/chat/poll': api_chat_poll,
    '/api/chat/confirm': api_chat_confirm,
    '/api/chat/cancel': api_chat_cancel,
    '/api/chat/reset': api_chat_reset,
    # 挂关 skill 化（唤醒/导航/战斗/剧情/签到/周目编排）
    '/api/skill/run': api_skill_run,
    '/api/skill/list': api_skill_list,
    '/api/skill/try': api_skill_try,
}
