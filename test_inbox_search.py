import unittest
import test_business_channels as fixtures
from app import get_or_create_channel_conversation
from inbox_search import search, highlight

class InboxSearchTests(unittest.TestCase):
    setUp = fixtures.BusinessChannelTests.setUp
    tearDown = fixtures.BusinessChannelTests.tearDown
    connection = fixtures.BusinessChannelTests.connection
    def test_acl_channel_and_old_messages(self):
        a = self.connection(self.a, '销售一号')
        b = self.connection(self.b, '销售二号')
        own = get_or_create_channel_conversation(self.db,self.tenant,'langbot','customer-a','张客户',account_id=a)
        hidden = get_or_create_channel_conversation(self.db,self.tenant,'langbot','customer-b','张客户',account_id=b)
        old = self.db.save_message(self.tenant,own,'buyer','旧消息 needle 50%_','test')
        for i in range(60):
            self.db.save_message(self.tenant,own,'buyer','新消息 unrelated '+str(i),'test')
        self.db.ensure_tenant('foreign-enterprise')
        foreign = self.db.create_conversation('foreign-enterprise','langbot')
        self.db.save_message('foreign-enterprise',foreign,'buyer','needle foreign secret','test')
        self.db.save_message(self.tenant,hidden,'buyer','needle secret','test')
        scope,args=self.db.conversation_scope(self.tenant,self.a['id'])
        result=search(self.db,self.tenant,"c.source='langbot' AND "+scope,args,'needle')
        self.assertEqual(list(result),[own])
        self.assertEqual(result[own]['hit_id'],old)
        self.assertEqual(search(self.db,self.tenant,"c.source='web' AND "+scope,args,'needle'),{})
        self.assertEqual(list(search(self.db,self.tenant,scope,args,'销售一号')),[own])
        self.assertEqual(list(search(self.db,self.tenant,scope,args,'张客户')),[own])
        self.assertEqual(list(search(self.db,self.tenant,scope,args,'50%_')),[own])
        self.assertEqual(search(self.db,self.tenant,scope,args,"' OR 1=1 --"),{})
    def test_safe_highlight(self):
        self.assertEqual(highlight('<script>Needle</script>','needle'),'&lt;script&gt;<mark>Needle</mark>&lt;/script&gt;')

class InboxSearchHTTPTests(unittest.TestCase):
    import test_console_presentation as fixture
    setUp = fixture.ConsolePresentationTests.setUp
    tearDown = fixture.ConsolePresentationTests.tearDown
    request = fixture.ConsolePresentationTests.request
    login = fixture.ConsolePresentationTests.login
    def test_all_four_inboxes_search_and_locate(self):
        from urllib.parse import quote
        cookie = self.login()
        admin = self.db.authenticate_user('demo-admin@example.invalid','demo-admin')
        self.db.create_business_connection('demo-tenant','whatsapp_personal','WhatsApp','测试连接',admin['id'],admin['email'])
        for slug,source in [('web','web'),('telegram','langbot'),('whatsapp','whatsapp'),('whatsapp_personal','whatsapp_personal')]:
            if source == 'web':
                conv = self.db.create_conversation('demo-tenant', source)
            else:
                conv = get_or_create_channel_conversation(self.db,'demo-tenant',source,slug+'-user','搜索客户')
                if source == 'langbot':
                    self.db.conn.execute("UPDATE channel_identities SET platform_meta=? WHERE conversation_id=?", ('{"adapter":"Telegram"}',conv))
                    self.db.conn.commit()
            message = self.db.save_message('demo-tenant',conv,'buyer','old NEEDLE <script> 50%_','test')
            self.db.save_message('demo-tenant',conv,'buyer','new unrelated','test')
            status,_,page = self.request('GET','/admin/inbox/'+slug+'?q=needle',cookie=cookie)
            self.assertEqual(status,200)
            self.assertIn('#message-'+message,page)
            self.assertIn('<mark>NEEDLE</mark>',page)
            self.assertIn('搜索当前渠道',page)
            status,_,detail = self.request('GET','/admin/inbox/'+slug+'/'+conv+'?q=needle',cookie=cookie)
            self.assertEqual(status,200)
            self.assertIn("id='message-"+message+"'",detail)
            self.assertIn('scrollIntoView',detail)
            self.assertIn('&lt;script&gt;',detail)
            self.assertIn('没有匹配结果',self.request('GET','/admin/inbox/'+slug+'?q=absent',cookie=cookie)[2])
