"""Enterprise model overrides; credentials stay in private files, outside business/audit data."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
from urllib.parse import urlsplit
from ai_client import (
    AIConfig, AIService, AIConfigurationError, MODE_DEEPSEEK,
    DEEPSEEK_DEFAULT_MODEL, is_official_deepseek_endpoint,
)


def validate_endpoint(value):
    url = urlsplit(str(value).strip())
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.port not in (None, 443)):
        raise ValueError('请填写不含密钥、用户名或查询参数的 HTTPS API 地址。')
    try:
        addresses = socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('无法解析 API 地址，请检查后重试。') from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('API 地址必须是公网 HTTPS 地址。')
    return str(value).strip().rstrip('/')


class EnterpriseModels:
    def __init__(self, db_path, platform):
        self.root = Path(str(db_path) + '.model-secrets')
        self.platform = platform

    def path(self, tenant):
        return self.root / (hashlib.sha256(str(tenant).encode()).hexdigest() + '.json')

    def read(self, tenant):
        try:
            with self.path(tenant).open() as f:
                value = json.load(f)
            if not all(isinstance(value.get(k), str) and value[k] for k in ('base_url', 'model', 'api_key')):
                raise ValueError('invalid stored config')
            if is_official_deepseek_endpoint(value['base_url']):
                # 官方端点固定 deepseek-flash：旧配置（如存有 deepseek-chat）读取时规范化；
                # 不改写原文件、不动密钥（用户 2026-10-13 明确）。
                value['model'] = DEEPSEEK_DEFAULT_MODEL
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # A broken override must never silently spend the platform API budget.
            raise AIConfigurationError('企业模型配置无法读取，请管理员重新连接或断开。') from None

    def public(self, tenant):
        value = self.read(tenant)
        return {'source': 'custom', 'base_url': value['base_url'], 'model': value['model']} if value else {'source': 'platform'}

    def service(self, tenant):
        value = self.read(tenant)
        if value is None:
            return self.platform
        return AIService(AIConfig(mode=MODE_DEEPSEEK, provider='custom', **value))

    def save(self, tenant, base_url, model, api_key):
        if is_official_deepseek_endpoint(base_url):
            # 官方配置持久保存为规范值：官方端点固定 deepseek-flash。
            model = DEEPSEEK_DEFAULT_MODEL
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(dict(base_url=base_url, model=model, api_key=api_key), f)
            os.replace(name, self.path(tenant))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def disconnect(self, tenant):
        self.path(tenant).unlink(missing_ok=True)


def settings_page(handler, query):
    from app import esc, svg_icon, ai_status_section
    messages = {'connected': '已连接企业模型，企业业务已切换到你的 API。',
                'disconnected': '已断开自定义 API，企业业务已恢复平台默认服务。',
                'failed': '连接测试未通过。当前服务未改变，请检查地址、模型名称和密钥后重试。'}
    notice = messages.get(query.get('notice', [''])[0], '')
    try:
        current = handler.models.public(handler.tenant_id)
    except AIConfigurationError:
        current = {'source': 'broken'}
    admin = handler.user_role == 'admin'
    if current['source'] == 'custom':
        status = f"<b>正在使用企业自定义模型</b><p>{esc(current['model'])}</p>"
    elif current['source'] == 'broken':
        status = '<b>企业模型配置需要修复</b><p>请管理员重新连接或断开自定义 API。</p>'
    else:
        status = '<b>正在使用平台默认服务</b><p>无需配置模型，即可使用平台提供的服务。</p>'
        if handler.models.platform.mode != MODE_DEEPSEEK:
            status += "<p class='muted'>当前部署尚未配置平台模型 API，暂由本地规则处理已有业务；平台模型接通后无需企业成员设置。</p>"
    control = '<p class="muted">模型设置由企业管理员统一管理，对本企业的所有业务生效。</p>'
    if admin:
        disconnect = "<form method='post' action='/admin/preferences/model/disconnect'><button class='secondary'>断开并恢复平台服务</button></form>" if current['source'] != 'platform' else ''
        control += disconnect + f"""<details class='detail-toggle' {'open' if query.get('notice') == ['failed'] else ''}><summary>使用自己的模型 API</summary><div>
<p>连接成功后，本企业统一使用你的 API，平台默认 API 停止用于本企业。自定义服务出现故障时不会自动切回；你可主动断开恢复平台服务。</p>
<form method='post' action='/admin/preferences/model/connect'>
<label for="model-base-url">API 地址</label><input id="model-base-url" name='base_url' type='url' required placeholder='https://api.deepseek.com' value='{esc(current.get('base_url', ''))}'>
<p class='muted'>支持 OpenAI Chat Completions 兼容接口，填写基础地址（通常以 /v1 结尾），不包含 /chat/completions。模型需支持 JSON 输出。</p>
<label for="model-name">模型名称</label><input id="model-name" name='model' required maxlength='200' placeholder='deepseek-flash' value='{esc(current.get('model', ''))}'>
<label for="model-api-key">API 密钥</label><input id="model-api-key" name='api_key' type='password' required autocomplete='new-password' maxlength='4096' placeholder='输入密钥，保存后不再显示'>
<p><button>测试连接并启用</button></p><p class='muted'>测试会发起一次小型模型请求，可能产生供应商费用。</p></form></div></details>"""
    body = f"<div class='page-header'><h1>设置</h1><p class='muted'>企业统一的模型服务设置</p></div>" + (f"<p role='status'>{esc(notice)}</p>" if notice else '') + f"<section class='card'><h2>{svg_icon('sliders')} 模型服务</h2>{status}{control}</section>"
    if admin and current['source'] != 'broken':
        body += "<details class='detail-toggle'><summary>运行诊断</summary><div>" + ai_status_section(handler.db, handler.ai_for(handler.tenant_id), handler.tenant_id) + '</div></details>'
    handler.send_html(handler.console_page('设置', body, 'preferences'))


def settings_action(handler, form, disconnect=False):
    from ai_client import AIUnavailableError
    if disconnect:
        handler.models.disconnect(handler.tenant_id)
        action = 'platform_restored'
        notice = 'disconnected'
    else:
        try:
            endpoint = validate_endpoint(str(form.get('base_url', '')))
            model = str(form.get('model', '')).strip()
            key = str(form.get('api_key', '')).strip()
            if not model or len(model) > 200 or not key or len(key) > 4096 or any(c in key for c in '\r\n'):
                raise ValueError('invalid config')
            config = AIConfig(mode=MODE_DEEPSEEK, provider='custom', base_url=endpoint, model=model, api_key=key)
            # Exercise the actual task and its JSON contract before switching any live business.
            AIService(config).extract_requirements('clean water')
            handler.models.save(handler.tenant_id, endpoint, model, key)
        except (ValueError, AIConfigurationError, AIUnavailableError, OSError):
            handler.redirect('/admin/preferences?notice=failed')
            return
        action = 'custom_connected'
        notice = 'connected'
    handler.db._audit(handler.tenant_id, 'model_service', handler.tenant_id, action, handler.actor, '企业模型服务切换；不记录密钥')
    handler.db.conn.commit()
    handler.redirect('/admin/preferences?notice=' + notice)


def custom_urlopen(request, timeout):
    """Pin the public IP for this request; disallow redirects and private-network endpoints."""
    import contextlib
    import http.client
    import ssl
    import urllib.error

    @contextlib.contextmanager
    def opened():
        url = urlsplit(request.full_url)
        connection = None
        try:
            validate_endpoint(request.full_url)
            addresses = socket.getaddrinfo(url.hostname, 443, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError('private endpoint')
            address = addresses[0]
            raw = socket.socket(address[0], address[1], address[2])
            raw.settimeout(timeout)
            try:
                raw.connect(address[4])
                secured = ssl.create_default_context().wrap_socket(raw, server_hostname=url.hostname)
            except BaseException:
                raw.close()
                raise
            connection = http.client.HTTPSConnection(url.hostname, timeout=timeout)
            connection.sock = secured
            connection.request(request.get_method(), url.path or '/', body=request.data, headers=dict(request.header_items()))
            response = connection.getresponse()
            if response.status >= 300:
                raise urllib.error.HTTPError(request.full_url, response.status, 'Model request failed', response.headers, None)
            yield response
        except (OSError, ValueError, http.client.HTTPException) as error:
            if isinstance(error, urllib.error.HTTPError):
                raise
            raise urllib.error.URLError('Model endpoint unavailable') from None
        finally:
            if connection:
                connection.close()
    return opened()
