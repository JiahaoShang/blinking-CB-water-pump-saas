"""企业业务账号、对象授权和客户交接；不依赖传输服务或界面。"""
import json
import re
import secrets
from datetime import datetime, timezone


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class BusinessChannels:
    def init_business_channels(self):
        self.conn.commit()
        self.conn.execute('PRAGMA foreign_keys = OFF')
        try:
            for table, fields in (
                ('channel_accounts', (
                    ('platform', "TEXT NOT NULL DEFAULT ''"),
                    ('label', "TEXT NOT NULL DEFAULT ''"),
                    ('owner_user_id', "TEXT NOT NULL DEFAULT ''"),
                    ('ai_enabled', 'INTEGER NOT NULL DEFAULT 0'),
                    ('legacy', 'INTEGER NOT NULL DEFAULT 1'),
                    ('remote_account_id', "TEXT NOT NULL DEFAULT ''"),
                )),
                ('conversations', (
                    ('channel_account_id', "TEXT NOT NULL DEFAULT ''"),
                    ('owner_user_id', "TEXT NOT NULL DEFAULT ''"),
                    ('original_owner_user_id', "TEXT NOT NULL DEFAULT ''"),
                )),
                ('channel_identities', (('account_id', "TEXT NOT NULL DEFAULT ''"),)),
            ):
                columns = {row['name'] for row in self.conn.execute(f'PRAGMA table_info({table})')}
                for name, definition in fields:
                    if name not in columns:
                        self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            for table, old_key, replacement in (
                ('channel_accounts', 'tenant_id, channel', ''),
                ('channel_identities', 'tenant_id, channel, external_user_id', ', UNIQUE (tenant_id, channel, account_id, external_user_id)'),
            ):
                sql = self.conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                pattern = r',\s*UNIQUE\s*\(' + re.escape(old_key) + r'\)'
                if re.search(pattern, sql):
                    new_sql = re.sub(pattern, replacement, sql)
                    new_sql = re.sub(r'CREATE TABLE(?: IF NOT EXISTS)?\s+' + table, 'CREATE TABLE ' + table + '_business', new_sql, count=1)
                    self.conn.execute(new_sql)
                    columns = ','.join(row['name'] for row in self.conn.execute(f'PRAGMA table_info({table})'))
                    self.conn.execute(f'INSERT INTO {table}_business ({columns}) SELECT {columns} FROM {table}')
                    self.conn.execute(f'DROP TABLE {table}')
                    self.conn.execute(f'ALTER TABLE {table}_business RENAME TO {table}')
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS connection_members (
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    account_id TEXT NOT NULL REFERENCES channel_accounts(id),
                    user_id TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY (account_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_access (
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    user_id TEXT NOT NULL REFERENCES users(id),
                    granted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_channel_identities_conversation
                    ON channel_identities(tenant_id, conversation_id);
                CREATE INDEX IF NOT EXISTS idx_conversation_account
                    ON conversations(tenant_id, channel_account_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_business_remote_account
                    ON channel_accounts(channel,platform,remote_account_id) WHERE remote_account_id<>'';
            ''')
            # 旧业务负责人有明确证据时才回填；其余历史保留管理员可见。
            self.conn.execute('''UPDATE conversations SET owner_user_id=COALESCE((
                SELECT u.id FROM leads l JOIN users u ON u.email=l.owner AND u.tenant_id=l.tenant_id
                WHERE l.tenant_id=conversations.tenant_id AND l.conversation_id=conversations.id
                ORDER BY l.updated_at DESC LIMIT 1), '') WHERE owner_user_id='' ''')
            self.conn.commit()
            for a in self.conn.execute('SELECT * FROM channel_accounts WHERE legacy=1').fetchall():
                count = self.conn.execute('SELECT COUNT(*) FROM channel_accounts WHERE tenant_id=? AND channel=?', (a['tenant_id'], a['channel'])).fetchone()[0]
                if count == 1:
                    self.conn.execute('''UPDATE conversations SET channel_account_id=? WHERE tenant_id=? AND channel_account_id=''
                        AND id IN (SELECT conversation_id FROM channel_identities WHERE tenant_id=? AND channel=?)''',
                        (a['id'], a['tenant_id'], a['tenant_id'], a['channel']))
            self.conn.commit()
        finally:
            self.conn.execute('PRAGMA foreign_keys = ON')

    def business_user(self, tenant_id, user_id):
        return self.conn.execute('SELECT * FROM users WHERE tenant_id=? AND id=? AND active=1', (tenant_id, user_id)).fetchone()

    def get_business_connection(self, tenant_id, account_id):
        return self.conn.execute('SELECT * FROM channel_accounts WHERE tenant_id=? AND id=?', (tenant_id, account_id)).fetchone()

    def list_business_connections(self, tenant_id, user_id):
        user = self.business_user(tenant_id, user_id)
        if not user:
            return []
        if user['role'] == 'admin':
            return self.conn.execute('SELECT * FROM channel_accounts WHERE tenant_id=? ORDER BY created_at,id', (tenant_id,)).fetchall()
        return self.conn.execute('''SELECT a.* FROM channel_accounts a WHERE a.tenant_id=? AND
            (a.owner_user_id=? OR EXISTS (SELECT 1 FROM connection_members m WHERE m.tenant_id=a.tenant_id AND m.account_id=a.id AND m.user_id=?))
            ORDER BY a.created_at,a.id''', (tenant_id, user_id, user_id)).fetchall()

    def create_business_connection(self, tenant_id, channel, platform, label, owner_user_id, actor):
        admin = self.conn.execute('SELECT * FROM users WHERE tenant_id=? AND email=? AND active=1', (tenant_id, actor)).fetchone()
        owner = self.business_user(tenant_id, owner_user_id)
        if not admin or not owner or (admin['role'] != 'admin' and (admin['id'] != owner_user_id or 'channels' not in self.get_user_modules(admin['id']))):
            raise ValueError('无权创建此账号连接')
        if channel not in {'langbot', 'whatsapp', 'whatsapp_personal'} or platform not in {'Telegram', 'WhatsApp', 'Discord', '企业微信'}:
            raise ValueError('不支持的平台')
        if (channel == 'langbot' and platform == 'WhatsApp') or (channel != 'langbot' and platform != 'WhatsApp'):
            raise ValueError('平台与接入方式不一致')
        if not str(label).strip():
            raise ValueError('请填写业务账号名称')
        account_id = secrets.token_hex(12)
        config = {'webhook_token': secrets.token_hex(24)}
        if channel == 'whatsapp_personal':
            config['session'] = 'business-' + account_id
        now = timestamp()
        self.conn.execute('''INSERT INTO channel_accounts
            (id,tenant_id,channel,status,config_json,created_by,created_at,updated_by,updated_at,platform,label,owner_user_id,ai_enabled,legacy)
            VALUES (?,?,?,'enabled',?,?,?,?,?,?,?,?,0,0)''',
            (account_id, tenant_id, channel, json.dumps(config), actor, now, actor, now, platform, str(label).strip()[:100], owner_user_id))
        self._audit(tenant_id, 'channel', account_id, 'connection_created', actor, '企业业务账号；AI 默认关闭')
        self.conn.commit()
        return account_id

    def can_manage_connection(self, tenant_id, account_id, user_id):
        user = self.business_user(tenant_id, user_id)
        account = self.get_business_connection(tenant_id, account_id)
        if not user or not account:
            return False
        if user['role'] == 'admin':
            return True
        if 'channels' not in self.get_user_modules(user_id):
            return False
        return account['owner_user_id'] == user_id or bool(self.conn.execute(
            'SELECT 1 FROM connection_members WHERE tenant_id=? AND account_id=? AND user_id=?', (tenant_id, account_id, user_id)).fetchone())

    def grant_connection(self, tenant_id, account_id, user_id, actor_id, grant=True):
        actor = self.business_user(tenant_id, actor_id)
        member = self.business_user(tenant_id, user_id)
        if not actor or actor['role'] != 'admin' or not member or not self.get_business_connection(tenant_id, account_id):
            raise ValueError('仅本企业管理员可授权本企业成员')
        if grant:
            self.conn.execute('INSERT OR IGNORE INTO connection_members VALUES (?,?,?)', (tenant_id, account_id, user_id))
        else:
            self.conn.execute('DELETE FROM connection_members WHERE tenant_id=? AND account_id=? AND user_id=?', (tenant_id, account_id, user_id))
        self._audit(tenant_id, 'channel', account_id, 'connection_granted' if grant else 'connection_revoked', actor['email'], user_id)
        self.conn.commit()

    def set_connection_ai(self, tenant_id, account_id, enabled, user_id, confirmed=False):
        if not self.can_manage_connection(tenant_id, account_id, user_id):
            raise ValueError('无权管理此账号')
        if enabled and not confirmed:
            raise ValueError('开启前请确认用于问答的企业资料')
        user = self.business_user(tenant_id, user_id)
        a = self.get_business_connection(tenant_id, account_id)
        config = json.loads(a['config_json'])
        config['ai_managed'] = True
        self.conn.execute('UPDATE channel_accounts SET ai_enabled=?, config_json=?,updated_by=?,updated_at=? WHERE tenant_id=? AND id=?',
                          (int(enabled), json.dumps(config), user['email'], timestamp(), tenant_id, account_id))
        self._audit(tenant_id, 'channel', account_id, 'ai_enabled' if enabled else 'ai_paused', user['email'], '已确认对外问答资料' if enabled else '')
        self.conn.commit()

    def conversation_scope(self, tenant_id, user_id, alias='c'):
        user = self.business_user(tenant_id, user_id)
        if not user:
            return '0=1', []
        if user['role'] == 'admin':
            return '1=1', []
        return f'''({alias}.owner_user_id=? OR EXISTS (
            SELECT 1 FROM conversation_access v WHERE v.tenant_id={alias}.tenant_id AND v.conversation_id={alias}.id AND v.user_id=?)
            OR EXISTS (SELECT 1 FROM channel_accounts a WHERE a.tenant_id={alias}.tenant_id AND a.id={alias}.channel_account_id
                AND (a.owner_user_id=? OR EXISTS (SELECT 1 FROM connection_members m WHERE m.tenant_id=a.tenant_id AND m.account_id=a.id AND m.user_id=?))))''', [user_id] * 4

    def can_view_conversation(self, tenant_id, conversation_id, user_id):
        clause, args = self.conversation_scope(tenant_id, user_id)
        return bool(self.conn.execute(f'SELECT 1 FROM conversations c WHERE c.tenant_id=? AND c.id=? AND {clause}', [tenant_id, conversation_id, *args]).fetchone())

    def can_send_conversation(self, tenant_id, conversation_id, user_id):
        c = self.get_conversation(tenant_id, conversation_id)
        if not c or not self.can_view_conversation(tenant_id, conversation_id, user_id):
            return False
        user = self.business_user(tenant_id, user_id)
        if user['role'] == 'admin' or not c['channel_account_id']:
            return True
        a = self.get_business_connection(tenant_id, c['channel_account_id'])
        return bool(a and (a['owner_user_id'] == user_id or self.conn.execute('SELECT 1 FROM connection_members WHERE tenant_id=? AND account_id=? AND user_id=?', (tenant_id, a['id'], user_id)).fetchone()))

    def can_view_business_record(self, tenant_id, record, user_id):
        user = self.business_user(tenant_id, user_id)
        if not user or not record:
            return False
        if user['role'] == 'admin':
            return True
        if record['conversation_id']:
            return self.can_view_conversation(tenant_id, record['conversation_id'], user_id)
        return 'owner' in record.keys() and record['owner'] == user['email']

    def transfer_customer(self, tenant_id, conversation_id, previous_user_id, successor_user_id, actor_id):
        actor = self.business_user(tenant_id, actor_id)
        previous = self.business_user(tenant_id, previous_user_id)
        successor = self.business_user(tenant_id, successor_user_id)
        if conversation_id.startswith('lead:'):
            lead_id = conversation_id[5:]
            lead = self.get_lead(tenant_id, lead_id)
            if not actor or actor['role'] != 'admin' or not previous or not successor or not lead or lead['conversation_id']:
                raise ValueError('仅本企业管理员可转交本企业独立客户线索')
            if previous_user_id == successor_user_id or lead['owner'] != previous['email']:
                raise ValueError('原负责人已变化或接替人与原负责人相同，请刷新后重试')
            with self.conn:
                result = self.conn.execute('UPDATE leads SET owner=?,updated_at=? WHERE tenant_id=? AND id=? AND owner=?', (successor['email'], timestamp(), tenant_id, lead_id, previous['email']))
                if result.rowcount != 1:
                    raise ValueError('负责人已变化，请刷新后重试')
                self._audit(tenant_id, 'lead', lead_id, 'customer_transferred', actor['email'], json.dumps({'previous_owner': previous_user_id, 'successor': successor_user_id, 'scope': '独立客户线索及其未完成跟进；历史操作者保留'}, ensure_ascii=False))
            return
        c = self.get_conversation(tenant_id, conversation_id)
        if not actor or actor['role'] != 'admin' or not previous or not successor or not c:
            raise ValueError('仅本企业管理员可转交给本企业有效成员')
        if previous_user_id == successor_user_id or c['owner_user_id'] != previous_user_id:
            raise ValueError('原负责人已变化或接替人与原负责人相同，请刷新后重试')
        # 同一会话代表当前可验证的客户边界；不按姓名或邮箱猜测跨会话客户。
        with self.conn:
            result = self.conn.execute('''UPDATE conversations SET owner_user_id=?,
                original_owner_user_id=CASE WHEN original_owner_user_id='' THEN owner_user_id ELSE original_owner_user_id END,
                updated_at=? WHERE tenant_id=? AND id=? AND owner_user_id=?''',
                (successor_user_id, timestamp(), tenant_id, conversation_id, previous_user_id))
            if result.rowcount != 1:
                raise ValueError('负责人已变化，请刷新后重试')
            self.conn.execute('INSERT OR REPLACE INTO conversation_access VALUES (?,?,?,?,?)', (tenant_id, conversation_id, successor_user_id, actor['email'], timestamp()))
            self.conn.execute('UPDATE leads SET owner=?,updated_at=? WHERE tenant_id=? AND conversation_id=?', (successor['email'], timestamp(), tenant_id, conversation_id))
            self._audit(tenant_id, 'conversation', conversation_id, 'customer_transferred', actor['email'], json.dumps({
                'previous_owner': previous_user_id, 'successor': successor_user_id,
                'scope': '客户会话、关联询价、线索及未完成跟进；原账号发送权限不变',
            }, ensure_ascii=False))
