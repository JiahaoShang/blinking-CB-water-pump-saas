"""本机单会话桥的账号隔离：每个企业账号独立容器和登录状态。"""
import json
import os
import re
import subprocess
from pathlib import Path


def provision_personal_bridge(db_path, account_id, config):
    if config.get('managed_runtime'):
        return config
    if os.environ.get('WHATSAPP_PERSONAL_MULTI_SESSION') == '1':
        # 使用支持多会话的服务时，复用平台配置并采用账号专属会话。
        return config
    if not re.fullmatch(r'[a-f0-9]{24}', account_id):
        raise ValueError('连接标识无效')
    api_key = os.environ.get('WHATSAPP_PERSONAL_API_KEY', '').strip()
    if not api_key or '\n' in api_key or '\r' in api_key:
        raise ValueError('连接服务未准备好，请联系平台运营方')
    name = 'b2b-account-' + account_id
    root = Path(db_path).resolve().parent / 'connection-runtime' / account_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        exists = subprocess.run(['docker', 'inspect', name], capture_output=True, timeout=10)
        if exists.returncode:
            env_file = root / 'bridge.env'
            env_data = 'WAHA_API_KEY=' + api_key + '\nWHATSAPP_DEFAULT_ENGINE=NOWEB\nWAHA_SWAGGER_ENABLED=false\nWAHA_DASHBOARD_ENABLED=false\n'
            proxy = os.environ.get('WHATSAPP_PERSONAL_PROXY', '').strip()
            if proxy and '\n' not in proxy and '\r' not in proxy:
                env_data += 'WHATSAPP_PROXY_SERVER=' + proxy + '\n'
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as file:
                file.write(env_data)
            result = subprocess.run([
                'docker', 'run', '-d', '--name', name, '--restart', 'unless-stopped',
                '--add-host', 'host.docker.internal:host-gateway',
                '-p', '127.0.0.1::3000', '--env-file', str(env_file),
                '-v', str(root / 'sessions') + ':/app/.sessions',
                os.environ.get('WHATSAPP_PERSONAL_IMAGE', 'devlikeapro/waha:noweb-arm-2026.9.2'),
            ], capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('连接服务启动失败，请联系平台运营方')
        else:
            subprocess.run(['docker', 'start', name], capture_output=True, timeout=15, check=True)
        result = subprocess.run(['docker', 'port', name, '3000/tcp'], capture_output=True, text=True, timeout=10, check=True)
        address = result.stdout.strip().splitlines()[0]
        if not re.fullmatch(r'127\.0\.0\.1:\d+', address):
            raise ValueError('连接服务地址无效')
    except (OSError, subprocess.SubprocessError, IndexError):
        raise ValueError('连接服务暂不可用，请联系平台运营方') from None
    return {**config, 'api_base': 'http://' + address, 'session': 'default', 'managed_runtime': name}
