"""企业账号、对象权限与客户移交：独立企业与双连接真实 SQLite 验证。"""
import json
import tempfile
import unittest
import threading
from unittest.mock import patch
from pathlib import Path

from app import Database


class BusinessChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'app.db')
        self.db.seed_demo_users()
        self.tenant = 'demo-tenant'
        self.admin, self.a, self.b = [self.db.authenticate_user(email, password) for email, password in (
            ('demo-admin@example.invalid', 'demo-admin'),
            ('demo-sales@example.invalid', 'demo-sales'),
            ('demo-engineer@example.invalid', 'demo-engineer'),
        )]
        self.db.set_user_modules(self.tenant, self.a['id'], ['inbox', 'channels', 'sales', 'rfqs'], self.admin['email'])
        self.db.set_user_modules(self.tenant, self.b['id'], ['inbox', 'channels', 'sales', 'rfqs'], self.admin['email'])

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def connection(self, user, name='业务号', platform='Telegram'):
        return self.db.create_business_connection(self.tenant, 'langbot', platform, name, user['id'], self.admin['email'])

    def test_two_connections_do_not_replace_and_default_ai_off(self):
        a, b = self.connection(self.a), self.connection(self.b)
        self.assertNotEqual(a, b)
        rows = self.db.list_business_connections(self.tenant, self.admin['id'])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row['ai_enabled'] for row in rows))
        self.assertIsNone(self.db.get_channel_account(self.tenant, 'langbot'))

    def test_member_only_own_connection_and_revocation(self):
        a, b = self.connection(self.a), self.connection(self.b)
        self.assertTrue(self.db.can_manage_connection(self.tenant, a, self.a['id']))
        self.assertFalse(self.db.can_manage_connection(self.tenant, b, self.a['id']))
        self.assertTrue(self.db.can_manage_connection(self.tenant, b, self.admin['id']))
        self.db.set_user_modules(self.tenant, self.a['id'], ['inbox'], self.admin['email'])
        self.assertFalse(self.db.can_manage_connection(self.tenant, a, self.a['id']))

    def test_ai_requires_explicit_knowledge_confirmation(self):
        a = self.connection(self.a)
        with self.assertRaises(ValueError):
            self.db.set_connection_ai(self.tenant, a, True, self.a['id'], confirmed=False)
        self.db.set_connection_ai(self.tenant, a, True, self.a['id'], confirmed=True)
        self.assertEqual(self.db.get_business_connection(self.tenant, a)['ai_enabled'], 1)
        self.db.set_connection_ai(self.tenant, a, False, self.a['id'])
        self.assertEqual(self.db.get_business_connection(self.tenant, a)['ai_enabled'], 0)

    def test_identity_and_conversation_scoped_by_account(self):
        from app import get_or_create_channel_conversation
        a, b = self.connection(self.a), self.connection(self.b)
        ca = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer-1', '客户', account_id=a)
        cb = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer-1', '客户', account_id=b)
        self.assertNotEqual(ca, cb)
        self.assertTrue(self.db.can_view_conversation(self.tenant, ca, self.a['id']))
        self.assertFalse(self.db.can_view_conversation(self.tenant, cb, self.a['id']))
        self.assertEqual(self.db.get_conversation(self.tenant, ca)['channel_account_id'], a)

    def test_cross_tenant_owner_rejected(self):
        self.db.ensure_tenant('other')
        with self.assertRaises(ValueError):
            self.db.create_business_connection('other', 'langbot', 'Telegram', '越权', self.a['id'], self.admin['email'])

    def test_transfer_atomic_and_preserves_history(self):
        from app import get_or_create_channel_conversation
        a = self.connection(self.a)
        c = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer', '客户', account_id=a)
        self.db.save_message(self.tenant, c, 'staff', '原负责人回复', 'staff-reply', actor_email=self.a['email'])
        with self.assertRaises(ValueError):
            self.db.transfer_customer(self.tenant, c, self.b['id'], self.a['id'], self.admin['id'])
        self.assertFalse(self.db.can_view_conversation(self.tenant, c, self.b['id']))
        self.db.transfer_customer(self.tenant, c, self.a['id'], self.b['id'], self.admin['id'])
        self.assertTrue(self.db.can_view_conversation(self.tenant, c, self.b['id']))
        self.assertFalse(self.db.can_send_conversation(self.tenant, c, self.b['id']))
        self.assertEqual(self.db.list_messages(self.tenant, c)[0]['actor_email'], self.a['email'])
        audit = self.db.conn.execute("SELECT * FROM audit_log WHERE action='customer_transferred'").fetchone()
        self.assertEqual(audit['actor'], self.admin['email'])
        self.assertEqual(json.loads(audit['details'])['previous_owner'], self.a['id'])

    def test_non_admin_transfer_rejected(self):
        c = self.db.create_conversation(self.tenant, 'web', 'en')
        with self.assertRaises(ValueError):
            self.db.transfer_customer(self.tenant, c, self.a['id'], self.b['id'], self.a['id'])

    def test_legacy_reopen_preserves_connection_and_messages(self):
        self.db.save_channel_account(self.tenant, 'whatsapp_personal', {'session': 'legacy'}, self.admin['email'])
        a = self.db.get_channel_account(self.tenant, 'whatsapp_personal')['id']
        self.db.init_schema()
        self.assertEqual(self.db.get_business_connection(self.tenant, a)['id'], a)
        self.assertEqual(len(self.db.list_business_connections(self.tenant, self.admin['id'])), 1)

    def test_inbound_off_still_saved_and_dedup_scoped(self):
        from app import process_channel_message
        a, b = self.connection(self.a), self.connection(self.b)
        first = process_channel_message(self.db, None, self.tenant, 'langbot', 'same-user', '客户', '询问流量?', 'same-message', a)
        second = process_channel_message(self.db, None, self.tenant, 'langbot', 'same-user', '客户', '询问流量?', 'same-message', b)
        retry = process_channel_message(self.db, None, self.tenant, 'langbot', 'same-user', '客户', '询问流量?', 'same-message', a)
        self.assertEqual(first['action'], 'ai_disabled')
        self.assertEqual(second['action'], 'ai_disabled')
        self.assertNotEqual(first['conversation_id'], second['conversation_id'])
        self.assertEqual(retry['action'], 'duplicate')
        self.assertEqual(len(self.db.list_messages(self.tenant, first['conversation_id'])), 1)

    def test_reply_uses_original_account_configuration(self):
        from app import get_or_create_channel_conversation, enqueue_outbound
        a, b = self.connection(self.a), self.connection(self.b)
        for account_id, bot in ((a, 'bot-a'), (b, 'bot-b')):
            config = json.loads(self.db.get_business_connection(self.tenant, account_id)['config_json'])
            config['bot_uuid'] = bot
            self.db.conn.execute('UPDATE channel_accounts SET config_json=? WHERE id=?', (json.dumps(config), account_id))
        self.db.conn.commit()
        ca = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'same-user', '客户', account_id=a)
        cb = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'same-user', '客户', account_id=b)
        with patch('app.send_channel_text', return_value='') as send:
            for c, actor in ((ca, self.a), (cb, self.b)):
                _, sent, error = enqueue_outbound(self.db, self.tenant, self.db.get_conversation(self.tenant, c), 'staff', '回复', 'staff-reply', actor_email=actor['email'])
                self.assertTrue(sent, error)
            self.assertEqual([call.args[1]['bot_uuid'] for call in send.call_args_list], ['bot-a', 'bot-b'])

    def test_transfer_moves_associated_rfq_and_followup(self):
        from app import get_or_create_channel_conversation, now_iso
        a = self.connection(self.a)
        c = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer', '客户', account_id=a)
        self.db.conn.execute("INSERT INTO rfqs(id,tenant_id,conversation_id,submission_key,snapshot_json,created_at) VALUES ('rfq-transfer',?,?, 'transfer','{}',?)", (self.tenant, c, now_iso()))
        self.db.conn.execute("INSERT INTO leads(id,tenant_id,rfq_id,conversation_id,owner,next_follow_up,note,created_at,updated_at) VALUES ('lead-transfer',?,'rfq-transfer',?,?, '2030-01-01','待跟进',?,?)", (self.tenant, c, self.a['email'], now_iso(), now_iso()))
        self.db.conn.commit()
        self.db.transfer_customer(self.tenant, c, self.a['id'], self.b['id'], self.admin['id'])
        lead = self.db.get_lead(self.tenant, 'lead-transfer')
        self.assertEqual(lead['owner'], self.b['email'])
        self.assertEqual(lead['next_follow_up'], '2030-01-01')
        self.assertTrue(self.db.can_view_business_record(self.tenant, self.db.get_rfq(self.tenant, 'rfq-transfer'), self.b['id']))

    def test_invalid_connection_leaves_no_orphan_conversation(self):
        from app import get_or_create_channel_conversation
        before = self.db.conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
        with self.assertRaises(ValueError):
            get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer', '客户', account_id='a' * 24)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], before)

    def test_independent_customer_transfer_keeps_followup_and_history(self):
        lead_id = self.db.create_nurture_lead(self.tenant, self.a['email'], '测试客户', '测试企业', 'buyer@example.invalid', '', 'visit', '原有跟进记录', consent_confirmed=True)
        self.db.conn.execute('UPDATE leads SET next_follow_up=? WHERE id=?', ('2030-01-01', lead_id))
        self.db.conn.commit()
        self.db.transfer_customer(self.tenant, 'lead:' + lead_id, self.a['id'], self.b['id'], self.admin['id'])
        lead = self.db.get_lead(self.tenant, lead_id)
        self.assertEqual(lead['owner'], self.b['email'])
        self.assertEqual(lead['note'], '原有跟进记录')
        self.assertEqual(lead['next_follow_up'], '2030-01-01')
        audit = self.db.conn.execute("SELECT * FROM audit_log WHERE entity_id=? AND action='customer_transferred'", (lead_id,)).fetchone()
        self.assertIn(self.a['id'], audit['details'])

    def test_internal_knowledge_revokes_answer_and_remote_document_immediately(self):
        from app import answer_question_with_mode, collect_kb_documents
        from knowledge_delivery import usable_document_ids, authorized_documents
        self.db.seed_demo_products(self.tenant)
        product = self.db.list_products(self.tenant)[0]
        for field in self.db.get_fields(self.tenant, product['id']):
            if field['state'] == 'candidate':
                self.db.approve_field(self.tenant, product['id'], field['field_key'])
        self.db.finalize_product_review(self.tenant, product['id'])
        self.db.publish_product(self.tenant, product['id'])
        current = authorized_documents(self.db, self.tenant)
        self.db.conn.execute("INSERT INTO kb_sync_state(tenant_id,product_id,document_id,content_hash,synced_at,last_error,indexing_status,batch) VALUES (?,?, 'remote-doc',?,'now','','completed','batch')", (self.tenant, product['id'], current[product['id']]))
        self.db.conn.commit()
        self.assertIn('remote-doc', usable_document_ids(self.db, self.tenant))
        self.db.conn.execute("UPDATE product_fields SET audience='internal' WHERE tenant_id=? AND product_id=? AND field_key='flow_range'", (self.tenant, product['id']))
        self.db.conn.commit()
        self.assertNotIn('remote-doc', usable_document_ids(self.db, self.tenant))
        status, *_ = answer_question_with_mode(self.db, None, self.tenant, '', 'What is the flow range?', product['id'])
        self.assertEqual(status, 'needs_human')
        self.assertTrue(all('流量范围' not in d['text'] for d in collect_kb_documents(self.db, self.tenant)))


class BusinessChannelHTTPTests(BusinessChannelTests):
    def setUp(self):
        super().setUp()
        from app import make_server
        from test_langbot_bridge import login
        self.server = make_server(Path(self.tmp.name) / 'http.db', port=0)
        self.server.RequestHandlerClass.db.conn.close()
        self.server.RequestHandlerClass.db = self.db
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies = {u['id']: login(self.server, u['email'], password) for u, password in ((self.admin, 'demo-admin'), (self.a, 'demo-sales'), (self.b, 'demo-engineer'))}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, method, path, user, fields=None):
        from test_langbot_bridge import form_request
        return form_request(self.server, method, path, fields, self.cookies[user['id']])

    def test_ui_hides_components_and_requires_ai_confirmation(self):
        a = self.connection(self.a)
        code, _, page = self.request('GET', '/admin/channels', self.a)
        self.assertEqual(code, 200)
        self.assertIn('业务号', page)
        for technical in ('LangBot', 'Dify', 'bot_uuid', 'API Key', 'webhook'):
            self.assertNotIn(technical, page)
        code, _, page = self.request('GET', '/admin/channels/accounts/' + a, self.a)
        self.assertEqual(code, 200)
        self.assertIn('<dialog', page)
        self.assertIn('开启前请确认企业资料', page)
        code, _, _ = self.request('POST', '/admin/channels/accounts/' + a + '/ai', self.a, {'enabled': '1'})
        self.assertEqual(code, 400)
        self.assertEqual(self.db.get_business_connection(self.tenant, a)['ai_enabled'], 0)
        code, _, _ = self.request('POST', '/admin/channels/accounts/' + a + '/ai', self.a, {'enabled': '1', 'confirmed': '1'})
        self.assertEqual(code, 303)

    def test_member_cannot_read_or_post_to_other_account_history(self):
        from app import handle_langbot_event
        a = self.connection(self.a)
        event = {'uuid': 'secret-message', 'event_type': 'bot.person_message', 'data': {'bot_uuid': 'bot-a', 'adapter_name': 'telegram', 'sender': {'id': 'customer'}, 'message': [{'type': 'Plain', 'text': '仅A可见的业务消息'}]}}
        result = handle_langbot_event(self.db, None, self.tenant, event, a)
        path = '/admin/inbox/telegram/' + result['conversation_id']
        code, _, page = self.request('GET', path, self.b)
        self.assertEqual(code, 404)
        self.assertNotIn('仅A可见的业务消息', page)
        code, _, _ = self.request('POST', path + '/reply', self.b, {'body': '越权回复'})
        self.assertEqual(code, 404)
        code, _, page = self.request('GET', '/admin/inbox/telegram', self.b)
        self.assertEqual(code, 200)
        self.assertNotIn('仅A可见的业务消息', page)
        code, _, page = self.request('GET', path, self.admin)
        self.assertEqual(code, 200)
        self.assertIn('仅A可见的业务消息', page)

    def test_knowledge_readable_without_module_and_write_still_blocked(self):
        self.db.set_user_modules(self.tenant, self.b['id'], [], self.admin['email'])
        code, _, page = self.request('GET', '/admin/knowledge', self.b)
        self.assertEqual(code, 200)
        self.assertIn('企业知识', page)
        code, _, _ = self.request('POST', '/admin/products', self.b, {'model': 'deny', 'name': 'deny'})
        self.assertEqual(code, 403)

    def test_transferred_history_cannot_resume_original_account_ai(self):
        from app import get_or_create_channel_conversation
        account = self.connection(self.a)
        conv = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer', '客户', account_id=account)
        self.db.conn.execute('UPDATE channel_identities SET platform_meta=? WHERE conversation_id=?', ('{"adapter":"Telegram"}', conv))
        self.db.conn.commit()
        self.db.transfer_customer(self.tenant, conv, self.a['id'], self.b['id'], self.admin['id'])
        self.assertEqual(self.request('GET', '/admin/inbox/telegram/' + conv, self.b)[0], 200)
        self.assertEqual(self.request('POST', '/admin/inbox/telegram/' + conv + '/ai-toggle', self.b, {})[0], 403)

    def test_nested_reply_cannot_mutate_other_customers_draft(self):
        from app import get_or_create_channel_conversation
        rfqs = []
        for user in (self.a, self.b):
            account = self.connection(user)
            conv = get_or_create_channel_conversation(self.db, self.tenant, 'langbot', 'customer', '客户', account_id=account)
            rfq, _ = self.db.create_rfq(self.tenant, conv, user['id'], {'contact_email':'buyer@example.invalid','contact_phone':'+49123','quantity':'2','region':'DE'}, '测试询价')
            rfqs.append(rfq)
        self.db.create_reply_draft(self.tenant, rfqs[1], 'B 的私有草稿', self.b['email'])
        lead = self.db.get_lead_for_rfq(self.tenant, rfqs[1])
        reply = self.db.conn.execute('SELECT * FROM lead_replies WHERE lead_id=?', (lead['id'],)).fetchone()
        code, _, _ = self.request('POST', f'/admin/sales/{rfqs[0]}/replies/{reply["id"]}', self.a, {'action':'discard'})
        self.assertEqual(code, 404)
        self.assertEqual(self.db.conn.execute('SELECT status FROM lead_replies WHERE id=?', (reply['id'],)).fetchone()[0], 'draft')


if __name__ == '__main__':
    unittest.main()
