"""界面渐进呈现的边界与原功能保留检查。"""
from html.parser import HTMLParser
import unittest
from console_guidance import guided_console_body


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag in ('form', 'input', 'select', 'textarea', 'button', 'a'):
            self.items.append((tag, tuple(attrs)))


class GuidanceTests(unittest.TestCase):
    def test_excluded_channels_unchanged(self):
        body = '<h1>业务</h1><form action="/save"><button>保存</button></form>'
        for path, active in [('/admin/channels', 'channels'), ('/admin/inbox/telegram', 'inbox_telegram'), ('/admin/inbox/whatsapp', 'inbox_whatsapp'), ('/admin/inbox/whatsapp_personal', 'inbox_whatsapp_personal')]:
            self.assertEqual(guided_console_body(body, path, active), body)

    def test_business_controls_remain_exactly_intact(self):
        body = '<h1>设置</h1><section class="card"><h2>线索阶段</h2><form action="/admin/settings/stages" method="post"><input name="label_new" required><select name="stage"><option>新线索</option></select><textarea name="note"></textarea><button>保存</button></form><a href="#history">历史</a></section>'
        old, new = Controls(), Controls()
        old.feed(body)
        new.feed(guided_console_body(body, '/admin/settings', 'settings'))
        self.assertEqual(old.items, new.items)

    def test_error_and_public_pages_unchanged(self):
        body = '<div class="error">无权操作</div>'
        self.assertEqual(guided_console_body(body, '/admin/products', 'products'), body)
        public = '<h1>买方产品</h1>'
        self.assertEqual(guided_console_body(public, '/products/pump', 'products'), public)


if __name__ == '__main__':
    unittest.main()
