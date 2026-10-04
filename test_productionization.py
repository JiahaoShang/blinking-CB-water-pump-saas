"""正式化清单 F-1～F-5 回归：演示工具部署级开关（默认关闭 / 唯一开启入口=start_demo.py）、
企业名称动态化、演示口径文案。

- 默认关闭（正式形态）：四个演示按钮不渲染；四个 seed 端点连管理员也被拒（403、且不产生数据）；
  演示 notice 不出现。
- 演示模式（make_server(demo_tools=True)，即 start_demo.py 的形态）：按钮照常渲染；端点可用，
  含修复后的概览页 Lowara 入口（此前 403 的权限矩阵缺口）。
- F-4：控制台工具栏读 tenants 表的真实企业名；改名后页面随之变化。
- F-5：产品页流程说明不再写死 Lowara 演示口径（授权披露原则保留）。
- start_demo.py 是唯一开启入口：源码传参断言 + 真实子进程拉起演示服务后实测按钮存在。
"""
import http.client
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app import make_server

ROOT = Path(__file__).resolve().parent


def form_request(server, method, path, fields=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {}
    body = None
    if fields is not None:
        body = urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    headers_out = dict(response.getheaders())
    status = response.status
    conn.close()
    return status, headers_out, raw


def login(server, email, password):
    status, headers, _ = form_request(server, "POST", "/login", {"email": email, "password": password})
    if status not in (302, 303) or "Set-Cookie" not in headers:
        raise AssertionError(f"登录失败: {email} -> {status}")
    return headers["Set-Cookie"].split(";", 1)[0]


class ProductionizationTests(unittest.TestCase):
    """两个对照实例：off=默认（正式形态）；on=演示模式（start_demo.py 形态）。"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.off_server = make_server(Path(cls.temp_dir.name) / "off.db", port=0)
        cls.on_server = make_server(Path(cls.temp_dir.name) / "on.db", port=0, demo_tools=True)
        cls.off_db = cls.off_server.RequestHandlerClass.db
        cls.on_db = cls.on_server.RequestHandlerClass.db
        for db in (cls.off_db, cls.on_db):
            db.seed_demo_users()
        cls.off_thread = threading.Thread(target=cls.off_server.serve_forever, daemon=True)
        cls.on_thread = threading.Thread(target=cls.on_server.serve_forever, daemon=True)
        cls.off_thread.start()
        cls.on_thread.start()
        cls.off_admin = login(cls.off_server, "demo-admin@example.invalid", "demo-admin")
        cls.on_admin = login(cls.on_server, "demo-admin@example.invalid", "demo-admin")

    @classmethod
    def tearDownClass(cls):
        cls.off_server.shutdown()
        cls.off_server.server_close()
        cls.on_server.shutdown()
        cls.on_server.server_close()
        cls.off_db.conn.close()
        cls.on_db.conn.close()
        cls.off_thread.join()
        cls.on_thread.join()
        cls.temp_dir.cleanup()

    def test_off_buttons_hidden_and_endpoints_refused(self):
        # 按钮不渲染（两个页面）
        _, _, page = form_request(self.off_server, "GET", "/admin", cookie=self.off_admin)
        self.assertNotIn("加载演示数据", page)
        self.assertNotIn("加载 Lowara 资料", page)
        _, _, products_page = form_request(self.off_server, "GET", "/admin/products", cookie=self.off_admin)
        self.assertNotIn("导入两款示例水泵", products_page)
        self.assertNotIn("加载 Lowara 资料", products_page)
        # 端点一律拒绝（连管理员），且不产生任何数据
        for path in ("/admin/demo/seed", "/admin/products/seed", "/admin/products/seed-lowara", "/admin/lowara/seed"):
            status, _, body = form_request(self.off_server, "POST", path, cookie=self.off_admin)
            self.assertEqual(status, 403, path)
            self.assertIn("演示工具未启用", body, path)
        self.assertEqual(self.off_db.list_products("demo-tenant"), [])

    def test_on_buttons_visible_and_endpoints_work_including_lowara_fix(self):
        # 按钮照常渲染
        _, _, page = form_request(self.on_server, "GET", "/admin", cookie=self.on_admin)
        self.assertIn("加载演示数据", page)
        self.assertIn("加载 Lowara 资料", page)
        _, _, products_page = form_request(self.on_server, "GET", "/admin/products", cookie=self.on_admin)
        self.assertIn("导入两款示例水泵", products_page)
        # 概览页 Lowara 入口（原 403 缺陷）现在可用，并有成功 notice
        status, headers, _ = form_request(self.on_server, "POST", "/admin/lowara/seed", cookie=self.on_admin)
        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("notice=lowara", location)
        _, _, notice_page = form_request(self.on_server, "GET", location, cookie=self.on_admin)
        self.assertIn("Lowara 资料已加载", notice_page)
        self.assertGreater(len(self.on_db.list_products("demo-tenant")), 0)
        # 另两个入口同样可用
        status, _, _ = form_request(self.on_server, "POST", "/admin/demo/seed", cookie=self.on_admin)
        self.assertEqual(status, 303)
        status, _, _ = form_request(self.on_server, "POST", "/admin/products/seed", cookie=self.on_admin)
        self.assertEqual(status, 303)
        # V1-01 修正：员工即使持有产品模块，也不得看到或触发演示工具（演示工具仍只对管理员开放）
        employee_id = self.on_db.create_user("demo-tenant", "demo-employee@example.invalid", "普通员工", "employee", "strong-pass", "demo-admin")
        self.on_db.set_user_modules("demo-tenant", employee_id, ["products"], "demo-admin")
        employee_cookie = login(self.on_server, "demo-employee@example.invalid", "strong-pass")
        _, _, employee_page = form_request(self.on_server, "GET", "/admin/products", cookie=employee_cookie)
        self.assertNotIn("导入两款示例水泵", employee_page)
        status, _, _ = form_request(self.on_server, "POST", "/admin/products/seed", cookie=employee_cookie)
        self.assertEqual(status, 403)

    def test_tenant_name_is_read_from_database(self):
        # 默认演示企业名来自数据库；改名后控制台各页随之变化（F-4）
        _, _, page = form_request(self.off_server, "GET", "/admin", cookie=self.off_admin)
        self.assertIn("示例水泵企业（演示）", page)
        self.off_db.conn.execute("UPDATE tenants SET name=? WHERE id='demo-tenant'", ("星海泵业（示例）",))
        self.off_db.conn.commit()
        for path in ("/admin", "/admin/products", "/admin/sales"):
            _, _, page = form_request(self.off_server, "GET", path, cookie=self.off_admin)
            self.assertIn("星海泵业（示例）", page, path)
            self.assertNotIn("示例水泵企业（演示）", page, path)

    def test_demo_copy_rewritten_and_notices_gated(self):
        # F-5：流程说明不再写死 Lowara 演示口径，授权披露原则保留
        _, _, page = form_request(self.off_server, "GET", "/admin/products", cookie=self.off_admin)
        self.assertNotIn("Lowara 数据仅用于内部 Demo", page)
        self.assertIn("对外发布前须确认资料归属与再发布授权", page)
        # 演示 notice 同样只在演示模式下出现
        _, _, page = form_request(self.off_server, "GET", "/admin/products?notice=lowara", cookie=self.off_admin)
        self.assertNotIn("内部演示型号", page)
        _, _, page = form_request(self.off_server, "GET", "/admin?notice=demo", cookie=self.off_admin)
        self.assertNotIn("演示数据已加载", page)

    def test_start_demo_is_the_only_enabler(self):
        # ① 源码契约：唯一以 demo_tools=True 启动的位置是 start_demo.py
        start_source = (ROOT / "tools" / "demo" / "start_demo.py").read_text(encoding="utf-8")
        self.assertIn("demo_tools=True", start_source)
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        code_hits = [
            line for line in app_source.splitlines()
            if "demo_tools=True" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(code_hits, [], "app.py 不得自带演示开启路径（只允许注释里提及）")
        # ② 真实子进程拉起 start_demo 后实测：按钮存在（复用同一运行入口）
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        demo_db = Path(self.temp_dir.name) / "live-demo.db"
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "demo" / "start_demo.py"), "--no-open", "--port", str(port), "--db", str(demo_db)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.time() + 60
            ready = False
            while time.time() < deadline:
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("GET", "/login")
                    response = conn.getresponse()
                    response.read()
                    conn.close()
                    if response.status == 200:
                        ready = True
                        break
                except OSError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.4)
            if not ready:
                output = process.stdout.read() if process.poll() is not None else "(仍在运行)"
                self.fail(f"start_demo 未就绪：poll={process.poll()}\n{output}")
            class _Server:
                server_port = port
            cookie = login(_Server(), "demo-admin@example.invalid", "demo-admin")
            _, _, page = form_request(_Server(), "GET", "/admin", cookie=cookie)
            self.assertIn("加载演示数据", page, "start_demo 启动的环境必须开启演示工具")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            # 清理本次种子落盘的上传文件（沿用 test_demo_tooling 的收集-删除口径）
            if demo_db.exists():
                conn = sqlite3.connect(str(demo_db))
                try:
                    for (storage_path,) in conn.execute("SELECT storage_path FROM source_documents WHERE storage_path IS NOT NULL"):
                        target = ROOT / storage_path
                        if target.is_file():
                            target.unlink()
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
