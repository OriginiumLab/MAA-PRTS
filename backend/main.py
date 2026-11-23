# -*- coding: utf-8 -*-
"""MAA-PRTS 作业控制台 · 启动器

pywebview（WebView2）原生窗口 + 内置 HTTP 服务。
打包后是单文件夹 exe，不依赖外部 python。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import accounts, api, applog, appupdate, settings, tasks, version  # noqa: E402

WEB = settings.static_dir()
LOG_PATH = settings.LOG_PATH
_last_ping = [time.time()]
_started = [time.time()]
_srv = [None]


# ---------------------------------------------------------------- 日志
def log(msg):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), msg)
    try:
        if sys.stdout:
            print(line, flush=True)
    except Exception:
        pass
    applog.write(line)   # 落盘/轮转统一走 core.applog，两个独立写端会竞争轮转


# ---------------------------------------------------------------- 解除网络锁定
def unblock_bundle():
    """删掉包内文件的「网络锁定」标记（MOTW，即 Zone.Identifier 数据流）。

    发布 zip 从网上下载后用资源管理器「全部解压」，每个文件都会带上
    ZoneId=3 标记。原生 DLL 加载不受影响，但 .NET Framework 拒绝加载带
    标记的托管程序集——pythonnet 的 Python.Runtime.dll 正是这种，于是
    webview.start 时报 Failed to resolve Python.Runtime.Loader.Initialize。
    启动时把整个 _internal 清一遍即可（等价于右键文件「解除锁定」）。
    """
    if not getattr(sys, 'frozen', False):
        return
    internal = os.path.join(os.path.dirname(sys.executable), '_internal')
    removed = 0
    for base, _dirs, files in os.walk(internal):
        for name in files:
            try:
                os.remove(os.path.join(base, name) + ':Zone.Identifier')
                removed += 1
            except OSError:
                pass
    if removed:
        log('已解除 %d 个文件的下载锁定标记（MOTW）' % removed)


# ---------------------------------------------------------------- HTTP
def trusted_request(headers, server_address):
    """防跨站请求：Host 必须指回本服务；带 Origin 时必须同源；
    带 Sec-Fetch-Site 时不得是跨站。

    服务只绑 127.0.0.1 且接口无鉴权，但浏览器里打开的任何网页都能向
    127.0.0.1 的端口发跨站请求（CSRF，触发写入 MAA / 改设置等副作用接口），
    恶意域名解析到 127.0.0.1 还能借 DNS 重绑定直接读接口。

    三层校验各挡一类：
    - Host：DNS 重绑定（Host 会是攻击者的域名）
    - Origin：fetch/XHR/表单 POST 类跨站请求（这类总带 Origin）
    - Sec-Fetch-Site：<img>/<script>/顶层导航这类不带 Origin 的盲打
      （浏览器对每个请求都带该头：same-origin / same-site / none 放行，
      cross-site 拒绝；curl 和老客户端没有这个头，不拦）
    """
    port = server_address[1]
    hosts = {'127.0.0.1:%d' % port, 'localhost:%d' % port}
    host = (headers.get('Host') or '').strip().lower()
    if host not in hosts:
        return False
    origin = (headers.get('Origin') or '').strip().lower()
    if origin and origin not in {'http://' + h for h in hosts}:
        return False
    sfs = (headers.get('Sec-Fetch-Site') or '').strip().lower()
    if sfs and sfs not in ('same-origin', 'same-site', 'none'):
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = 'MAA-PRTS/%s' % version.app_version()
    protocol_version = 'HTTP/1.1'
    # 挂死客户端（Content-Length 虚大、发一半断掉）不至于永久占用线程：
    # 超时后 rfile.read 抛 socket.timeout，连接被回收（#61）
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _err(self, msg, code=400):
        self._json({'ok': False, 'error': str(msg)}, code)

    def _query(self):
        p = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}, p.path

    def _body(self):
        """读 POST body；坏 JSON / 非 JSON 对象抛 ValueError，由调用方回 400。

        此前静默吞掉返回 {}：带损坏 body 的 /api/settings 会走 patch={}
        分支返回 ok:true，前端 toast「成功」而实际什么都没存（#61）。
        """
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ValueError('Content-Length 不是合法数字')
        if not n:
            return {}
        try:
            v = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            raise ValueError('请求体不是合法 JSON')
        if not isinstance(v, dict):
            raise ValueError('请求体必须是 JSON 对象')
        return v

    def _static(self, path):
        if path in ('/', ''):
            path = '/index.html'
        rel = path.lstrip('/')
        if rel.startswith('static/'):
            rel = rel[len('static/'):]
        full = os.path.normpath(os.path.join(WEB, rel))        # 目录分量级的前缀校验：字符串 startswith 会把 frontend-xxx 这类
        # 兄弟目录也放进来（#61）；不同盘符 commonpath 抛 ValueError，必在 WEB 外
        try:
            inside = os.path.commonpath([os.path.normpath(WEB), full]) == os.path.normpath(WEB)
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            self._send(404, b'not found', 'text/plain; charset=utf-8')
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except OSError as e:
            # 杀软临时锁文件在 Windows 不罕见：按 500 报，别让异常冒泡成连接重置
            self._send(500, ('read failed: %s' % e).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        self._send(200, data, api.MIME.get(ext, 'application/octet-stream'))

    def _theme(self, path):
        """第三方主题包里的文件：/themes/<id>/<相对路径>

        主题包解压在 config/themes/<id>/，里面有 theme.css、图片、字体。
        路径校验交给 themes.file_of（防目录穿越），这里只负责发字节。
        """
        from core import themes as _themes
        rest = path[len('/themes/'):]
        tid, _sep, rel = rest.partition('/')
        try:
            full = _themes.file_of(tid, rel)
        except Exception:
            full = None
        if not full:
            self._send(404, b'not found', 'text/plain; charset=utf-8')
            return
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except OSError as e:
            self._send(500, ('read failed: %s' % e).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        ext = os.path.splitext(full)[1].lower()
        self._send(200, data, api.MIME.get(ext, 'application/octet-stream'))

    def do_GET(self):
        if not trusted_request(self.headers, self.server.server_address):
            self._reject()
            return
        q, path = self._query()
        if path.startswith('/themes/'):
            self._theme(path)
            return
        if path.startswith('/api/'):
            if path == '/api/ping':
                _last_ping[0] = time.time()
                self._json({'ok': True})
                return
            fn = api.ROUTES.get(path)
            if not fn:
                self._err('接口不存在：' + path, 404)
                return
            try:
                self._json({'ok': True, 'data': fn(q, {})})
            except Exception as e:
                self._fail(e)
            return
        self._static(path)

    def do_POST(self):
        if not trusted_request(self.headers, self.server.server_address):
            self._reject()
            return
        q, path = self._query()
        if path == '/api/ping':
            _last_ping[0] = time.time()
            self._json({'ok': True})
            return
        fn = api.ROUTES.get(path)
        if not fn:
            self._err('接口不存在：' + path, 404)
            return
        try:
            body = self._body()
        except ValueError as e:
            self._err('请求体读不了：%s' % e, 400)
            return
        try:
            self._json({'ok': True, 'data': fn(q, body)})
        except Exception as e:
            self._fail(e)

    def _fail(self, e):
        tb = traceback.format_exc()
        log(tb)
        self._json({'ok': False, 'error': '%s: %s' % (type(e).__name__, e),
                    'trace': tb.splitlines()[-3:]}, 500)

    def _reject(self):
        log('已拒绝可疑请求：Host=%r Origin=%r' % (
            self.headers.get('Host'), self.headers.get('Origin')))
        self._send(403, b'forbidden', 'text/plain; charset=utf-8')


def find_port(start=8787, tries=40):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # Windows 上 SO_REUSEADDR 会允许两个进程绑同一端口（互相抢连接），
            # 必须用独占绑定，第二个实例才会自动顺延到下一个端口。
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    raise RuntimeError('找不到可用端口')


# ---------------------------------------------------------------- 前端可调用的原生能力
class Bridge:
    """暴露给 JS：window.pywebview.api.xxx()"""

    def app_info(self):
        return {
            'version': version.app_version(),
            'gamedata': version.pack_meta()['version'],
            'frozen': settings.FROZEN,
            'root': settings.project_root(),
            'accountsDir': settings.accounts_dir(),
            'configDir': settings.CONFIG_DIR,
            'webDir': WEB,
            'logPath': LOG_PATH,
            'native': True,
        }

    def open_folder(self, which='accounts'):
        """打开项目内的数据目录（accounts / config / logs / root）"""
        table = {
            'accounts': settings.accounts_dir(),
            'config': settings.CONFIG_DIR,
            'logs': settings.DEBUG_DIR,
            'root': settings.project_root(),
        }
        path = table.get(str(which or 'accounts').lower()) or settings.accounts_dir()
        os.makedirs(path, exist_ok=True)
        return self.open_path(path)

    def choose_folder(self, title='选择文件夹', initial=''):
        import webview
        try:
            res = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=initial or '',
                allow_multiple=False,
            )
            if not res:
                return {'path': None}
            if isinstance(res, (list, tuple)):
                return {'path': res[0] if res else None}
            return {'path': res}
        except Exception as e:
            return {'path': None, 'error': str(e)}

    def choose_file(self, title='选择程序', initial=''):
        """选一个文件（定时任务里挑要启动的 exe / bat）。没有 pywebview 时返回空。"""
        import webview
        try:
            res = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                directory=initial or '',
                allow_multiple=False,
                file_types=('可执行文件 (*.exe;*.bat;*.cmd)', '所有文件 (*.*)'),
            )
            if not res:
                return {'path': None}
            if isinstance(res, (list, tuple)):
                return {'path': res[0] if res else None}
            return {'path': res}
        except Exception as e:
            return {'path': None, 'error': str(e)}

    def open_path(self, path):
        """用系统默认方式打开文件/文件夹"""
        try:
            if not path or not os.path.exists(path):
                return {'ok': False, 'error': '路径不存在：%s' % path}
            os.startfile(path)  # noqa: S606 (Windows only)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def open_url(self, url):
        # 只放行 http(s)：Windows 上 webbrowser 最终走 os.startfile，
        # 其他 scheme（file:/smb:/自定义协议）等于任意命令执行面（#61）
        u = urllib.parse.urlparse(str(url or ''))
        if u.scheme not in ('http', 'https'):
            return {'ok': False, 'error': '只允许打开 http/https 链接'}
        try:
            webview_url = url
            webbrowser.open(webview_url)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def quit(self):
        _quit(0, '用户退出')

    def minimize(self):
        return {'ok': True}


def serve(port):
    """起 HTTP 服务。find_port 探测与真正绑定之间有 TOCTOU 窗口，
    绑定失败（OSError）顺延端口重试而不是让守护线程无声死掉（#61）。"""
    srv = None
    for off in range(10):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port + off), Handler)
            break
        except OSError:
            continue
    if srv is None:
        log('[error] 127.0.0.1:%d 起连续 10 个端口都绑定失败，界面服务起不来' % port)
        return
    if srv.server_address[1] != port:
        log('端口 %d 被占，改用 %d' % (port, srv.server_address[1]))
    srv.daemon_threads = True
    _srv[0] = srv
    srv.serve_forever()


def _quit(code=0, reason='退出'):
    """进程统一出口：先等在跑的后台任务收尾，再关服务退出。

    同步账号、写入 MAA 都是后台线程，直接 os._exit 会把它们拦腰斩断——
    配置写入本身是 .tmp + os.replace 原子写不怕斩，但任务日志会丢、
    app.log 里也看不出个所以然。退出前最多等 5 秒，等不完就如实记录。
    """
    log('正在退出（%s）…' % reason)
    try:
        _wingeom_save()        # 窗口几何先落盘（窗口还活着时读得到实际位置）
    except Exception:
        pass
    try:
        from core import watch as _watch
        _watch.stop_all()      # 观测线程是 daemon，但主动停能让它把最后一条日志写完
    except Exception:
        pass
    try:
        from core import garrison as _garrison
        _garrison.stop_all()   # 卫戍协议的 Node 宿主：通知收尾，等不及硬杀
    except Exception:
        pass
    try:
        left = tasks.wait_all(5)
        if left:
            log('仍有 %d 个任务未完成，不再等待：%s' % (len(left), '、'.join(left)))
    except KeyboardInterrupt:
        log('再次中断，不再等待后台任务')
    try:
        if _srv[0]:
            _srv[0].shutdown()
    except BaseException:
        pass
    log('已退出')
    os._exit(code)


# ---------------------------------------------------------------- 窗口位置记忆
# pywebview 的 moved/resized/maximized/restored 事件都是逻辑像素，和
# create_window(x=, y=, width=, height=) 的口径一致。关闭时写回
# settings.json 的 windowGeom，下次启动按它还原；位置不在任何屏幕上
# （换显示器/拔屏）就退回居中，别把窗口丢到看不见的地方。
_WINGEOM = {'x': None, 'y': None, 'width': None, 'height': None,
            'max': False, 'min': False}
_WINGEOM_LOCK = threading.Lock()
_WIN = [None]          # 主窗口（供 _quit 兜底保存）
_WIN_SHOWN = [False]


def _geom_visible(g: dict, screens) -> bool:
    """保存的位置至少让标题栏露出 120x30，否则当没存过"""
    try:
        x, y, w, h = (int(g.get(k)) for k in ('x', 'y', 'width', 'height'))
    except (TypeError, ValueError):
        return False
    for s in screens or []:
        ix = min(x + w, s.x + s.width) - max(x, s.x)
        iy = min(y + 40, s.y + s.height) - max(y, s.y)
        if ix >= 120 and iy >= 30:
            return True
    return False


def _wingeom_moved(x, y):
    with _WINGEOM_LOCK:
        if not (_WINGEOM['max'] or _WINGEOM['min']):
            _WINGEOM['x'], _WINGEOM['y'] = int(x), int(y)


def _wingeom_resized(w, h):
    with _WINGEOM_LOCK:
        if not (_WINGEOM['max'] or _WINGEOM['min']):
            _WINGEOM['width'], _WINGEOM['height'] = int(w), int(h)


def _wingeom_maximized():
    with _WINGEOM_LOCK:
        _WINGEOM['max'] = True
        _WINGEOM['min'] = False


def _wingeom_minimized():
    with _WINGEOM_LOCK:
        _WINGEOM['min'] = True


def _wingeom_restored():
    with _WINGEOM_LOCK:
        _WINGEOM['max'] = False
        _WINGEOM['min'] = False


def _wingeom_save():
    """把主窗口几何写回 settings.json（窗口关闭/进程退出时调用，幂等）"""
    with _WINGEOM_LOCK:
        g = {k: _WINGEOM[k] for k in ('x', 'y', 'width', 'height')
             if _WINGEOM.get(k) is not None}
        g['max'] = bool(_WINGEOM.get('max'))
        frozen = bool(_WINGEOM.get('max') or _WINGEOM.get('min'))
    win = _WIN[0]
    if win is not None and _WIN_SHOWN[0] and not frozen:
        # 最大化/最小化时读出来的是屏幕尺寸或 -32000，用事件里攒的正常值
        try:
            g.update(x=int(win.x), y=int(win.y),
                     width=int(win.width), height=int(win.height))
        except Exception:
            pass
    try:
        if g.get('width') and g.get('height'):
            settings.save({'windowGeom': g})
    except Exception:
        log(traceback.format_exc())
    return True          # closing 事件：不返回 False 就不会拦关闭


def main():
    if sys.argv[1:] == ['--verify-chatgpt-crypto']:
        try:
            from core import chatgpt_crypto_check
            chatgpt_crypto_check.verify()
        except Exception:
            if sys.stderr is not None:
                traceback.print_exc()
            return 1
        return 0

    # CLI 入口：打包后同一个 exe 用 `MAA-PRTS.exe --cli accounts` 调
    # （给外部 harness / 脚本用，不启窗口、不起 HTTP）
    if '--cli' in sys.argv:
        import cli          # 与 main.py 同级的 backend/cli.py，不是 core 包里的
        i = sys.argv.index('--cli')
        return cli.main(sys.argv[i + 1:])

    # 多开子进程入口（skills.run_batch 多组时每组起一个）：同进程两个 MaaCore
    # 会抢命名管道，只能一进程一台模拟器，详见 core/skillworker.py
    if '--skill-worker' in sys.argv:
        from core import skillworker
        i = sys.argv.index('--skill-worker')
        payload = sys.argv[i + 1] if i + 1 < len(sys.argv) else ''
        if not payload:
            print('[error] --skill-worker 需要 payload 文件路径')
            return 2
        return skillworker.main(payload)

    if not os.path.isdir(WEB):
        print('[error] frontend dir not found: %s' % WEB)
        print('        Resources are loaded relative to the working directory --')
        print('        run from the project root (double-click run.bat) in source mode,')
        print('        or keep exe next to _internal in packaged mode.')
        return 1

    no_window = '--no-window' in sys.argv
    debug = '--debug' in sys.argv
    port = find_port()

    # 先完成初始化再起 HTTP：窗口期打进来的 /api/app/install 会因 quit hook
    # 未装而「前端收到已拉起安装、进程永远不退」（#61）
    settings.ensure_dirs()          # config/ 与 debug/ 在启动时才建（import 不建）
    # 开机自启只对账，不擅自开启：设置里是 False 就把残留的 Run 项删掉，
    # 是 True 才写（用户 2026-11-25：极度厌恶没同意就自启）。源码模式直接跳过。
    try:
        from core import autostart as _auto
        _auto.sync(bool(settings.load().get('launchAtLogin')))
    except Exception:
        pass
    # 卫戍协议核心「保持最新」：启动时看一眼本机小黑盒记录，有新版就后台
    # 升级（幂等；下载校验失败不动已装版本）
    try:
        from core import garrisondl as _gdl
        threading.Thread(target=_gdl.check_update, daemon=True,
                         name='garrison-upd-check').start()
    except Exception:
        pass
    # 定时任务：时间触发 + 链式启动。线程自己 sleep 轮询，起不来不影响主流程
    try:
        from core import scheduler as _sched
        _sched.start()
    except Exception:
        log('[警告] 定时任务没起来\n' + traceback.format_exc())
    # 程序自更新：装完后由 api 层调 install()，需要让整个进程退出，
    # 而 api 不能 import main，所以反过来把退出函数注册进去
    appupdate.set_quit_hook(lambda reason: _quit(0, reason))
    st = settings.load()
    accounts.ensure_tokens()

    threading.Thread(target=serve, args=(port,), daemon=True, name='http').start()
    # 确认真的在监听再开窗（绑定可能顺延端口/整体失败），别给 webview 一个死地址
    deadline = time.time() + 5
    while _srv[0] is None and time.time() < deadline:
        time.sleep(0.05)
    if _srv[0] is None:
        log('[error] 界面服务没起来，退出（详见上面的端口绑定错误）')
        return 1
    url = 'http://127.0.0.1:%d/' % _srv[0].server_address[1]

    log('=' * 56)
    log('MAA-PRTS %s  界面 %s' % (version.app_version(), url))
    log('运行目录  %s' % settings.project_root())
    log('账号数据  %s' % settings.accounts_dir())
    log('配置文件  %s' % settings.SETTINGS_PATH)
    log('日志文件  %s' % settings.LOG_PATH)
    # MAA 目录：全局默认可能为空（账号各自绑定时不影响使用），顺带展示上次账号的绑定
    maa_line = st['maaDir'] or '（未设置全局默认）'
    if st.get('lastAccount'):
        try:
            snap = accounts.get_account(st['lastAccount'])
            bound = (snap.get('maa') or {}).get('dir') or ''
            if bound:
                maa_line = '%s ｜ 上次账号绑定 %s' % (maa_line, bound)
        except Exception:
            pass
    log('MAA 目录  %s' % maa_line)
    log('=' * 56)
    # 上一次「重启并安装」到底成没成：pending.json 里记着目标版本
    try:
        up = appupdate.on_start()
        if up.get('state') == 'done':
            log('程序已更新：%s → %s' % (up.get('from'), up.get('to')))
        elif up.get('state') == 'stale':
            log('[警告] 上次更新到 %s 没生效（当前仍是 %s），安装包已保留'
                % (up.get('to'), version.app_version()))
    except Exception:
        log(traceback.format_exc())
    if accounts.TOKENS_WARNING:
        log('[警告] ' + accounts.TOKENS_WARNING)

    if no_window:
        log('（--no-window：只起服务，用浏览器打开上面的地址）')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _quit(0, 'Ctrl+C')
        return 0

    unblock_bundle()  # 必须先于 webview：MOTW 会让 pythonnet 的程序集加载失败
    import webview
    geom = st.get('windowGeom') or {}
    win_w = max(1120, min(10000, int(geom.get('width') or 1520)))
    win_h = max(720, min(10000, int(geom.get('height') or 960)))
    try:
        screens = list(webview.screens)   # 逻辑像素，和 create_window 同口径
    except Exception:
        screens = []
    win_x, win_y = geom.get('x'), geom.get('y')
    if win_x is None or win_y is None or not _geom_visible(geom, screens):
        win_x = win_y = None              # 位置不在了（换屏/拔显示器）→ 居中
    win_kw = dict(width=win_w, height=win_h, min_size=(1120, 720),
                  background_color='#0b0e14', easy_drag=False, js_api=Bridge())
    if win_x is not None:
        win_kw.update(x=int(win_x), y=int(win_y))
    if geom.get('max'):
        win_kw['maximized'] = True
    window = webview.create_window('MAA-PRTS · 作业控制台', url, **win_kw)
    _WIN[0] = window
    with _WINGEOM_LOCK:
        _WINGEOM.update(x=int(win_x) if win_x is not None else None,
                        y=int(win_y) if win_y is not None else None,
                        width=win_w, height=win_h,
                        max=bool(geom.get('max')), min=False)
    window.events.shown += lambda: _WIN_SHOWN.__setitem__(0, True)
    window.events.moved += _wingeom_moved
    window.events.resized += _wingeom_resized
    window.events.maximized += _wingeom_maximized
    window.events.minimized += _wingeom_minimized
    window.events.restored += _wingeom_restored
    window.events.closing += _wingeom_save
    webview.start(debug=debug, private_mode=False)
    _quit(0, '窗口已关闭')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        raise
