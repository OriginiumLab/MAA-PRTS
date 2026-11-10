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

from core import accounts, api, appupdate, settings, tasks, version  # noqa: E402

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
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + '.1')
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


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
        full = os.path.normpath(os.path.join(WEB, rel))
        # 目录分量级的前缀校验：字符串 startswith 会把 frontend-xxx 这类
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

    def do_GET(self):
        if not trusted_request(self.headers, self.server.server_address):
            self._reject()
            return
        q, path = self._query()
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
        """打开项目内的数据目录（accounts / config / root）"""
        table = {
            'accounts': settings.accounts_dir(),
            'config': settings.CONFIG_DIR,
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


def main():
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
    webview.create_window(
        'MAA-PRTS · 作业控制台',
        url,
        width=1520,
        height=960,
        min_size=(1120, 720),
        background_color='#0b0e14',
        easy_drag=False,
        js_api=Bridge(),
    )
    webview.start(debug=debug, private_mode=False)
    _quit(0, '窗口已关闭')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        raise
