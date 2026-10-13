import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ai_client import AIConfig, AIService, AIUnavailableError
from model_settings import EnterpriseModels

class EnterpriseModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = AIService(AIConfig())
        self.models = EnterpriseModels(Path(self.tmp.name) / 'app.db', self.platform)
    def tearDown(self):
        self.tmp.cleanup()
    def test_switch_is_enterprise_scoped_and_persistent(self):
        self.assertIs(self.models.service('a'), self.platform)
        self.models.save('a', 'https://api.deepseek.com', 'deepseek-chat', 'secret-example')
        self.assertEqual(self.models.service('a').config.api_key, 'secret-example')
        self.assertEqual(self.models.service('a').config.model, 'deepseek-flash', '官方端点模型读取规范化为 deepseek-flash')
        self.assertIs(self.models.service('b'), self.platform)
        restarted = EnterpriseModels(Path(self.tmp.name) / 'app.db', self.platform)
        self.assertEqual(restarted.service('a').config.model, 'deepseek-flash')
        self.assertEqual(self.models.public('a')['model'], 'deepseek-flash', '公开状态与实际请求模型一致')
        self.assertEqual(json.loads(self.models.path('a').read_text())['model'], 'deepseek-flash', '官方配置持久保存为 deepseek-flash')
        self.assertEqual(os.stat(self.models.path('a')).st_mode & 0o777, 0o600)
        self.assertNotIn('secret-example', str(self.models.public('a')))
        self.models.disconnect('a')
        self.assertIs(self.models.service('a'), self.platform)
    def test_failure_never_uses_platform(self):
        self.models.save('a', 'https://api.deepseek.com', 'deepseek-chat', 'secret-example')
        with patch('ai_client.DeepSeekChatClient.call_json', side_effect=AIUnavailableError('timeout', 'timeout')):
            with self.assertRaises(AIUnavailableError):
                self.models.service('a').extract_requirements('water')
        self.assertEqual(self.models.public('a')['source'], 'custom')
    def test_corrupt_credentials_fail_closed(self):
        self.models.save('a', 'https://api.deepseek.com', 'deepseek-chat', 'secret-example')
        self.models.path('a').write_text('{invalid')
        with self.assertRaises(Exception):
            self.models.service('a')
    def test_endpoint_rejects_credentials_and_private_hosts(self):
        from model_settings import validate_endpoint
        for url in ['http://api.deepseek.com', 'https://u:p@api.deepseek.com', 'https://127.0.0.1', 'https://localhost', 'https://api.deepseek.com?key=x']:
            with self.assertRaises(ValueError):
                validate_endpoint(url)

    def test_legacy_official_config_normalized_on_read_without_rewriting(self):
        """旧官方配置（如 deepseek-chat）读取时规范化为 flash；不改写旧文件、不动凭据。"""
        self.models.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        legacy = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat', 'api_key': 'legacy-key'}
        self.models.path('a').write_text(json.dumps(legacy))
        self.assertEqual(self.models.service('a').config.model, 'deepseek-flash')
        self.assertEqual(self.models.public('a')['model'], 'deepseek-flash')
        self.assertEqual(self.models.service('a').config.api_key, 'legacy-key')
        self.assertEqual(json.loads(self.models.path('a').read_text())['model'], 'deepseek-chat', '读取规范化不改写旧文件')

    def test_official_custom_request_forced_and_third_party_protocol_unchanged(self):
        """官方自定义端点请求套用固定三参数；第三方兼容端点保留原协议（不塞 DeepSeek 专用字段）。"""
        records = []

        class _CapturedResponse:
            status = 200

            def __init__(self, request):
                self._request = request

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant', 'content': json.dumps({'values': {}})}}],
                    'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
                }).encode()

        def fake_urlopen(request, timeout):
            records.append(request)
            return _CapturedResponse(request)

        with patch('model_settings.custom_urlopen', side_effect=fake_urlopen):
            self.models.save('official', 'https://api.deepseek.com', 'deepseek-v4-pro', 'official-key')
            self.models.service('official').extract_requirements('clean water')
            self.models.save('third', 'https://example.com/v1', 'custom-model', 'third-key')
            self.models.service('third').extract_requirements('clean water')
        self.assertEqual(self.models.public('official')['model'], 'deepseek-flash')
        self.assertEqual(self.models.public('third')['model'], 'custom-model')
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].full_url, 'https://api.deepseek.com/chat/completions')
        official_body = json.loads(records[0].data.decode())
        third_body = json.loads(records[1].data.decode())
        self.assertEqual(official_body['model'], 'deepseek-flash')
        self.assertEqual(official_body['thinking'], {'type': 'enabled'})
        self.assertEqual(official_body['reasoning_effort'], 'low')
        self.assertNotIn('temperature', official_body)
        self.assertEqual(official_body['max_tokens'], 1024, '官方端点未显式设置时默认 1024（低思考真实 512 截断后适配）')
        self.assertEqual(third_body['model'], 'custom-model')
        self.assertNotIn('thinking', third_body, '第三方端点不注入 DeepSeek 专用字段')
        self.assertNotIn('reasoning_effort', third_body)
        self.assertEqual(third_body['temperature'], 0.2)
        self.assertEqual(third_body['max_tokens'], 512, '第三方端点默认沿用原 512')

class ModelSettingsHTTPTests(unittest.TestCase):
    from test_console_presentation import ConsolePresentationTests as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    request = _Fixture.request
    login = _Fixture.login

    def test_roles_and_failed_connect_keep_previous_service(self):
        from urllib.parse import urlencode
        cookie = self.login()
        status, _, page = self.request('GET', '/admin/preferences', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('测试连接并启用', page)
        self.server.RequestHandlerClass.models.save('demo-tenant', 'https://api.deepseek.com', 'old-model', 'old-key')
        with patch('model_settings.validate_endpoint', return_value='https://api.deepseek.com'), patch('ai_client.DeepSeekChatClient.call_json', side_effect=AIUnavailableError('timeout', 'timeout')):
            status, headers, _ = self.request('POST', '/admin/preferences/model/connect', urlencode({'base_url':'https://api.deepseek.com','model':'new-model','api_key':'new-key'}), cookie=cookie)
        self.assertIn('notice=failed', headers['Location'])
        self.assertEqual(self.server.RequestHandlerClass.models.service('demo-tenant').config.api_key, 'old-key')
        employee = self.login('demo-sales@example.invalid', 'demo-sales')
        status, _, page = self.request('GET', '/admin/preferences', cookie=employee)
        self.assertEqual(status, 200)
        self.assertNotIn('old-key', page)
        self.assertNotIn('测试连接并启用', page)
        self.assertEqual(self.request('POST', '/admin/preferences/model/disconnect', '', cookie=employee)[0], 403)
        self.request('POST', '/admin/preferences/model/disconnect', '', cookie=cookie)
        self.assertIs(self.server.RequestHandlerClass.models.service('demo-tenant'), self.server.RequestHandlerClass.ai)

    def test_real_provider_routing_connect_disconnect_and_other_tenant(self):
        import urllib.request
        from urllib.parse import urlencode
        from test_ai_deepseek import MockDeepSeekServer, extraction_success, ai_config_for
        success = extraction_success({'media': {'value':'clean water','unit':''}})
        platform = MockDeepSeekServer([success, success])
        custom = MockDeepSeekServer([success, success])
        try:
            self.server.RequestHandlerClass.models.platform = AIService(ai_config_for(platform))
            admin = self.login()
            with patch('model_settings.validate_endpoint', return_value=custom.base_url), patch('model_settings.custom_urlopen', side_effect=urllib.request.urlopen):
                _, headers, _ = self.request('POST', '/admin/preferences/model/connect', urlencode({'base_url':'https://example.com/v1','model':'custom-model','api_key':'custom-test-key'}), cookie=admin)
                self.assertIn('notice=connected', headers['Location'])
                for tenant, site in [('demo-tenant','a'), ('other-tenant','b')]:
                    self.db.ensure_tenant(tenant)
                    self.db.register_buyer_site(tenant, site, 'https://example.com')
                    _, _, body = self.request('POST', '/api/v1/buyer/conversations', json_body={'site_key':site})
                    import json
                    conv = json.loads(body)
                    self.request('POST', '/api/v1/buyer/conversations/'+conv['conversation_id']+'/messages', json_body={'text':'clean water'}, extra_headers={'X-Site-Key':site,'Authorization':'Bearer '+conv['access_token']})
            self.assertEqual(len(custom.requests), 2, 'connect probe + enterprise request')
            self.assertEqual(len(platform.requests), 1, 'only other enterprise uses platform')
            custom_body = custom.requests[0]['body']
            self.assertNotIn('thinking', custom_body, '第三方端点不注入 DeepSeek 专用字段')
            self.assertNotIn('reasoning_effort', custom_body)
            self.assertEqual(custom_body['model'], 'custom-model')
            self.assertEqual(custom_body['temperature'], 0.2)
            platform_body = platform.requests[0]['body']
            self.assertEqual(platform_body['model'], 'deepseek-flash')
            self.assertEqual(platform_body['thinking'], {'type': 'enabled'})
            self.assertEqual(platform_body['reasoning_effort'], 'low')
            self.assertNotIn('custom-test-key', self.db_path.read_bytes().decode('latin1'))
            self.assertNotIn('custom-test-key', self.request('GET','/admin/preferences',cookie=admin)[2])
            self.request('POST','/admin/preferences/model/disconnect','',cookie=admin)
            _, _, body = self.request('POST','/api/v1/buyer/conversations',json_body={'site_key':'a'})
            conv = json.loads(body)
            self.request('POST','/api/v1/buyer/conversations/'+conv['conversation_id']+'/messages',json_body={'text':'clean water'},extra_headers={'X-Site-Key':'a','Authorization':'Bearer '+conv['access_token']})
            self.assertEqual(len(platform.requests), 2)
            self.assertEqual(len(custom.requests), 2)
        finally:
            platform.stop()
            custom.stop()
