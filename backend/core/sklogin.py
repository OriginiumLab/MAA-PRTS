# -*- coding: utf-8 -*-
"""森空岛内置登录：独立小窗 + WebView2 + CDP 网络嗅探抓 token。

思路抄自 MaaEnd 的 ZiplineImport：内嵌 WebView2 打开官方页面，旁路抄
网络数据。实测下来网页只在登录瞬间短暂持有鹰角通行证 token（拿去换
cred 用完即弃），已登录的页面只发 cred 签名请求，localStorage 里存的
也是 32 位 cred——所以每次开窗都先清掉小窗里的登录态、逼出完整登录
流程，再从登录请求体里把 token 抄出来，grant 接口验证有效后自动关窗
回填表单。

登录窗不交给 pywebview 管：它运行时 create_window 的子窗口路径会把
构造 Invoke 到主窗口 UI 线程，pythonnet 下两边互等 GIL，整个进程假死
（实测过）。这里自己起一个 STA 线程直接摆 WinForms Form + WebView2，
和主窗口互不相干；UserDataFolder 也单独放一个目录，避免和主窗口的
WebView2 environment 冲突。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse

from . import skland

LOGIN_URL = 'https://www.skland.com/'

# phase: idle → opening → waiting ⇄ verifying → done / failed / closed
_lock = threading.Lock()
_state = {'phase': 'idle', 'error': '', 'token': ''}
_win = [None]               # {'form': .NET Form, 'wv': .NET WebView2, 'cwv': CoreWebView2}
_closed = [False]           # 登录窗已关闭（用户手动关或抓完自动关）
_gen = [0]                   # 登录会话代：取消后立刻重开时，旧窗口/旧 watch 靠它退场
_cand_lock = threading.Lock()
_candidates = []            # 抓到的候选 token，等 watch 线程逐个验证
_seen = set()
_debug = {'requests': 0, 'urls': [], 'header_names': set(), 'scan': []}   # 诊断轨迹，只在内存，不外发给前端

WATCH_TIMEOUT = 15 * 60     # 用户慢悠悠登录也够用，超时强制收摊


def status():
    with _lock:
        return _status_locked()


def _status_locked():
    """已持有 _lock 时用的读法（threading.Lock 不可重入，别在锁内调 status()）"""
    d = {'phase': _state['phase'], 'error': _state['error']}
    if _state['phase'] == 'done':
        d['token'] = _state['token']
    return d


def start():
    """打开登录小窗并开始嗅探；已经在跑就直接返回当前状态"""
    with _lock:
        if _state['phase'] in ('opening', 'waiting', 'verifying'):
            return _status_locked()
        _state.update(phase='opening', error='', token='')

    # 只在桌面窗口模式下有意义：pywebview 没起来就没有 WebView2 环境，
    # 而且负责加载 WebView2 DLL 的正是 pywebview 的 edgechromium 后端
    import webview
    if not getattr(webview, 'guilib', None) or not webview.windows:
        return _fail('当前不是桌面窗口模式，请手动粘贴 token')

    _closed[0] = False
    _reset_done[0] = False
    _gen[0] += 1
    mygen = _gen[0]
    with _cand_lock:
        _candidates.clear()
        _seen.clear()
        _debug.update(requests=0, urls=[], header_names=set(), scan=[])
    try:
        from System.Threading import Thread, ThreadStart, ApartmentState
        t = Thread(ThreadStart(lambda: _thread_main(mygen)))
        t.SetApartmentState(ApartmentState.STA)
        t.IsBackground = True
        t.Start()
    except Exception as e:
        return _fail('打不开登录窗口：%s' % e)
    threading.Thread(target=_watch, args=(mygen,), daemon=True,
                     name='skland-login').start()
    return status()


def cancel():
    _close_window()
    _gen[0] += 1                 # 旧 watch 立即退场，别去消费下一轮的候选
    with _lock:
        if _state['phase'] != 'done':
            _state.update(phase='idle', error='')
    return status()


# ---------------------------------------------------------------- 登录窗线程
def _thread_main(mygen):
    """登录小窗自己的 STA 线程：摆 Form + WebView2，泵消息直到关窗"""
    mywin = None
    try:
        import System.Windows.Forms as WinForms
        from System import Uri
        from System.Drawing import Size
        from Microsoft.Web.WebView2.WinForms import WebView2, CoreWebView2CreationProperties

        form = WinForms.Form()
        form.Text = '森空岛登录 · 登录成功自动抓取 token'
        form.ClientSize = Size(560, 1000)      # 还原后的尺寸
        form.MinimumSize = Size(460, 660)
        form.StartPosition = WinForms.FormStartPosition.CenterScreen
        form.WindowState = WinForms.FormWindowState.Maximized   # 点开即窗口化全屏，可一键还原

        wv = WebView2()
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = _user_data_folder()
        wv.CreationProperties = props
        wv.Dock = WinForms.DockStyle.Fill
        wv.Source = Uri(LOGIN_URL)
        form.Controls.Add(wv)

        wv.CoreWebView2InitializationCompleted += _on_ready
        form.FormClosed += _on_form_closed
        mywin = {'form': form, 'wv': wv}
        _win[0] = mywin
        WinForms.Application.Run(form)
    except Exception as e:
        _fail('登录窗口出错：%s' % e)
    finally:
        # 只有自己还是当前一代时才动全局状态，免得误杀新一轮登录
        if mygen == _gen[0]:
            if _win[0] is mywin:
                _win[0] = None
            _closed[0] = True
            with _lock:
                if _state['phase'] in ('opening', 'waiting', 'verifying'):
                    _state['phase'] = 'closed'


def _on_ready(sender, args):
    """WebView2 初始化完成（UI 线程回调）：挂上请求嗅探"""
    try:
        if not args.IsSuccess:
            _fail('WebView2 没起来（装一下 Microsoft Edge WebView2 Runtime？）')
            _close_window()
            return
        cwv = sender.CoreWebView2
        _win[0]['cwv'] = cwv
        # 嗅探走 CDP 网络域（MaaEnd 同款）：WebResourceRequested 在这套
        # pythonnet + WebView2 组合下连 '*' 全拦都不触发（实测），放弃之
        recv = cwv.GetDevToolsProtocolEventReceiver('Network.requestWillBeSent')
        recv.DevToolsProtocolEventReceived += _on_cdp_request
        cwv.CallDevToolsProtocolMethodAsync('Network.enable', '{}')
        cwv.NavigationCompleted += _on_nav_done   # 控件层同名事件不触发，挂这层
        sender.ZoomFactor = 1.25              # 页面内容跟着窗口一起放大
        with _lock:
            if _state['phase'] == 'opening':
                _state['phase'] = 'waiting'
    except Exception as e:
        _fail('订阅网络事件失败：%s' % e)
        _close_window()


def _on_nav_done(_sender, args):
    """首页打开失败给个提示；首次打开先清登录态强制走完整登录流程"""
    try:
        if not args.IsSuccess:
            with _lock:
                if _state['phase'] in ('opening', 'waiting'):
                    _state['error'] = '页面没加载出来（网络问题？），检查后可在小窗里按 F5 刷新'
            return
        if not _reset_done[0]:
            _reset_done[0] = True
            _reset_login_state()
            return
        _scan_storage()
    except Exception:
        pass


_reset_done = [False]


def _reset_login_state():
    """清掉小窗里的 skland 登录态（cookie + 存储）并重载。

    页面只在登录流程中短暂持有鹰角通行证 token（换完 cred 就丢），已登录
    的页面只会发 cred 签名请求——不清登录态就永远等不到 token。
    """
    try:
        win = _win[0] or {}
        cwv, wv = win.get('cwv'), win.get('wv')
        if not cwv or not wv:
            return
        from System import Action, String
        from System.Threading.Tasks import Task

        def _after_clear(_t):
            try:
                from System import Func, Object

                def _nav():
                    cwv.Navigate(LOGIN_URL)  # 清完再重载，这次是未登录页面
                    return True

                # ContinueWith 的回调在线程池，Navigate 必须回 UI 线程
                wv.Invoke(Func[Object](_nav))
                _note_scan('RESET-AND-RELOAD')
            except Exception as e:
                _note_scan('RESET-NAV-EXC %s' % str(e)[:120])

        cwv.CookieManager.DeleteAllCookies()
        wv.ExecuteScriptAsync('localStorage.clear();sessionStorage.clear()').ContinueWith(
            Action[Task[String]](_after_clear))
        _note_scan('RESET-CLEAR')
    except Exception as e:
        _note_scan('RESET-EXC %s' % e)


# ---------------------------------------------------------------- 读页面存储
_scan_at = [0.0]     # 上次扫描时刻（节流）

_SCAN_JS = (
    'JSON.stringify((()=>{'
    'const o={ls:{}};'
    "for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o.ls[k]=localStorage.getItem(k)}"
    'return o})())'
)


def _note_scan(msg):
    with _cand_lock:
        if len(_debug['scan']) < 60:
            _debug['scan'].append(msg)


def _scan_storage():
    """读页面存储与 cookie 找 token（UI 线程调用）

    网页端 API 只带签名后的 cred 头、不带鹰角通行证 token 头（实测），
    token 在页面存储里，ExecuteScriptAsync 读出来再验证即可。
    CDP Runtime.evaluate 的自定义委托 pythonnet 包不了（试过多种姿势），
    ExecuteScriptAsync + ContinueWith（内置泛型 Action）是 pywebview 验证过的模式。
    """
    try:
        if not _reset_done[0]:
            return          # 清登录态之前扫到的都是旧 cred，白验
        now = time.time()
        if now - _scan_at[0] < 8:          # 节流
            return
        _scan_at[0] = now
        wv = (_win[0] or {}).get('wv')
        if not wv:
            return
        from System import Action, String
        from System.Threading.Tasks import Task

        def _cb(t):
            try:
                _on_storage_result(str(t.Result))
            except Exception as e:
                _note_scan('SCAN-CB-EXC %s' % e)

        _note_scan('SCAN-RUN')
        wv.ExecuteScriptAsync(_SCAN_JS).ContinueWith(Action[Task[String]](_cb))
    except Exception as e:
        _note_scan('SCAN-EXC %s' % e)


def _offer(value, first=False):
    """把像 token 的值塞进候选队列（真伪由 watch 线程的 grant 验证兜底）。

    first=True 插队：token 字段出身的候选优先验证，别排在 localStorage
    里一堆设备指纹后面干等。清登录态（_reset_done）之前的候选都是旧
    登录态的残留 cred，直接丢。
    """
    try:
        if not _reset_done[0]:
            return
        if not isinstance(value, str) or len(value) <= 20 or ' ' in value:
            return
        with _cand_lock:
            if value not in _seen:
                _seen.add(value)
                if first:
                    _candidates.insert(0, value)
                else:
                    _candidates.append(value)
    except Exception:
        pass


def _offer_json_values(text):
    """请求体/存储值本身是 JSON 时，只取 token 字段——全量收会把设备指纹
    payload 里一堆长字符串当候选，白打一堆验证请求"""
    try:
        data = json.loads(text)
    except Exception:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() == 'token':
                _offer(v, first=True)   # token 字段插队优先验证


def _on_storage_result(raw):
    """ExecuteScriptAsync 回调：结果是 JS 返回值的 JSON 编码（字符串再包一层引号）"""
    try:
        if not raw or raw == 'null':
            _note_scan('SCAN-CB empty（页面 JS 抛异常或还没就绪）')
            return
        val = json.loads(raw)
        if not isinstance(val, str):
            _note_scan('SCAN-CB not-str')
            return
        data = json.loads(val)
        found = 0
        for k, v in ((data.get('ls') or {})).items():
            _note_scan('LS %s len=%s' % (k, len(v) if isinstance(v, str) else '?'))
            if 'token' in str(k).lower():
                _offer(v, first=True)      # key 名带 token 的直投
            _offer_json_values(v)          # 其他的只认 JSON 内层的 token 字段
            found += 1
        _note_scan('SCAN-CB %d keys' % found)
    except Exception as e:
        _note_scan('SCAN-PARSE-EXC %s' % e)


def _on_cdp_request(_sender, args):
    """CDP requestWillBeSent（UI 线程回调）：抄 token 请求头进候选队列"""
    try:
        # 本机这套 WebView2 SDK 的属性名叫 ParameterObjectAsJson（文档常见 ParametersAsJson）
        raw = (getattr(args, 'ParameterObjectAsJson', None)
               or getattr(args, 'ParametersAsJson', None) or '')
        data = json.loads(str(raw)) if raw else {}
        req = data.get('request') or {}
        url = str(req.get('url') or '')
        headers = req.get('headers') or {}
        with _cand_lock:
            _debug['requests'] += 1
            if len(_debug['urls']) < 24:
                _debug['urls'].append(url[:120])
            _debug['header_names'].update(str(k) for k in headers)
        tok = str(headers.get('token') or '')
        if len(tok) > 20:
            _offer(tok, first=True)
        # 登录流程中页面会用鹰角通行证 token 去换 cred：POST body 里就有它
        pd = req.get('postData')
        if isinstance(pd, str) and 0 < len(pd) < 100000:
            try:
                body = json.loads(pd)
            except Exception:
                body = None
            if isinstance(body, dict):
                _note_scan('POST %s token? %s' % (
                    url[:80], 'yes' if isinstance(body.get('token'), str) else 'no'))
                _offer(body.get('token'), first=True)
                _offer_json_values(pd)
        # URL query 里带 token= 的也顺手收
        if 'token=' in url:
            for kv in url.split('?', 1)[-1].split('&'):
                if kv.startswith('token='):
                    _offer(urllib.parse.unquote(kv[6:]), first=True)
        # 带签名 cred 头的请求 = 页面有登录态在活跃，顺手扫一遍存储
        if 'cred' in headers:
            _scan_storage()
    except Exception:
        pass


def _on_form_closed(sender, _args=None):
    win = _win[0]
    if win and win.get('form') is not sender:
        return          # 关的是上一代的窗口，别动这一轮的状态
    _closed[0] = True
    with _lock:
        if _state['phase'] in ('opening', 'waiting', 'verifying'):
            _state['phase'] = 'closed'


def _close_window():
    """从任意线程关掉登录窗（Close 必须在它自己的 UI 线程上执行）"""
    win = _win[0]
    if not win:
        return
    try:
        form = win['form']
        if form.IsHandleCreated and not form.IsDisposed:
            from System.Windows.Forms import MethodInvoker
            form.BeginInvoke(MethodInvoker(form.Close))
    except Exception:
        pass


# ---------------------------------------------------------------- 验证
def _watch(mygen):
    """守着候选队列：来了就用 grant 接口验证，有效即关窗收工"""
    deadline = time.time() + WATCH_TIMEOUT
    while time.time() < deadline:
        if mygen != _gen[0]:
            return                  # 被新一轮登录取代，安静退场
        cand = None
        with _cand_lock:
            if _candidates:
                cand = _candidates.pop(0)
        if cand is None:
            if _closed[0]:
                # 只把「还在进行中」的会话标成 closed：失败路径（如 WebView2
                # 没起来 → _fail → 关窗）已经带着原因置了 failed，别把它
                # 覆盖成中性的「窗口已关闭」，用户会看不到修复指引（#54）
                with _lock:
                    if _state['phase'] in ('opening', 'waiting', 'verifying'):
                        _state.update(phase='closed', error='')
                return
            time.sleep(0.4)
            continue
        _set_phase('verifying')
        ok, err = _verify(cand)
        if mygen != _gen[0]:
            return                  # 验证是最长约 12 秒的网络请求，回来时
                                    # 可能已被新一轮会话取代：别替新会话写
                                    # done、更别关掉新一轮的窗口（#54）
        if ok:
            with _lock:
                _state.update(phase='done', token=cand, error='')
            _close_window()
            return
        # 多半是页面里残留的过期 token：丢掉继续等用户登录后的新请求
        _set_phase('waiting', err)
    if mygen == _gen[0]:
        _set_phase('failed', '等太久了先收摊，也可以直接手动粘贴 token')
        _close_window()


def _verify(token):
    """候选 token 先用 grant 接口验一遍，别把过期值回填给用户"""
    try:
        g = skland._http(
            'POST', skland.GRANT_URL,
            {'User-Agent': skland.SKLAND_UA, 'Accept-Encoding': 'identity',
             'Connection': 'close', 'dId': skland.DID},
            {'appCode': skland.APP_CODE, 'token': token, 'type': 0},
            timeout=12, retries=1)
        if g.get('status') == 0:
            return True, ''
        return False, 'token 换 grant 失败：%s' % (g.get('msg') or g)
    except Exception as e:
        return False, '验证请求失败：%s' % e


# ---------------------------------------------------------------- 内部工具
def _fail(msg):
    with _lock:
        _state.update(phase='failed', error=msg)
    return status()


def _set_phase(phase, error=''):
    with _lock:
        _state.update(phase=phase, error=error)


def _user_data_folder():
    """登录窗专属 UDF（和主窗口的分开，避免 WebView2 environment 冲突）"""
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'pywebview', 'skland-login')
    os.makedirs(d, exist_ok=True)
    return d
