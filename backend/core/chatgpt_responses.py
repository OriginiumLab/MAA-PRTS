"""Translate the built-in assistant's tool loop to the official Responses API."""
from __future__ import annotations

import http.client
import json

from . import chatgpt_auth as auth

NAMESPACE = 'maa_prts'
MESSAGES = {
    'subscription_sharing_user_not_eligible': '此账号或工作区目前无法使用 ChatGPT 套餐额度。',
    'subscription_sharing_usage_limit_exceeded': '已达到 ChatGPT 用量上限。请查看用量，在助手设置中恢复请求。',
    'subscription_sharing_usage_unavailable': '暂时无法检查 ChatGPT 用量，请稍后再试。',
    'subscription_sharing_unsupported_capability': '此模型或账号不支持本次请求中的功能，请在助手设置中换个模型。',
    'subscription_sharing_route_not_supported': '当前 OpenAI 服务未开放此套餐调用路径。',
    'subscription_sharing_invalid_user': 'OpenAI 无法验证套餐账号，请在助手设置重新登录。',
    'chatpass_v2_scope_not_authorized': '此账号未授权使用 ChatGPT 套餐，请在助手设置重新授权。',
    'chatpass_v2_invalid_authorization_context': 'OpenAI 无法验证套餐授权，请在助手设置重新登录。',
    'subscription_sharing_user_unavailable': '暂时无法读取 ChatGPT 账号用量，请稍后再试。',
}


def _service_error(profile_id, status, code, request_id=''):
    message = MESSAGES.get(code)
    if code == 'subscription_sharing_usage_limit_exceeded':
        auth.set_limited(profile_id, True)
    if not message:
        message = {
            401: 'ChatGPT 连接未获接受，请在助手设置重新登录。',
            403: 'OpenAI 拒绝了此请求，请检查套餐授权和服务可用地区。',
            429: 'ChatGPT 请求受限，请查看用量并稍后再试。',
            503: 'ChatGPT 服务暂不可用，请稍后再试。',
        }.get(status, 'ChatGPT 请求未能完成。')
    details = ', '.join(str(part) for part in (status, code, request_id) if part)
    return auth.ChatGPTError(message + (f'（{details}）' if details else ''))


def models(profile_id):
    try:
        result = auth.get_json(auth.RESOURCE + '/models', token=auth.access_token(profile_id))
    except auth.OpenAIError as exc:
        raise _service_error(profile_id, exc.status, exc.code, exc.request_id) from None
    items = result.get('models')
    if not isinstance(items, list):
        raise auth.ChatGPTError('OpenAI 未返回此账号的模型列表。')
    return [{'id': item['slug'], 'label': item.get('display_name') or item['slug']}
            for item in items if item.get('visibility') == 'list' and item.get('slug')]


def _payload(model, messages, tools):
    history, instructions = [], []
    for message in messages:
        role = message['role']
        if role == 'system':
            instructions.append(message['content'])
        elif role == 'tool':
            history.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                            'output': message['content']})
        elif '_chatgpt_output' in message:
            # Preserve OpenAI's original order, including encrypted reasoning, when
            # sending tool results back without server-side conversation storage.
            history.extend(message['_chatgpt_output'])
        elif message.get('content'):
            history.append({'role': role, 'content': message['content']})
    body = {'model': model, 'input': history, 'instructions': '\n\n'.join(instructions),
            'store': False, 'stream': True}
    if tools:
        body['tools'] = [{
            'type': 'namespace', 'name': NAMESPACE,
            'description': 'Built-in MAA-PRTS game assistant tools',
            'tools': [{'type': 'function', 'strict': False, **tool['function']} for tool in tools],
        }]
    return body


def _events(response):
    data = []
    for raw in response:
        line = raw.decode('utf-8').rstrip('\r\n')
        if not line and data:
            yield json.loads('\n'.join(data))
            data.clear()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))


def _completed(profile_id, response, on_delta):
    emitted = False
    for event in _events(response):
        kind = event.get('type')
        result = event.get('response') or {}
        if kind in ('response.output_text.delta', 'response.refusal.delta'):
            if on_delta and event.get('delta'):
                emitted = True
                on_delta(event['delta'])
        elif kind == 'response.completed':
            return result['output'], emitted
        elif kind in ('response.failed', 'error'):
            code = auth.OpenAIError(None, (result.get('error') or event).get('code')).code
            raise _service_error(profile_id, None, code)
        elif kind == 'response.incomplete':
            raise auth.ChatGPTError('ChatGPT 回复未完成，请重试。')
    raise auth.ChatGPTError('ChatGPT 连接中断，未收到完整回复，请重试。')


def stream(connection, messages, tools, on_delta=None):
    profile_id, generation, model = connection
    body = _payload(model, messages, tools)
    try:
        token = auth.access_token(profile_id, generation, for_inference=True)
        with auth.open_request(auth.RESOURCE + '/responses', token=token,
                               data=body, stream=True, timeout=180) as response:
            output, emitted = _completed(profile_id, response, on_delta)
        allowed = {tool['function']['name'] for tool in tools or []}
        text, calls, history = [], [], []
        for item in output:
            kind = item.get('type')
            if kind == 'message':
                for content in item['content']:
                    if content.get('type') == 'output_text':
                        text.append(content['text'])
                    elif content.get('type') == 'refusal':
                        text.append(content['refusal'])
                history.append(item)
            elif kind == 'reasoning' and item.get('encrypted_content'):
                history.append(item)
            elif kind == 'function_call':
                name, call_id, args = item['name'], item['call_id'], item['arguments']
                if (name not in allowed or item.get('namespace') not in (None, NAMESPACE)
                        or not call_id or not isinstance(json.loads(args), dict)):
                    raise auth.ChatGPTError('ChatGPT 返回了无法执行的助手工具请求。')
                calls.append({'id': call_id, 'type': 'function',
                              'function': {'name': name, 'arguments': args}})
                history.append(item)
        content = ''.join(text)
        if content and on_delta and not emitted:
            on_delta(content)
        return content, calls, 'tool_calls' if calls else 'stop', history
    except auth.OpenAIError as exc:
        raise _service_error(profile_id, exc.status, exc.code, exc.request_id) from None
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
        raise auth.ChatGPTError('ChatGPT 回复中断或格式异常，请重试。') from None
