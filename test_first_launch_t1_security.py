"""首次上线 T1 安全审查（Hermes）：HTTP 入口/对象授权矩阵 + 已观察项的失败复现。

边界（与本轮 T1 指令一致）：
- 只用临时数据库、临时上传目录（app.ROOT / app.UPLOAD_DIR 指向临时目录）与假密钥；
  不读取仓库 data/ 的真实业务数据，不读取任何真实密钥，不触发任何外部网络调用
  （渠道出站一律在鉴权/门控处被拦下，不进入发送器；模型一律 rules 模式或假配置）。
- 修复前预期失败（= 失败复现，见 docs/evidence/first-launch-t1/repro-before-fix.log）：
  * test_login_page_hides_demo_credentials_in_normal_deployment（登录页公开演示口令）
  * test_session_cookie_is_secure_behind_https_proxy / test_buyer_cookie_is_secure_behind_https_proxy
  * test_admin_write_post_rejects_cross_site_origin / test_login_post_rejects_cross_site_origin /
    test_inquiry_message_post_rejects_cross_site_origin（跨站 POST 无 Origin 校验）
  * test_backup_output_permissions_are_owner_only / test_restore_check_output_permissions_are_owner_only
  * test_debug_log_file_permissions_are_owner_only
- 其余用例为矩阵验证（修复前即应通过）：匿名入口、模块授权、跨租户隔离、
  停用/撤权/转交即时权限、买方凭据与跨站交接、演示门控、模型密钥不回显、日志/备份边界。

第二轮（2026-10-13，独立验收缺口的最小修复；修复前预期失败 11 个用例——unittest 报告
failures=15，其中来源覆盖用例含 5 个子用例——见 docs/evidence/first-launch-t1/round2-repro-before-fix.log）：
- 来源校验收紧：伪造 X-Forwarded-Host 不再放行（trust_forwarded_host）、协议必须匹配
  （requires_matching_scheme）、有效端口参与比较（compares_effective_port）、无 Origin 但
  Sec-Fetch-Site: cross-site 拒绝（cross_site_fetch_metadata）；
  交接换取与买方 API 同口径（handoff_exchange_scheme_mismatch、buyer_api_same_host_page_scheme）。
- 出站总线投递前复核：撤模块后不得再投递（rechecks_module_grant_after_revoke、
  rechecks_module_for_each_staff_send_source）。
- 新写入默认属主权限：业务库/原件 0600（new_application_database、new_upload_files、new_knowledge_files）。
修复前即正确、修复后必须保持的锁定用例：停用/连接撤权即时阻止投递、无 actor 的系统消息照常投递、
管理员路径与失败重试状态机不变、有效端口默认端口归一、登记企业网站 Origin 白名单照常受理。
"""
import http.client
import json
import os
import secrets
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

import app as app_module
from app import make_server
from tools.backup_restore import backup, restore_and_verify

TENANT = "demo-tenant"
ADMIN_EMAIL = "demo-admin@example.invalid"
ADMIN_PASSWORD = "demo-admin"


def request(server, method, path, *, fields=None, json_body=None, extra_headers=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = dict(extra_headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif fields is not None:
        body = urlencode(fields).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
    out = {}
    for key, value in response.getheaders():
        out[key] = f"{out[key]}\n{value}" if key in out else value
    conn.close()
    return status, out, raw


def page_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def login(server, email, password):
    status, headers, _ = request(server, "POST", "/login", fields={"email": email, "password": password})
    assert status in (302, 303) and "Set-Cookie" in headers, f"登录失败: {email} -> {status}"
    return headers["Set-Cookie"].split(";", 1)[0]


def tree_modes(root: Path) -> dict:
    entries = {str(root): stat.S_IMODE(root.stat().st_mode)}
    for path in root.rglob("*"):
        entries[str(path)] = stat.S_IMODE(path.stat().st_mode)
    return entries


class T1HttpSecurityTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls._saved_paths = (app_module.ROOT, app_module.UPLOAD_DIR)
        real_root = app_module.ROOT
        app_module.ROOT = cls.root
        app_module.UPLOAD_DIR = cls.root / "data" / "uploads"
        # 公开参考客户端是源码内静态资源：复制进临时 ROOT，隔离运行不影响仓库文件。
        shutil.copytree(real_root / "reference-client", cls.root / "reference-client")
        cls.db_path = cls.root / "t1-security.db"
        cls.server = make_server(cls.db_path, port=0)
        cls.db = cls.server.RequestHandlerClass.db
        cls.models = cls.server.RequestHandlerClass.models
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.base_origin = f"http://127.0.0.1:{cls.port}"

        def restore_module_paths():
            app_module.ROOT, app_module.UPLOAD_DIR = cls._saved_paths

        cls.addClassCleanup(cls.temp.cleanup)
        cls.addClassCleanup(restore_module_paths)
        cls.addClassCleanup(cls.db.conn.close)
        cls.addClassCleanup(cls.server.server_close)

        cls.db.seed_demo_users()
        cls.admin_cookie = login(cls.server, ADMIN_EMAIL, ADMIN_PASSWORD)
        cls.db.ensure_tenant("other-tenant", "其他企业（T1 测试）")
        cls.db.create_user("other-tenant", "other-admin@example.invalid", "对方管理员", "admin", "other-admin-pass", "t1-setup")
        cls.other_admin_cookie = login(cls.server, "other-admin@example.invalid", "other-admin-pass")

    # ── 帮助 ──────────────────────────────────────────────────────────────

    def make_employee(self, email, modules=()):
        user_id = self.db.create_user(TENANT, email, email.split("@")[0], "employee", "t1-pass-12345", "t1-setup")
        if modules:
            self.db.set_user_modules(TENANT, user_id, list(modules), "t1-setup")
        return user_id, login(self.server, email, "t1-pass-12345")

    def published_product(self, model="T1-PUMP", name="T1 审查用例泵"):
        product_id = self.db.create_product(TENANT, model, name, "T1 用途")
        self.db.conn.execute(
            "INSERT INTO product_versions(id, tenant_id, product_id, version, snapshot_json, status, created_by, created_at, published_at) "
            "VALUES (?, ?, ?, 1, ?, 'published', 't1-setup', ?, ?)",
            ("pv-" + product_id[:12], TENANT, product_id,
             json.dumps({"fields": [], "public_sources": [], "missing_fields": []}),
             "2026-10-13T00:00:00+00:00", "2026-10-13T00:00:00+00:00"),
        )
        self.db.conn.execute("UPDATE products SET status='published' WHERE tenant_id=? AND id=?", (TENANT, product_id))
        self.db.conn.commit()
        return product_id

    # ── A. 登录页演示口令公开（已观察项 1）───────────────────────────────

    def test_login_page_hides_demo_credentials_in_normal_deployment(self):
        """普通部署（demo_tools 关闭）登录页不得公开任何演示账号与口令。"""
        status, _, raw = request(self.server, "GET", "/login")
        page = page_text(raw)
        self.assertEqual(status, 200)
        self.assertIn("create_admin", page, "首位管理员创建指引应保留")
        self.assertNotIn("demo-admin", page, "登录页不得出现演示账号名")
        self.assertNotIn("密码与账号相同", page, "登录页不得公开演示口令规则")

    def test_fresh_database_has_no_default_accounts(self):
        """普通新库不预置账号：演示口令不可登录（根因侧的边界验证）。"""
        from app import Database
        fresh = Database(self.root / "fresh-empty.db")
        try:
            self.assertEqual(fresh.list_users(TENANT), [])
            self.assertIsNone(fresh.authenticate_user(ADMIN_EMAIL, ADMIN_PASSWORD))
        finally:
            fresh.conn.close()

    # ── B. 登录/买方 Cookie 无 Secure（已观察项 2）──────────────────────────

    def test_session_cookie_is_secure_behind_https_proxy(self):
        """反代声明 https（X-Forwarded-Proto）时会话 Cookie 必须带 Secure。"""
        status, headers, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"X-Forwarded-Proto": "https"},
        )
        self.assertIn(status, (302, 303))
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("cb_session=", cookie)
        self.assertIn("; Secure", cookie, "https 下的会话 Cookie 必须带 Secure")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_buyer_cookie_is_secure_behind_https_proxy(self):
        """反代声明 https 时买方会话 Cookie 必须带 Secure。"""
        status, headers, _ = request(
            self.server, "GET", "/inquiry?lang=en",
            extra_headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("cb_buyer_", cookie)
        self.assertIn("; Secure", cookie, "https 下的买方 Cookie 必须带 Secure")

    def test_session_cookie_stays_usable_on_plain_http_local(self):
        """本地直连 http（无反代声明）保持可用的非 Secure Cookie，避免破坏本地开发。"""
        status, headers, _ = request(self.server, "POST", "/login", fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        self.assertIn(status, (302, 303))
        cookie_header = headers.get("Set-Cookie", "")
        self.assertNotIn("; Secure", cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        status2, _, _ = request(self.server, "GET", "/admin", cookie=cookie)
        self.assertEqual(status2, 200)

    # ── C. 跨站 POST 无 Origin 校验（已观察项 3）────────────────────────────

    def test_admin_write_post_rejects_cross_site_origin(self):
        """带会话的跨站 POST（浏览器会带 Origin）不得产生任何写入。"""
        email = "t1-cross-origin-write@example.invalid"
        status, _, _ = request(
            self.server, "POST", "/admin/users",
            fields={"email": email, "display_name": "跨站写入", "role": "employee", "password": "t1-pass-12345"},
            extra_headers={"Origin": "https://evil.example"},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 403, "跨站来源的写操作必须被拒绝")
        row = self.db.conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        self.assertIsNone(row, "跨站 POST 不得创建任何账号")

    def test_admin_write_post_allows_same_origin_and_no_origin(self):
        """同源 Origin 与不带 Origin 的非浏览器调用保持既有行为（不误杀）。"""
        same_email = "t1-same-origin-write@example.invalid"
        status, headers, _ = request(
            self.server, "POST", "/admin/users",
            fields={"email": same_email, "display_name": "同源写入", "role": "employee", "password": "t1-pass-12345"},
            extra_headers={"Origin": self.base_origin},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 303)
        self.assertTrue(self.db.conn.execute("SELECT 1 FROM users WHERE email=?", (same_email,)).fetchone())
        cli_email = "t1-no-origin-write@example.invalid"
        status2, _, _ = request(
            self.server, "POST", "/admin/users",
            fields={"email": cli_email, "display_name": "无 Origin 写入", "role": "employee", "password": "t1-pass-12345"},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status2, 303)
        self.assertTrue(self.db.conn.execute("SELECT 1 FROM users WHERE email=?", (cli_email,)).fetchone())

    def test_login_post_rejects_cross_site_origin(self):
        """登录 CSRF：跨站登录请求不得发放会话。"""
        status, headers, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers, "跨站登录请求不得收到会话 Cookie")

    def test_inquiry_message_post_rejects_cross_site_origin(self):
        """买方旧询价页：跨站提交不得写入消息。"""
        status, headers, _ = request(self.server, "GET", "/inquiry?lang=en")
        self.assertEqual(status, 303)
        conversation_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        before = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=? AND conversation_id=?", (TENANT, conversation_id)
        ).fetchone()[0]
        status2, _, _ = request(
            self.server, "POST", f"/inquiry/{conversation_id}/message",
            fields={"message": "cross-site probe"},
            extra_headers={"Origin": "https://evil.example"},
            cookie=cookie,
        )
        self.assertEqual(status2, 403)
        after = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=? AND conversation_id=?", (TENANT, conversation_id)
        ).fetchone()[0]
        self.assertEqual(before, after, "跨站提交不得产生消息")
        # 同源提交仍可用（行为不回归）
        status3, _, _ = request(
            self.server, "POST", f"/inquiry/{conversation_id}/message",
            fields={"message": "same-origin probe"},
            extra_headers={"Origin": self.base_origin},
            cookie=cookie,
        )
        self.assertIn(status3, (302, 303))
        self.assertTrue(self.db.conn.execute(
            "SELECT 1 FROM messages WHERE tenant_id=? AND conversation_id=? AND content='same-origin probe'",
            (TENANT, conversation_id),
        ).fetchone())

    # ── C2. 同源判定收紧（第二轮：协议/主机/有效端口、转发 Host 不可信、Sec-Fetch-Site）──

    def test_same_origin_gate_does_not_trust_forwarded_host(self):
        """伪造 X-Forwarded-Host 不得把外站 Origin 放行：只认请求自身 Host。"""
        status, headers, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Origin": "https://evil.invalid", "X-Forwarded-Host": "evil.invalid"},
        )
        self.assertEqual(status, 403, "转发 Host 不得作为来源判定依据")
        self.assertNotIn("Set-Cookie", headers, "跨站登录请求不得获得会话 Cookie")

    def test_same_origin_gate_requires_matching_scheme(self):
        """协议必须匹配：HTTP 直连请求携带 https Origin 视为跨源；反代声明 https 后同源表单照常可用。"""
        status, headers, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Origin": f"https://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 403, "https Origin 落在 http 请求上必须被拒绝")
        self.assertNotIn("Set-Cookie", headers)
        status2, _, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Origin": f"https://127.0.0.1:{self.port}", "X-Forwarded-Proto": "https"},
        )
        self.assertIn(status2, (302, 303), "反代声明 https 时同源 https 表单照常受理")

    def test_same_origin_gate_compares_effective_port(self):
        """有效端口参与比较：缺省端口按协议归一（https:443），端口不同即跨源。"""
        status, _, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Host": "example.invalid:443", "Origin": "https://example.invalid", "X-Forwarded-Proto": "https"},
        )
        self.assertIn(status, (302, 303), "Host 显式 443 与缺省端口应等价")
        status2, headers2, _ = request(
            self.server, "POST", "/login",
            fields={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            extra_headers={"Host": "example.invalid", "Origin": "https://example.invalid:8443", "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status2, 403, "端口不一致不得当作同源")
        self.assertNotIn("Set-Cookie", headers2)

    def test_missing_origin_cross_site_fetch_metadata_is_rejected(self):
        """无 Origin 但浏览器声明 Sec-Fetch-Site: cross-site：照拒；无该头的 CLI 调用不受影响。"""
        email = "t1-fetch-metadata@example.invalid"
        status, _, _ = request(
            self.server, "POST", "/admin/users",
            fields={"email": email, "display_name": "跨站元数据", "role": "employee", "password": "t1-pass-12345"},
            extra_headers={"Sec-Fetch-Site": "cross-site"},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 403, "跨站 Fetch 元数据不得产生写入")
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone())
        ok_email = "t1-fetch-same-origin@example.invalid"
        status2, _, _ = request(
            self.server, "POST", "/admin/users",
            fields={"email": ok_email, "display_name": "同站元数据", "role": "employee", "password": "t1-pass-12345"},
            extra_headers={"Sec-Fetch-Site": "same-origin"},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status2, 303, "同源的 Sec-Fetch-Site 正常放行")
        self.assertTrue(self.db.conn.execute("SELECT 1 FROM users WHERE email=?", (ok_email,)).fetchone())

    def test_handoff_exchange_scheme_mismatch_is_not_accepted(self):
        """交接换取的同源判定与后台同口径：http 请求携带 https 同主机 Origin 必须拒绝，且不消费交接码。"""
        site_key = self.db.create_buyer_site(TENANT, "https://t1-shop.example")
        product_id = self.published_product(model="T1-HANDOFF-SCHEME", name="T1 交接协议用例泵")
        status, _, raw = request(
            self.server, "POST", "/api/v1/buyer/conversations",
            json_body={"site_key": site_key, "locale": "en"},
            extra_headers={"Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status, 201)
        created = json.loads(raw)
        conversation_id, token = created["conversation_id"], created["access_token"]
        status1, _, raw1 = request(
            self.server, "POST", f"/api/v1/buyer/conversations/{conversation_id}/handoff-codes",
            json_body={"product_id": product_id},
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status1, 201)
        code = json.loads(raw1)["code"]
        status2, _, _ = request(
            self.server, "POST", "/api/v1/buyer/handoff/exchange",
            json_body={"code": code, "product_id": product_id},
            extra_headers={"X-Site-Key": site_key, "Origin": f"https://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status2, 403, "协议不符的同主机来源不得换取凭据")
        status3, _, raw3 = request(
            self.server, "POST", "/api/v1/buyer/handoff/exchange",
            json_body={"code": code, "product_id": product_id},
            extra_headers={"X-Site-Key": site_key, "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status3, 200, "被拒请求不得消费交接码；正确的同源来源照常换取")
        self.assertEqual(json.loads(raw3)["conversation_id"], conversation_id)

    def test_buyer_api_same_host_page_scheme_consistency(self):
        """买方 API：登记企业网站 Origin 白名单照常受理；平台同主机页面按协议/端口判定。"""
        site_key = self.db.create_buyer_site(TENANT, "https://t1-shop.example")
        status, _, raw = request(
            self.server, "POST", "/api/v1/buyer/conversations",
            json_body={"site_key": site_key, "locale": "en"},
            extra_headers={"Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status, 201, "登记来源（HTTPS 白名单）必须照常受理")
        created = json.loads(raw)
        conversation_id, token = created["conversation_id"], created["access_token"]
        status1, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status1, 200, "平台同主机同协议页面照常可用")
        status2, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": f"https://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status2, 403, "协议不符的同主机来源不得当作同源放行")

    # ── D. 备份/日志输出权限（已观察项 4/5，同属 umask 泄漏类）──────────────

    def _build_backup_fixture(self, root: Path):
        import hashlib
        uploads = root / "uploads"
        uploads.mkdir(parents=True)
        db = app_module.Database(root / "app.db")
        db.ensure_tenant(TENANT, "示例水泵企业（演示）")
        product_id = db.create_product(TENANT, "T1-BACKUP", "T1 备份用例", "")
        source_id = db.add_source(TENANT, product_id, "manual.pdf", "PDF", "Page 1")
        contents = b"t1 backup sample bytes"
        filename = f"{source_id}-manual.pdf"
        (uploads / filename).write_bytes(contents)
        db.conn.execute(
            "UPDATE source_documents SET storage_path=?, sha256=? WHERE id=?",
            (f"data/uploads/{filename}", hashlib.sha256(contents).hexdigest(), source_id),
        )
        db.conn.commit()
        db.conn.close()
        return db, uploads

    def test_backup_output_permissions_are_owner_only(self):
        """备份产物（数据库+原件）不得随 umask 变成其他本地用户可读。"""
        old_umask = os.umask(0o022)
        try:
            fixture = self.root / "backup-case"
            fixture.mkdir()
            _db, uploads = self._build_backup_fixture(fixture)
            destination = fixture / "bundle-out"
            backup(fixture / "app.db", uploads, destination)
        finally:
            os.umask(old_umask)
        modes = tree_modes(destination)
        for path, mode in modes.items():
            if Path(path).is_dir():
                self.assertEqual(mode, 0o700, f"备份目录过宽: {path} -> {oct(mode)}")
            else:
                self.assertEqual(mode, 0o600, f"备份文件过宽: {path} -> {oct(mode)}")

    def test_restore_check_output_permissions_are_owner_only(self):
        """隔离恢复产物同样只允许属主访问。"""
        old_umask = os.umask(0o022)
        try:
            fixture = self.root / "restore-case"
            fixture.mkdir()
            _db, uploads = self._build_backup_fixture(fixture)
            bundle = fixture / "bundle"
            backup(fixture / "app.db", uploads, bundle)
            restored = fixture / "restored"
            restore_and_verify(bundle, restored)
        finally:
            os.umask(old_umask)
        modes = tree_modes(restored)
        for path, mode in modes.items():
            if Path(path).is_dir():
                self.assertEqual(mode, 0o700, f"恢复目录过宽: {path} -> {oct(mode)}")
            else:
                self.assertEqual(mode, 0o600, f"恢复文件过宽: {path} -> {oct(mode)}")

    def test_debug_log_file_permissions_are_owner_only(self):
        """诊断日志含买方消息摘要，落盘权限不得随 umask 放宽。"""
        (self.root / "data").mkdir(exist_ok=True)
        os.environ["WHATSAPP_PERSONAL_DEBUG"] = "1"
        try:
            app_module._whatsapp_personal_debug({"at": "t1", "body": "买家消息摘要", "outcome": "t1"})
        finally:
            os.environ.pop("WHATSAPP_PERSONAL_DEBUG", None)
        log_path = self.root / "data" / "whatsapp_personal_events.log"
        self.assertTrue(log_path.is_file(), "诊断日志应已写入")
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600, "诊断日志必须只允许属主读写")

    # ── D2. 新写入默认属主权限（第二轮：业务库与私有原件不再随 umask）────────

    def test_new_application_database_is_owner_only(self):
        """新建业务库（含连接配置与客户隐私）默认 0600；既有库在打开时不改权限（不修改现有数据）。"""
        from app import Database
        fresh_path = self.root / "perm-fresh-app.db"
        old_umask = os.umask(0o022)
        try:
            fresh = Database(fresh_path)
        finally:
            os.umask(old_umask)
        try:
            self.assertEqual(stat.S_IMODE(fresh_path.stat().st_mode), 0o600, "新业务库必须只允许属主读写")
        finally:
            fresh.conn.close()
        # 既有库（已存在的文件）打开时不改权限：不改动现有业务数据（原目录权限由 T4 复核）。
        os.chmod(fresh_path, 0o644)
        reopened = Database(fresh_path, initialize=False)
        try:
            self.assertEqual(stat.S_IMODE(fresh_path.stat().st_mode), 0o644, "不得擅改既有库权限")
        finally:
            reopened.conn.close()

    def test_new_upload_files_are_owner_only(self):
        """产品来源原件：新写入文件 0600、新建私有目录 0700（umask=022 下也收紧）。"""
        saved_upload = app_module.UPLOAD_DIR
        app_module.UPLOAD_DIR = self.root / "perm-uploads"
        old_umask = os.umask(0o022)
        try:
            product_id = self.db.create_product(TENANT, "T1-PERM-UP", "T1 权限用例（原件）", "")
            source_id = self.db.add_source(
                TENANT, product_id, "t1-perm.txt", "other", "T1 权限探针", content=b"t1 private originals probe",
            )
        finally:
            os.umask(old_umask)
            app_module.UPLOAD_DIR = saved_upload
        row = self.db.conn.execute("SELECT storage_path FROM source_documents WHERE id=?", (source_id,)).fetchone()
        target = self.root / row["storage_path"]
        self.assertTrue(target.is_file())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600, "新写入的原件必须只允许属主读写")
        self.assertEqual(stat.S_IMODE((self.root / "perm-uploads").stat().st_mode), 0o700, "新建私有目录必须只允许属主访问")

    def test_new_knowledge_files_are_owner_only(self):
        """企业知识两批导入的原件：新文件 0600、新建目录 0700（私有资料写点同口径）。"""
        admin_id = self.db.conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()["id"]
        self.db.knowledge_storage_root = self.root / "perm-kb"
        old_umask = os.umask(0o022)
        try:
            imported = self.db.import_knowledge_file(
                TENANT, admin_id, filename="t1-perm-probe.txt", data=b"t1 knowledge probe",
            )
            batch_id = self.db.kb2_create_batch(
                TENANT, admin_id, [{"kind": "file", "filename": "t1-perm-batch.txt", "data": b"t1 kb2 probe"}],
                process_now=False,
            )
        finally:
            os.umask(old_umask)
        kb_row = self.db.conn.execute(
            "SELECT storage_path FROM knowledge_sources WHERE tenant_id=? AND id=?", (TENANT, imported["source_id"])
        ).fetchone()
        self.assertEqual(stat.S_IMODE(Path(kb_row["storage_path"]).stat().st_mode), 0o600, "知识原件必须只允许属主读写")
        kb2_row = self.db.conn.execute(
            "SELECT storage_path FROM kb2_sources WHERE tenant_id=? AND batch_id=?", (TENANT, batch_id)
        ).fetchone()
        self.assertEqual(stat.S_IMODE(Path(kb2_row["storage_path"]).stat().st_mode), 0o600, "批次原件必须只允许属主读写")
        self.assertEqual(stat.S_IMODE((self.root / "perm-kb").stat().st_mode), 0o700, "新建知识目录必须只允许属主访问")
        self.assertEqual(stat.S_IMODE((self.root / "perm-kb" / "phase2").stat().st_mode), 0o700, "新建批次目录必须只允许属主访问")

    # ── D3. 短写复现（第三轮：单次 os.write 短写不得丢数据）───────────────────

    def test_private_writes_are_complete_under_single_short_os_write(self):
        """故障注入：底层单次 write 只落前 3 字节时，私有原件/日志仍必须完整（且保持 0600）。

        复现（修复前）：write_private_bytes/_append_private_log 单次 os.write 且无视返回值——
        payload 10 字节、底层只写 3 字节时函数「成功」返回而文件只剩 3 字节（控制组直接
        单次 os.write 证明注入生效）。修复后：经 os.fdopen 二进制文件对象写入，由文件对象
        负责写完整；句柄关闭后权限仍为 0600。
        """
        payload = b"0123456789"  # 10 字节（与 Codex 复现一致）
        line = "0123456789\n"
        control = self.root / "short-write-control.bin"
        target = self.root / "short-write-private.bin"
        log_target = self.root / "short-write-private.log"
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, data[:3])

        with patch("app.os.write", side_effect=short_write):
            fd = os.open(control, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload)  # 控制组：单次 os.write 在注入下只落前 3 字节
            finally:
                os.close(fd)
            app_module.write_private_bytes(target, payload)
            app_module._append_private_log(log_target, line)
        self.assertEqual(control.read_bytes(), payload[:3], "故障注入应生效：直接单次 os.write 只落前 3 字节")
        self.assertEqual(target.read_bytes(), payload, "故障注入下私有原件必须完整落盘")
        self.assertEqual(log_target.read_bytes(), line.encode("utf-8"), "故障注入下日志行必须完整落盘")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600, "私有原件必须保持 0600")
        self.assertEqual(stat.S_IMODE(log_target.stat().st_mode), 0o600, "日志文件必须保持 0600")

    # ── E. 入口矩阵：匿名、模块授权、管理面 ────────────────────────────────

    ADMIN_GET_PATHS = (
        "/admin", "/admin/users", "/admin/settings", "/admin/audit", "/admin/analytics",
        "/admin/export/rfqs.csv", "/admin/sites", "/admin/products", "/admin/tasks", "/admin/rfqs",
        "/admin/sales", "/admin/creative", "/admin/channels", "/admin/customer-transfers",
        "/admin/inbox/web", "/admin/knowledge", "/admin/preferences", "/admin/nav-counts",
    )

    def test_anonymous_admin_surface_requires_login(self):
        for path in self.ADMIN_GET_PATHS:
            with self.subTest(path=path):
                status, headers, _ = request(self.server, "GET", path)
                self.assertEqual(status, 303, path)
                self.assertTrue(headers.get("Location", "").startswith("/login"), path)
        status, headers, _ = request(self.server, "POST", "/admin/users", fields={"email": "x@example.invalid"})
        self.assertEqual(status, 303)
        self.assertTrue(headers.get("Location", "").startswith("/login"))
        status2, headers2, _ = request(self.server, "POST", "/admin/material-tasks")
        self.assertEqual(status2, 303)
        self.assertTrue(headers2.get("Location", "").startswith("/login"))

    def test_zero_module_employee_denied_every_module_and_admin_area(self):
        _uid, cookie = self.make_employee("t1-zero-module@example.invalid")
        for path in self.ADMIN_GET_PATHS:
            if path in ("/admin", "/admin/knowledge", "/admin/preferences", "/admin/nav-counts"):
                continue  # 设计约定：概览、企业知识阅读、模型设置页（只读状态）为登录即可
            with self.subTest(path=path):
                status, _, raw = request(self.server, "GET", path, cookie=cookie)
                self.assertEqual(status, 403, path)
                self.assertIn("无权限", page_text(raw))
        # 设计约定内的两个页面保持可读
        for path in ("/admin", "/admin/knowledge", "/admin/preferences"):
            status, _, _ = request(self.server, "GET", path, cookie=cookie)
            self.assertEqual(status, 200, path)
        # 写入口同样被拦（企业知识编辑需要产品管理/管理员）
        status, _, _ = request(self.server, "POST", "/admin/knowledge/new", fields={"title": "x"}, cookie=cookie)
        self.assertEqual(status, 403)

    def test_single_module_grant_is_scoped(self):
        _uid, cookie = self.make_employee("t1-sales-only@example.invalid", modules=["sales"])
        status, _, _ = request(self.server, "GET", "/admin/sales", cookie=cookie)
        self.assertEqual(status, 200)
        for path in ("/admin/rfqs", "/admin/tasks", "/admin/inbox/web", "/admin/users", "/admin/channels"):
            status, _, _ = request(self.server, "GET", path, cookie=cookie)
            self.assertEqual(status, 403, path)

    def test_admin_can_open_module_pages(self):
        for path in self.ADMIN_GET_PATHS:
            with self.subTest(path=path):
                status, _, _ = request(self.server, "GET", path, cookie=self.admin_cookie)
                self.assertEqual(status, 200, path)

    # ── F. 跨租户对象隔离（会话/RFQ/任务/原件/连接）─────────────────────────

    def test_cross_tenant_objects_are_invisible(self):
        conversation_id = self.db.create_conversation(TENANT, "web")
        message_id = self.db.save_message(TENANT, conversation_id, "buyer", "跨企业探针")
        self.db.create_rfq(
            TENANT, conversation_id, "t1-cross-tenant",
            {"contact_email": "buyer@example.invalid", "contact_phone": "+49 30 555 0000", "quantity": "1", "region": "DE"},
            "T1 probe",
        )
        self.db.conn.execute(
            "INSERT INTO human_tasks(id, tenant_id, conversation_id, question_message_id, question, reason, status, created_at, updated_at) "
            "VALUES ('t1-task-x', ?, ?, ?, '跨企业任务探针', 't1', 'pending', '2026-10-13T00:00:00+00:00', '2026-10-13T00:00:00+00:00')",
            (TENANT, conversation_id, message_id),
        )
        product_id = self.db.create_product(TENANT, "T1-CROSS", "跨企业产品", "")
        source_id = self.db.add_source(TENANT, product_id, "t1-cross.txt", "other", "T1", content=b"t1 cross tenant bytes", actor="demo-admin")
        kb2_batch_id = "b10000000000000000000001"
        kb2_source_file = self.root / "t1-kb2-source.txt"
        kb2_source_file.write_bytes(b"t1 kb2 bytes")
        kb2_source_id = "b20000000000000000000001"
        self.db.conn.execute(
            "INSERT INTO kb2_batches(id, tenant_id, status, created_by, created_at, updated_at) VALUES (?, ?, 'draft', 't1-setup', '2026-10-13T00:00:00+00:00', '2026-10-13T00:00:00+00:00')",
            (kb2_batch_id, TENANT),
        )
        self.db.conn.execute(
            "INSERT INTO kb2_sources(id, tenant_id, batch_id, kind, filename, format, storage_path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'file', 't1-kb2-source.txt', 'txt', ?, 'ready', '2026-10-13T00:00:00+00:00', '2026-10-13T00:00:00+00:00')",
            (kb2_source_id, TENANT, kb2_batch_id, str(kb2_source_file)),
        )
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "T1 连接", self.db.conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()[0], ADMIN_EMAIL)
        self.db.conn.commit()

        rfq_id = self.db.conn.execute("SELECT id FROM rfqs WHERE tenant_id=? AND conversation_id=?", (TENANT, conversation_id)).fetchone()[0]
        other = self.other_admin_cookie
        checks = (
            ("GET", f"/admin/inbox/web/{conversation_id}", None),
            ("GET", f"/admin/rfqs/{rfq_id}", None),
            ("GET", "/admin/tasks/t1-task-x", None),
            ("GET", f"/admin/knowledge/sources/{source_id}", None),
            ("GET", f"/admin/knowledge/source/{kb2_source_id}/file", None),
            ("GET", f"/admin/channels/accounts/{account_id}", None),
            ("POST", f"/admin/inbox/web/{conversation_id}/reply", {"body": "x"}),
        )
        before_messages = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=?", (TENANT,)
        ).fetchone()[0]
        for method, path, fields in checks:
            with self.subTest(path=path):
                status, _, _ = request(self.server, method, path, fields=fields, cookie=other)
                # GET 为 404；写入口为 400 通用兜底（无任何跨企业写入），均属拒绝。
                self.assertIn(status, (400, 404), path)
        after_messages = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=?", (TENANT,)
        ).fetchone()[0]
        self.assertEqual(before_messages, after_messages, "跨企业请求不得产生任何写入")
        # 本企业管理员在同一路径上可用，证明缺口确为“跨企业不可见”而非路径不存在
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/sources/{source_id}", cookie=self.admin_cookie)
        self.assertEqual(status, 200)
        status, _, _ = request(self.server, "GET", f"/admin/knowledge/source/{kb2_source_id}/file", cookie=self.admin_cookie)
        self.assertEqual(status, 200)

    # ── G. 停用/撤权/转交即时权限 ───────────────────────────────────────────

    def test_deactivated_member_session_rejected_immediately(self):
        user_id, cookie = self.make_employee("t1-deactivate@example.invalid", modules=["sales"])
        status, _, _ = request(self.server, "GET", "/admin/sales", cookie=cookie)
        self.assertEqual(status, 200)
        self.db.set_user_active(TENANT, user_id, False, "t1-setup")
        status2, headers2, _ = request(self.server, "GET", "/admin/sales", cookie=cookie)
        self.assertEqual(status2, 303)
        self.assertTrue(headers2.get("Location", "").startswith("/login"))

    def test_module_revocation_effective_immediately(self):
        user_id, cookie = self.make_employee("t1-revoke@example.invalid", modules=["sales"])
        status, _, _ = request(self.server, "GET", "/admin/sales", cookie=cookie)
        self.assertEqual(status, 200)
        self.db.set_user_modules(TENANT, user_id, [], "t1-setup")
        status2, _, _ = request(self.server, "GET", "/admin/sales", cookie=cookie)
        self.assertEqual(status2, 403)

    def test_customer_transfer_grants_view_not_send(self):
        prev_id, _prev_cookie = self.make_employee("t1-transfer-prev@example.invalid", modules=["sales", "inbox"])
        succ_id, succ_cookie = self.make_employee("t1-transfer-succ@example.invalid", modules=["sales", "inbox"])
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "T1 转交账号", prev_id, ADMIN_EMAIL)
        conversation_id = self.db.create_conversation(TENANT, "langbot")
        self.db.conn.execute(
            "UPDATE conversations SET owner_user_id=?, channel_account_id=? WHERE tenant_id=? AND id=?",
            (prev_id, account_id, TENANT, conversation_id),
        )
        self.db.bind_channel_identity(TENANT, "langbot", "t1-ext-user", "T1 客户", conversation_id, account_id)
        self.db.update_channel_identity_meta(TENANT, "langbot", "t1-ext-user", {"platform": "Telegram", "adapter": "telegram"})
        self.db.conn.commit()

        status, _, _ = request(
            self.server, "POST", "/admin/customer-transfers",
            fields={"conversation_id": conversation_id, "previous_user_id": prev_id, "successor_user_id": succ_id},
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 303)
        # 接替人获得历史访问权（可看）
        status, _, _ = request(self.server, "GET", f"/admin/inbox/telegram/{conversation_id}", cookie=succ_cookie)
        self.assertEqual(status, 200)
        # 但未获原账号连接/发送权限：回复被 403 拦下，且不落消息
        before = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=? AND conversation_id=? AND source='staff-reply'",
            (TENANT, conversation_id),
        ).fetchone()[0]
        status2, _, _ = request(
            self.server, "POST", f"/admin/inbox/telegram/{conversation_id}/reply",
            fields={"body": "T1 转交后发送探针"},
            cookie=succ_cookie,
        )
        self.assertEqual(status2, 403)
        after = self.db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE tenant_id=? AND conversation_id=? AND source='staff-reply'",
            (TENANT, conversation_id),
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertTrue(self.db.can_view_conversation(TENANT, conversation_id, succ_id))
        self.assertFalse(self.db.can_send_conversation(TENANT, conversation_id, succ_id))
        self.assertTrue(self.db.can_send_conversation(TENANT, conversation_id, prev_id))

    # ── J. 出站总线投递前的发送人复核（第二轮：撤权后不得再投递）────────────

    def admin_user_id(self) -> str:
        return self.db.conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()["id"]

    def channel_conversation(self, owner_email: str, label: str = "T1 复核账号"):
        """建一个企业连接（负责人=指定员工）与连接的渠道会话（含外部身份绑定）。"""
        owner = self.db.conn.execute(
            "SELECT id FROM users WHERE tenant_id=? AND email=?", (TENANT, owner_email)
        ).fetchone()
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", label, owner["id"], ADMIN_EMAIL)
        conversation_id = app_module.get_or_create_channel_conversation(
            self.db, TENANT, "langbot", f"ext-{secrets.token_hex(6)}", "T1 客户", account_id=account_id,
        )
        return account_id, conversation_id

    def test_dispatch_rechecks_module_grant_after_revoke(self):
        """已复现缺口：员工全部模块被清空后，排队消息仍被投递（发送器被调用）。
        修复后：实际投递前复核模块与对象权限——不调用发送器，消息落「发送失败」。"""
        email = "t1-dispatch-revoke@example.invalid"
        user_id, _cookie = self.make_employee(email, modules=["inbox", "channels"])
        _account_id, conversation_id = self.channel_conversation(email, "T1 撤权复核账号")
        message_id = self.db.save_message(
            TENANT, conversation_id, "staff", "卖家回复：撤权后不应发出", "staff-reply",
            channel="langbot", delivery_status="queued", actor_email=email,
        )
        self.db.set_user_modules(TENANT, user_id, [], "t1-setup")
        with patch("app.send_channel_text", return_value="fake-receipt") as send:
            sent, error = app_module.dispatch_outbound(self.db, TENANT, message_id)
        self.assertFalse(sent, "模块被撤销后不得投递")
        self.assertFalse(send.called, "模块被撤销后不得调用外发通道")
        row = self.db.conn.execute(
            "SELECT delivery_status, delivery_error FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        self.assertEqual(row["delivery_status"], "failed", "阻止投递必须留下可见失败状态")
        self.assertIn("模块", row["delivery_error"])

    def test_dispatch_rechecks_module_for_each_staff_send_source(self):
        """覆盖各发送来源（inbox/tasks/sales）：模块在位可投递；撤销后同一条消息不得再投递。"""
        cases = (
            ("inbox", "staff-reply"),
            ("inbox", "rfq-link"),
            ("tasks", "human-engineer"),
            ("tasks", "engineer-followup:t1-task"),
            ("sales", "lead-reply"),
        )
        for module, source in cases:
            with self.subTest(source=source):
                email = f"t1-src-{secrets.token_hex(4)}@example.invalid"
                user_id, _ = self.make_employee(email, modules=[module])
                _account_id, conversation_id = self.channel_conversation(email, f"T1 {module} 来源")
                message_id = self.db.save_message(
                    TENANT, conversation_id, "staff", "卖家回复：来源复核探针", source,
                    channel="langbot", delivery_status="queued", actor_email=email,
                )
                with patch("app.send_channel_text", return_value="receipt") as send:
                    sent, error = app_module.dispatch_outbound(self.db, TENANT, message_id)
                self.assertTrue(sent, f"{source} 在模块在位时应可投递：{error}")
                self.assertTrue(send.called)
                # 撤掉模块 → 同一条消息重置为排队后不得再投递
                self.db.conn.execute("UPDATE messages SET delivery_status='queued' WHERE id=?", (message_id,))
                self.db.conn.commit()
                self.db.set_user_modules(TENANT, user_id, [], "t1-setup")
                with patch("app.send_channel_text", return_value="receipt") as send2:
                    sent2, _error2 = app_module.dispatch_outbound(self.db, TENANT, message_id)
                self.assertFalse(sent2, f"{source} 在模块撤销后不得投递")
                self.assertFalse(send2.called, f"{source} 撤权后不得调用外发通道")

    def test_dispatch_blocks_deactivated_sender_and_revoked_member(self):
        """停用与连接撤权即时生效：投递前复核不通过则不调用发送器（锁定既有口径）。"""
        email = "t1-dispatch-off@example.invalid"
        user_id, _ = self.make_employee(email, modules=["inbox"])
        _account_id, conversation_id = self.channel_conversation(email, "T1 停用复核账号")
        message_id = self.db.save_message(
            TENANT, conversation_id, "staff", "卖家回复：停用探针", "staff-reply",
            channel="langbot", delivery_status="queued", actor_email=email,
        )
        self.db.set_user_active(TENANT, user_id, False, "t1-setup")
        with patch("app.send_channel_text", return_value="receipt") as send:
            sent, _error = app_module.dispatch_outbound(self.db, TENANT, message_id)
        self.assertFalse(sent)
        self.assertFalse(send.called)
        # 连接成员被移除：对象权限即时失效（成员在位时可投递）
        member_email = "t1-dispatch-member@example.invalid"
        member_id, _ = self.make_employee(member_email, modules=["inbox"])
        owner_email = "t1-dispatch-owner-b@example.invalid"
        owner_id, _ = self.make_employee(owner_email, modules=["channels"])
        account_id = self.db.create_business_connection(TENANT, "langbot", "Telegram", "T1 成员复核账号", owner_id, ADMIN_EMAIL)
        conversation_id2 = app_module.get_or_create_channel_conversation(
            self.db, TENANT, "langbot", f"ext-{secrets.token_hex(6)}", "T1 客户", account_id=account_id,
        )
        self.db.grant_connection(TENANT, account_id, member_id, self.admin_user_id(), grant=True)
        message_id2 = self.db.save_message(
            TENANT, conversation_id2, "staff", "卖家回复：成员探针", "staff-reply",
            channel="langbot", delivery_status="queued", actor_email=member_email,
        )
        with patch("app.send_channel_text", return_value="receipt") as send2:
            sent2, error2 = app_module.dispatch_outbound(self.db, TENANT, message_id2)
        self.assertTrue(sent2, f"连接成员在位时应可投递：{error2}")
        self.db.grant_connection(TENANT, account_id, member_id, self.admin_user_id(), grant=False)
        self.db.conn.execute("UPDATE messages SET delivery_status='queued' WHERE id=?", (message_id2,))
        self.db.conn.commit()
        with patch("app.send_channel_text", return_value="receipt") as send3:
            sent3, _error3 = app_module.dispatch_outbound(self.db, TENANT, message_id2)
        self.assertFalse(sent3)
        self.assertFalse(send3.called)

    def test_dispatch_system_messages_without_actor_still_send(self):
        """无 actor_email 的系统消息（如 AI 应答）不参与人工权限复核（正常渠道来源保持可用）。"""
        email = "t1-dispatch-sysowner@example.invalid"
        user_id, _ = self.make_employee(email, modules=["channels"])
        _account_id, conversation_id = self.channel_conversation(email, "T1 系统消息账号")
        message_id = self.db.save_message(
            TENANT, conversation_id, "assistant", "自动应答文本", "grounded-answer",
            channel="langbot", delivery_status="queued",
        )
        with patch("app.send_channel_text", return_value="receipt") as send:
            sent, error = app_module.dispatch_outbound(self.db, TENANT, message_id)
        self.assertTrue(sent, error)
        self.assertTrue(send.called)

    def test_dispatch_admin_and_retry_paths_unchanged(self):
        """管理员（非连接负责人）发送与失败重试保持既有业务规则（本轮不收紧管理员路径）。"""
        email = "t1-dispatch-owner-c@example.invalid"
        user_id, _ = self.make_employee(email, modules=["inbox"])
        _account_id, conversation_id = self.channel_conversation(email, "T1 管理员路径账号")
        admin_message = self.db.save_message(
            TENANT, conversation_id, "staff", "卖家回复：管理员探针", "staff-reply",
            channel="langbot", delivery_status="queued", actor_email=ADMIN_EMAIL,
        )
        with patch("app.send_channel_text", return_value="") as send:
            sent, error = app_module.dispatch_outbound(self.db, TENANT, admin_message)
        self.assertTrue(sent, error)
        self.assertTrue(send.called)
        # 失败重试：failed → queued → 投递（retry_outbound_message 状态机不变）
        retry_message = self.db.save_message(
            TENANT, conversation_id, "staff", "卖家回复：重试探针", "staff-reply",
            channel="langbot", delivery_status="failed", actor_email=email,
        )
        with patch("app.send_channel_text", return_value="") as send2:
            sent2, error2 = app_module.retry_outbound_message(self.db, TENANT, retry_message, ADMIN_EMAIL)
        self.assertTrue(sent2, error2)
        self.assertTrue(send2.called)

    # ── H. 买方凭据与跨站交接 ───────────────────────────────────────────────

    def test_buyer_inquiry_detail_requires_cookie(self):
        status, headers, _ = request(self.server, "GET", "/inquiry?lang=en")
        self.assertEqual(status, 303)
        conversation_id = urlsplit(headers["Location"]).path.rsplit("/", 1)[1]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status2, _, _ = request(self.server, "GET", f"/inquiry/{conversation_id}")
        self.assertEqual(status2, 404, "无凭据不得读取会话")
        status3, _, _ = request(self.server, "GET", f"/inquiry/{conversation_id}", cookie=cookie)
        self.assertEqual(status3, 200)
        # 另一浏览器（无凭据）同样 404
        status4, _, _ = request(self.server, "GET", f"/inquiry/{conversation_id}", cookie="cb_buyer_%s=wrong-token" % conversation_id)
        self.assertEqual(status4, 404)

    def test_buyer_api_requires_site_key_and_bearer(self):
        site_key = self.db.create_buyer_site(TENANT, "https://t1-shop.example")
        status, _, raw = request(
            self.server, "POST", "/api/v1/buyer/conversations",
            json_body={"site_key": site_key, "locale": "en"},
            extra_headers={"Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status, 201)
        created = json.loads(raw)
        conversation_id, token = created["conversation_id"], created["access_token"]
        # 无 Bearer → 401
        status2, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status2, 401)
        # 错误站点键 → 404
        status3, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": "bogus-site-key", "Authorization": f"Bearer {token}", "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status3, 404)
        # 跨站来源 → 403
        status4, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        )
        self.assertEqual(status4, 403)
        # 正确凭据 → 200
        status5, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status5, 200)

    def test_handoff_code_is_single_use_and_bound(self):
        site_key = self.db.create_buyer_site(TENANT, "https://t1-shop.example")
        product_id = self.published_product(model="T1-HANDOFF", name="T1 交接用例泵")
        other_product_id = self.published_product(model="T1-HANDOFF-2", name="T1 交接用例泵二")
        status, _, raw = request(
            self.server, "POST", "/api/v1/buyer/conversations",
            json_body={"site_key": site_key, "locale": "en"},
            extra_headers={"Origin": "https://t1-shop.example"},
        )
        created = json.loads(raw)
        conversation_id, token = created["conversation_id"], created["access_token"]
        status1, _, raw1 = request(
            self.server, "POST", f"/api/v1/buyer/conversations/{conversation_id}/handoff-codes",
            json_body={"product_id": product_id},
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {token}", "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status1, 201)
        code = json.loads(raw1)["code"]
        # 绑定不符 → 403（不消费有效代码）
        status2, _, _ = request(
            self.server, "POST", "/api/v1/buyer/handoff/exchange",
            json_body={"code": code, "product_id": other_product_id},
            extra_headers={"X-Site-Key": site_key},
        )
        self.assertEqual(status2, 403)
        # 平台页面来源换取（无跨站 Origin）→ 200
        status3, _, raw3 = request(
            self.server, "POST", "/api/v1/buyer/handoff/exchange",
            json_body={"code": code, "product_id": product_id},
            extra_headers={"X-Site-Key": site_key},
        )
        self.assertEqual(status3, 200)
        exchanged = json.loads(raw3)
        self.assertEqual(exchanged["conversation_id"], conversation_id)
        # 一次性：同一代码再次换取 → 409
        status4, _, _ = request(
            self.server, "POST", "/api/v1/buyer/handoff/exchange",
            json_body={"code": code, "product_id": product_id},
            extra_headers={"X-Site-Key": site_key},
        )
        self.assertEqual(status4, 409)
        # 交接换取的凭据可直接访问同一会话
        status5, _, _ = request(
            self.server, "GET", f"/api/v1/buyer/conversations/{conversation_id}",
            extra_headers={"X-Site-Key": site_key, "Authorization": f"Bearer {exchanged['access_token']}", "Origin": "https://t1-shop.example"},
        )
        self.assertEqual(status5, 200)

    # ── I. 演示门控与模型密钥 ───────────────────────────────────────────────

    def test_demo_seed_endpoints_disabled_without_demo_mode(self):
        before = len(self.db.list_products(TENANT))
        status, _, raw = request(self.server, "POST", "/admin/products/seed", fields={}, cookie=self.admin_cookie)
        self.assertEqual(status, 403)
        self.assertIn("演示工具未启用", page_text(raw))
        self.assertEqual(len(self.db.list_products(TENANT)), before, "演示端点不得产生数据")
        status2, _, raw2 = request(self.server, "GET", "/admin/products", cookie=self.admin_cookie)
        self.assertEqual(status2, 200)
        self.assertNotIn("导入两款示例水泵", page_text(raw2))

    def test_model_custom_key_not_echoed_and_secret_files_private(self):
        fake_key = "sk-t1-fake-key-not-real"
        self.models.save(TENANT, "https://api.example.com/v1", "deepseek-chat", fake_key)
        try:
            secret_dir = Path(str(self.db_path) + ".model-secrets")
            files = list(secret_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(secret_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            status, _, raw = request(self.server, "GET", "/admin/preferences", cookie=self.admin_cookie)
            page = page_text(raw)
            self.assertEqual(status, 200)
            self.assertNotIn(fake_key, page, "页面不得回显密钥")
            self.assertIn("api.example.com", page, "非敏感字段照常显示")
        finally:
            self.models.disconnect(TENANT)

    def test_reference_client_public_and_no_public_file_direct_links(self):
        status, _, _ = request(self.server, "GET", "/reference-client")
        self.assertEqual(status, 200, "参考客户端为文档化公开演示资产")
        status2, _, _ = request(self.server, "GET", "/files/anything.pdf")
        self.assertEqual(status2, 404, "不提供公开文件直链")


class T1DemoModeLoginPageTests(unittest.TestCase):
    """演示模式（tools/demo/start_demo.py 专用开关）下，登录页保留文档化演示说明。"""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp.name) / "t1-demo-mode.db"
        cls.server = make_server(cls.db_path, port=0, demo_tools=True)
        cls.db = cls.server.RequestHandlerClass.db
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.addClassCleanup(cls.db.conn.close)
        cls.addClassCleanup(cls.server.server_close)

    def test_demo_mode_login_page_keeps_documented_demo_accounts(self):
        status, _, raw = request(self.server, "GET", "/login")
        page = page_text(raw)
        self.assertEqual(status, 200)
        self.assertIn("demo-admin", page)
        self.assertIn("仅限本地演示", page)


if __name__ == "__main__":
    unittest.main()
