"""Official Sign in with ChatGPT for the local Windows application."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from . import net, settings, vault

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
SCOPES = 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
DISCOVERY_URL = ISSUER + '/.well-known/openid-configuration'
JWKS_URL = ISSUER + '/.well-known/jwks.json'
DYNAMIC_CLIENT = 'dynamic_agent_client'
CALLBACK = '/auth/callback'
EXPIRED_GRANTS = {
    'invalid_grant', 'invalid_refresh_token', 'token_expired', 'refresh_token_expired',
    'refresh_token_invalidated', 'refresh_token_reused', 'session_revoked',
}
_file_lock = threading.Lock()
_login_lock = threading.Lock()
_login = {'running': False, 'error': ''}


class ChatGPTError(RuntimeError):
    """Safe to show in the UI. Never include a URL, token, or raw HTTP body."""


class OpenAIError(ChatGPTError):
    def __init__(self, status, code='', request_id=''):
        self.status = status
        self.code = code if re.fullmatch(r'[a-zA-Z0-9_]{1,80}', str(code)) else ''
        self.request_id = request_id if re.fullmatch(r'[\w-]{1,100}', request_id) else ''
        super().__init__(f'OpenAI 请求失败（HTTP {status}）')


def open_request(url, *, token='', data=None, form=False, stream=False, timeout=15):
    headers = {'Accept': 'text/event-stream' if stream else 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if data is not None:
        if form:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urlencode(data).encode('ascii')
        else:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    try:
        return net.open(Request(url, data=body, headers=headers), timeout=timeout,
                        redirect=False, retry_direct=body is None)
    except HTTPError as exc:
        with exc:
            try:
                error = json.loads(exc.read(65536)).get('error')
                code = error.get('code') if isinstance(error, dict) else error
            except (ValueError, AttributeError, OSError):
                code = ''
        raise OpenAIError(exc.code, code, exc.headers.get('x-request-id', '')) from None
    except (OSError, URLError):
        raise ChatGPTError('连不上 OpenAI，请检查网络或代理设置。') from None


def get_json(url, **kwargs):
    with open_request(url, **kwargs) as resp:
        try:
            result = json.load(resp)
        except (ValueError, OSError):
            raise ChatGPTError('无法读取 OpenAI 的响应。') from None
    if not isinstance(result, dict):
        raise ChatGPTError('OpenAI 返回了无法识别的响应。')
    return result


@contextmanager
def _locked_store():
    """Serialize rotating-token operations across processes sharing config/."""
    if os.name != 'nt':
        raise ChatGPTError('ChatGPT 登录仅在 Windows 版可用。')
    import msvcrt

    path = os.path.join(settings.CONFIG_DIR, 'chatgpt.dpapi')
    with _file_lock:
        try:
            os.makedirs(settings.CONFIG_DIR, exist_ok=True)
            with open(path + '.lock', 'a+b') as lockfile:
                lockfile.seek(0)
                msvcrt.locking(lockfile.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    try:
                        with open(path, 'rb') as saved:
                            db = json.loads(vault.unprotect(saved.read()))
                    except FileNotFoundError:
                        db = {'host': '', 'active': '', 'profiles': {}}
                    yield db, path
                finally:
                    lockfile.seek(0)
                    msvcrt.locking(lockfile.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, ValueError):
            raise ChatGPTError('无法读写本机 ChatGPT 登录配置。请检查目录权限或关闭使用同一目录的实例。') from None


def _save(db, path):
    blob = vault.protect(json.dumps(db, ensure_ascii=False).encode('utf-8'))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='chatgpt-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _enabled(profile):
    return bool(profile.get('access_token') and PLAN_SCOPE in profile.get('scopes', []))


def status():
    with _login_lock:
        login = dict(_login)
    try:
        with _locked_store() as (db, _):
            active = db['active']
            profiles = db['profiles']
            profile = profiles.get(active, {})
            return {
                'active': active, 'permission': _enabled(profile),
                'connection': profile.get('generation', ''),
                'signedIn': bool(profile.get('id_token')), 'model': profile.get('model', ''),
                'limited': bool(profile.get('limited')), 'login': login,
                'accounts': [
                    {'id': key, 'label': value.get('email') or 'ChatGPT 账号',
                     'number': value['number'], 'connected': _enabled(value)}
                    for key, value in profiles.items()
                ],
            }
    except ChatGPTError as exc:
        return {'active': '', 'accounts': [], 'login': login, 'error': str(exc)}


def _profile(db, profile_id):
    profile = db['profiles'].get(profile_id)
    if profile is None:
        raise ChatGPTError('找不到此 ChatGPT 连接，请打开助手设置。')
    return profile


def select(profile_id):
    with _locked_store() as (db, path):
        _profile(db, profile_id)
        db['active'] = profile_id
        _save(db, path)


def save_model(profile_id, model):
    with _locked_store() as (db, path):
        if db['active'] != profile_id:
            raise ChatGPTError('ChatGPT 账号已切换，请重新选择模型。')
        _profile(db, profile_id)['model'] = model
        _save(db, path)


def set_limited(profile_id, limited):
    with _locked_store() as (db, path):
        _profile(db, profile_id)['limited'] = limited
        _save(db, path)


def _random():
    return secrets.token_urlsafe(32)


def _browser_code(host, profile, reconsent, *, lifetime=180):
    state, nonce, verifier = _random(), _random(), _random()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
    saved_id = profile.get('client_id')
    reply = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def log_message(self, *_):
            pass  # The query string contains the authorization code.

        def do_GET(self):
            try:
                query = urlsplit(self.path)
                params = parse_qs(query.query, max_num_fields=20)
            except ValueError:
                self.send_error(400)
                return
            local_host = f'127.0.0.1:{self.server.server_port}'
            valid = (self.headers.get('Host') == local_host and query.path == CALLBACK
                     and len(params.get('state', [])) == 1
                     and hmac.compare_digest(params['state'][0].encode(), state.encode()))
            if valid and not reply:
                reply.update(params)
            text = ('已收到 ChatGPT 授权结果。请关闭此页面并返回 MAA-PRTS。' if valid
                    else '此 ChatGPT 登录请求无效或已过期。')
            body = text.encode('utf-8')
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(('127.0.0.1', 0), CallbackHandler) as listener:
        listener.timeout = 0.5
        uri = f'http://127.0.0.1:{listener.server_port}{CALLBACK}'
        params = {
            'client_id': saved_id or DYNAMIC_CLIENT, 'ext_agent_host_id': host,
            'response_type': 'code', 'redirect_uri': uri, 'scope': SCOPES,
            'resource': RESOURCE, 'state': state, 'nonce': nonce,
            'code_challenge_method': 'S256',
            'code_challenge': challenge.decode('ascii').rstrip('='),
        }
        if not saved_id:
            params['agent_name_hint'] = 'MAA-PRTS ChatGPT Assistant'
        if profile.get('id_token'):
            params['id_token_hint'] = profile['id_token']
        if reconsent:
            params['prompt'] = 'consent'
        if not webbrowser.open(ISSUER + '/api/accounts/authorize?' + urlencode(params)):
            raise ChatGPTError('无法打开系统浏览器。')
        deadline = time.monotonic() + lifetime
        while not reply and time.monotonic() < deadline:
            listener.handle_request()
    if not reply:
        raise ChatGPTError('ChatGPT 登录已超时，请重新试一次。')
    if reply.get('error'):
        raise ChatGPTError('ChatGPT 授权已取消或被拒绝。')
    codes, ids = reply.get('code', []), reply.get('client_id', [])
    client_id = ids[0] if len(ids) == 1 else saved_id if not ids else ''
    if not client_id or not re.fullmatch(r'oaiapp_[A-Za-z0-9_-]+', client_id):
        raise ChatGPTError('OpenAI 未返回有效的应用注册。')
    if saved_id and client_id != saved_id:
        raise ChatGPTError('OpenAI 返回的应用注册与所选账号不一致。')
    if len(codes) != 1:
        raise ChatGPTError('OpenAI 未返回有效的授权码。')
    return client_id, nonce, {
        'grant_type': 'authorization_code', 'code': codes[0], 'client_id': client_id,
        'code_verifier': verifier, 'redirect_uri': uri, 'resource': RESOURCE,
    }


def _identity(id_token, client_id, nonce, access_token):
    return _verify_identity(id_token, client_id, nonce, access_token, get_json(JWKS_URL))


def _verify_identity(id_token, client_id, nonce, access_token, keys):
    import jwt

    try:
        kid = jwt.get_unverified_header(id_token)['kid']
        candidates = [key for key in jwt.PyJWKSet.from_dict(keys).keys if key.key_id == kid
                      and key.public_key_use in ('sig', None) and key.algorithm_name == 'RS256']
        if len(candidates) != 1:
            raise ValueError('signing key')
        claims = jwt.decode(id_token, candidates[0].key, algorithms=['RS256'],
                            issuer=ISSUER, audience=client_id,
                            options={'require': ['iss', 'aud', 'sub', 'exp', 'nonce']})
        audiences = claims['aud'] if isinstance(claims['aud'], list) else [claims['aud']]
        if claims.get('azp', client_id) != client_id or (len(audiences) > 1 and 'azp' not in claims):
            raise ValueError('authorized party')
        if not claims['sub'] or claims['nonce'] != nonce:
            raise ValueError('identity')
        if 'at_hash' in claims:
            digest = hashlib.sha256(access_token.encode('ascii')).digest()
            expected = base64.urlsafe_b64encode(digest[:16]).decode('ascii').rstrip('=')
            if not hmac.compare_digest(claims['at_hash'], expected):
                raise ValueError('access hash')
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise ChatGPTError('无法验证 OpenAI 返回的账号身份。') from None
    return claims


def _token_fields(tokens, previous=None):
    previous = previous or {}
    scope = tokens.get('scope')
    if scope is not None and not isinstance(scope, str):
        raise ChatGPTError('OpenAI 返回的权限信息无效。')
    scopes = scope.split() if scope is not None else previous.get('scopes', [])
    access = tokens.get('access_token')
    refresh = tokens.get('refresh_token', previous.get('refresh_token', ''))
    if PLAN_SCOPE in scopes:
        expiry = tokens.get('expires_in')
        if (not isinstance(access, str) or not access or not isinstance(refresh, str)
                or not refresh or type(expiry) is not int or expiry <= 0
                or str(tokens.get('token_type', '')).lower() != 'bearer'):
            raise ChatGPTError('OpenAI 返回的 ChatGPT 套餐凭据不完整。')
        return {'access_token': access, 'refresh_token': refresh, 'scopes': scopes,
                'expires_at': time.time() + expiry}
    return {'access_token': '', 'refresh_token': refresh if isinstance(refresh, str) else '',
            'scopes': scopes, 'expires_at': 0}


def start_login(profile_id='', reconsent=False):
    with _login_lock:
        if _login['running']:
            raise ChatGPTError('浏览器里已有一次 ChatGPT 登录正在等待授权。')
        _login.update(running=True, error='', newConnection=False)
    threading.Thread(target=_login_worker, args=(profile_id, reconsent), daemon=True,
                     name='chatgpt-sign-in').start()


def _login_worker(profile_id, reconsent):
    error, new_connection = '', ''
    try:
        new_connection = _sign_in(profile_id, reconsent)
    except ChatGPTError as exc:
        error = str(exc)
    except ImportError:
        error = '缺少登录依赖，请用新版程序或重新安装构建依赖。'
    except Exception:
        error = 'ChatGPT 登录未能完成，请重试。'
    finally:
        with _login_lock:
            _login.update(running=False, error=error, newConnection=new_connection)


def _sign_in(profile_id, reconsent):
    with _locked_store() as (db, path):
        profile = dict(_profile(db, profile_id)) if profile_id else {}
        if not db['host']:
            db['host'] = uuid.uuid4().urn
            _save(db, path)
        host = db['host']
    client_id, nonce, grant = _browser_code(host, profile, reconsent)
    try:
        tokens = get_json(TOKEN_URL, data=grant, form=True)
    except OpenAIError as exc:
        if exc.code != 'invalid_grant':
            raise
        client_id, nonce, grant = _browser_code(
            host, {**profile, 'client_id': client_id}, reconsent)
        tokens = get_json(TOKEN_URL, data=grant, form=True)
    identity = _identity(tokens.get('id_token', ''), client_id, nonce, tokens.get('access_token') or '')
    if profile and profile['subject'] != identity['sub']:
        raise ChatGPTError('登录的 ChatGPT 账号与选中的连接不同，请使用「添加账号」。')
    fields = _token_fields(tokens)
    with _locked_store() as (db, path):
        profiles = db['profiles']
        current = profiles.get(client_id, {})
        if current and current['subject'] != identity['sub']:
            raise ChatGPTError('该 ChatGPT 注册已关联其他账号。')
        if profile and current.get('generation') != profile.get('generation'):
            raise ChatGPTError('此连接在授权时已变更，请重新登录。')
        new_connection = _enabled(fields) and not current.get('welcomed')
        email = identity.get('email') if identity.get('email_verified') is True else ''
        generation = _random()
        profiles[client_id] = {
            **current, **fields, 'client_id': client_id, 'subject': identity['sub'],
            'email': email, 'number': current.get('number', len(profiles) + 1),
            'id_token': tokens['id_token'], 'generation': generation,
            'welcomed': current.get('welcomed', False) or new_connection,
        }
        db['active'] = client_id
        _save(db, path)
        return generation if new_connection else ''


def _clear_session(profile):
    for key in ('id_token', 'access_token', 'refresh_token', 'scopes', 'expires_at', 'limited'):
        profile.pop(key, None)
    profile['generation'] = _random()


def access_token(profile_id, generation=None, *, for_inference=False):
    with _locked_store() as (db, path):
        if db['active'] != profile_id:
            raise ChatGPTError('ChatGPT 账号已切换，请开启新对话。')
        profile = _profile(db, profile_id)
        if generation is not None and generation != profile.get('generation'):
            raise ChatGPTError('ChatGPT 登录已变更，请开启新对话。')
        if not _enabled(profile):
            raise ChatGPTError('请在助手设置登录 ChatGPT 并授权使用套餐额度。')
        if for_inference and profile.get('limited'):
            raise ChatGPTError('ChatGPT 用量上限已触发。请查看用量，在助手设置中恢复请求。')
        if profile['expires_at'] <= time.time() + 60:
            try:
                tokens = get_json(TOKEN_URL, form=True, data={
                    'grant_type': 'refresh_token', 'client_id': profile_id,
                    'refresh_token': profile['refresh_token'], 'resource': RESOURCE,
                })
            except OpenAIError as exc:
                if exc.code in EXPIRED_GRANTS:
                    _clear_session(profile)
                    _save(db, path)
                    raise ChatGPTError('ChatGPT 会话已过期或被撤销，请重新登录。') from None
                raise
            profile.update(_token_fields(tokens, profile))
            _save(db, path)
            if not _enabled(profile):
                raise ChatGPTError('此连接已失去 ChatGPT 套餐授权，请在助手设置重新授权。')
        return profile['access_token']


def _revoke(profile_id, refresh_token):
    endpoint = get_json(DISCOVERY_URL).get('revocation_endpoint')
    if endpoint != ISSUER + '/api/accounts/oauth/revoke':
        raise ChatGPTError('OpenAI 撤销地址无法验证。')
    for attempt in range(2):
        try:
            with open_request(endpoint, form=True, data={
                'token': refresh_token, 'token_type_hint': 'refresh_token', 'client_id': profile_id,
            }, timeout=5) as response:
                if response.status != 200:
                    raise ChatGPTError('OpenAI 未确认撤销。')
            return
        except OpenAIError as exc:
            if attempt or exc.status < 500:
                raise
        except ChatGPTError:
            if attempt:
                raise
        time.sleep(0.5)


def sign_out(profile_id):
    with _locked_store() as (db, path):
        profile = _profile(db, profile_id)
        confirmed = True
        if profile.get('refresh_token'):
            try:
                _revoke(profile_id, profile['refresh_token'])
            except ChatGPTError:
                confirmed = False
        _clear_session(profile)
        _save(db, path)
        return confirmed
