"""控制台视觉系统复刻的回归测试：深浅主题、字体、图标、报错呈现。"""
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import make_server


class ConsoleThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "app.db"
        self.server = make_server(self.db_path, port=0)
        self.db = self.server.RequestHandlerClass.db
        # 控制台走查需要演示账号与演示企业：显式种入（普通新库不再预置）。
        self.db.seed_demo_users()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.thread.join()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode())
        conn.close()
        return result

    def login(self, email="demo-admin@example.invalid", password="demo-admin") -> str:
        status, headers, _ = self.request("POST", "/login", urlencode({"email": email, "password": password}))
        assert status == 303, status
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_console_theme_tokens_fonts_icons(self):
        cookie = self.login()
        _, _, page = self.request("GET", "/admin", cookie=cookie)
        # 深浅模式：主题开关 + 深色令牌块 + 切换脚本（localStorage 记忆）
        self.assertIn("id='themeToggle'", page)
        self.assertIn('html[data-theme="dark"]', page)
        self.assertIn("seller-workspace-theme", page)
        self.assertIn("--bg-app:#F7F7F7", page)
        # 字体：Inter + JetBrains Mono（联网加载，断网回退系统字体）
        self.assertIn("fonts.googleapis.com/css2?family=Inter", page)
        self.assertIn("JetBrains Mono", page)
        # 图标：sprite + 导航图标化
        self.assertIn('<symbol id="i-home"', page)
        self.assertIn("href='#i-inbox'", page)
        self.assertIn("href='#i-globe'", page)
        # 外壳与主题按钮样式
        self.assertIn("class='side-link", page)
        self.assertIn("class='theme-toggle'", page)

    def test_login_error_uses_form_alert_and_theme(self):
        _, _, page = self.request("GET", "/login")
        self.assertIn("id='themeToggle'", page)
        self.assertIn("class='auth-card'", page)
        self.assertIn("Growave", page)
        status, _, page2 = self.request(
            "POST", "/login", urlencode({"email": "demo-admin@example.invalid", "password": "wrong-password"})
        )
        self.assertEqual(status, 200)
        self.assertIn("class='form-alert'", page2)
        self.assertIn("邮箱或密码不正确", page2)

    def test_permission_denied_uses_reason_card(self):
        cookie = self.login("demo-sales@example.invalid", "demo-sales")
        status, _, page = self.request("GET", "/admin/users", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn("你无法执行这个操作", page)
        self.assertIn("当前账号没有访问此管理功能的权限", page)
        self.assertIn("class='dlg-reason'", page)
        self.assertIn("所需权限", page)
        self.assertIn("管理员", page)


if __name__ == "__main__":
    unittest.main()
