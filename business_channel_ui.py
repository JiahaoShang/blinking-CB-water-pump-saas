"""企业用户的账号连接与客户交接界面；组件配置仅由服务端处理。"""
import json
import re


class BusinessChannelUI:
    def business_object_authorized(self, path, method):
        if self.user_role == 'admin':
            return True
        match = re.match(r'/admin/(inbox/[^/]+|rfqs|sales|tasks|leads)/([a-f0-9]+)(?:/|$)', path)
        if not match:
            return True
        area, object_id = match.groups()
        if area.startswith('inbox/'):
            record = self.db.get_conversation(self.tenant_id, object_id)
            allowed = self.db.can_view_conversation(self.tenant_id, object_id, self.user_id)
            conversation_id = object_id
        else:
            getter = {'rfqs': self.db.get_rfq, 'sales': self.db.get_rfq,
                      'tasks': self.db.get_human_task, 'leads': self.db.get_lead}[area]
            record = getter(self.tenant_id, object_id)
            allowed = self.db.can_view_business_record(self.tenant_id, record, self.user_id)
            conversation_id = record['conversation_id'] if record else ''
        if not allowed:
            self.send_html(self.console_page('未找到', '<div class="error">记录不存在或不在你的授权范围内。</div>', ''), 404)
            return False
        if method == 'POST' and any(action in path for action in ('/reply', '/followup', '/rfq-link', '/retry', '/send', '/ai-toggle')):
            if conversation_id and not self.db.can_send_conversation(self.tenant_id, conversation_id, self.user_id):
                self.send_html(self.console_page('无发送权限', '<div class="error">可查看转交历史，但未获原业务账号的发送授权。</div>', ''), 403)
                return False
        return True

    def visible_business_records(self, records):
        return [r for r in records if self.db.can_view_business_record(self.tenant_id, r, self.user_id)]

    def business_summary(self):
        summary = self.db.get_admin_summary(self.tenant_id)
        if self.user_role == 'admin':
            return summary
        tasks = self.visible_business_records(self.db.list_human_tasks(self.tenant_id))
        rfqs = self.visible_business_records(self.db.list_rfqs(self.tenant_id))
        leads = self.visible_business_records(self.db.conn.execute('SELECT * FROM leads WHERE tenant_id=? AND merged_into IS NULL', (self.tenant_id,)).fetchall())
        from app import now_iso
        terminal = {s['key'] for s in self.db.get_lead_stages(self.tenant_id) if s['terminal']}
        summary.update(tasks_total=len(tasks), tasks_pending=sum(t['status'] == 'pending' for t in tasks),
                       rfqs_total=len(rfqs), rfqs_needs_info=sum(r['status'] == 'needs_info' for r in rfqs),
                       leads_active=sum(l['stage'] not in terminal for l in leads),
                       leads_overdue=sum(bool(l['next_follow_up']) and l['next_follow_up'] < now_iso()[:10] and l['stage'] not in terminal for l in leads))
        return summary

    def business_get(self, path, query):
        source_match = re.fullmatch(r'/admin/knowledge/sources/([a-f0-9]+)', path)
        if source_match:
            from app import ROOT, hashlib, quote
            source = self.db.conn.execute('SELECT * FROM source_documents WHERE tenant_id=? AND id=?', (self.tenant_id, source_match.group(1))).fetchone()
            target = (ROOT / source['storage_path']).resolve() if source and source['storage_path'] else None
            if not target or not target.is_relative_to(ROOT.resolve()) or not target.is_file():
                self.send_error(404)
                return True
            content = target.read_bytes()
            if source['sha256'] and hashlib.sha256(content).hexdigest() != source['sha256']:
                self.send_error(409)
                return True
            self.db._audit(self.tenant_id, 'source_document', source['id'], 'enterprise_knowledge_read', self.actor)
            self.db.conn.commit()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(source['filename']))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return True
        if path == '/admin/knowledge':
            self.business_knowledge_page(query)
            return True
        if path == '/admin/customer-transfers':
            self.business_transfer_page(query)
            return True
        match = re.fullmatch(r'/admin/channels/accounts/([a-f0-9]+)(?:/(state|qr))?', path)
        if not match:
            return False
        account_id, action = match.groups()
        a = self.db.get_business_connection(self.tenant_id, account_id)
        if not a or not self.db.can_manage_connection(self.tenant_id, account_id, self.user_id):
            self.send_json({'error': 'not_found'}, 404)
            return True
        if not action:
            self.business_account_page(a, query)
            return True
        from app import whatsapp_personal_session_state, whatsapp_personal_qr_image, ChannelSendError
        if a['channel'] != 'whatsapp_personal':
            self.send_json({'error': 'unsupported'}, 400)
            return True
        config = json.loads(a['config_json'])
        try:
            if action == 'state':
                state = whatsapp_personal_session_state(config)
                self.send_json({'ok': True, 'status': state['status'], 'name': state.get('me_name', ''), 'ai_enabled': bool(a['ai_enabled'])})
            else:
                raw, content_type = whatsapp_personal_qr_image(config)
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)
        except ChannelSendError:
            self.send_json({'ok': False, 'error': '连接暂不可用，请稍后重试或联系平台运营方。'}, 502)
        return True

    def business_post(self, path, form):
        if path == '/admin/channels/accounts':
            platform = str(form.get('platform', ''))
            channel = 'whatsapp_personal' if platform == 'WhatsApp' else 'langbot'
            owner = str(form.get('owner_user_id') or self.user_id)
            account_id = self.db.create_business_connection(self.tenant_id, channel, platform, str(form.get('label', '')), owner, self.actor)
            self.redirect('/admin/channels/accounts/' + account_id)
            return True
        if path == '/admin/customer-transfers':
            self.db.transfer_customer(self.tenant_id, str(form.get('conversation_id', '')), str(form.get('previous_user_id', '')), str(form.get('successor_user_id', '')), self.user_id)
            self.redirect('/admin/customer-transfers?notice=transferred')
            return True
        match = re.fullmatch(r'/admin/channels/accounts/([a-f0-9]+)/(ai|start|disconnect|connect|grant|owner)', path)
        if not match:
            return False
        account_id, action = match.groups()
        a = self.db.get_business_connection(self.tenant_id, account_id)
        if not a or not self.db.can_manage_connection(self.tenant_id, account_id, self.user_id):
            self.send_html(self.console_page('无权限', '<div class="error">连接不存在或未获管理授权。</div>', 'channels'), 403)
            return True
        config = json.loads(a['config_json'])
        from app import whatsapp_personal_start_session, whatsapp_personal_logout_session, ChannelSendError, channel_http_json, channel_api_base
        if action == 'ai':
            self.db.set_connection_ai(self.tenant_id, account_id, form.get('enabled') == '1', self.user_id, form.get('confirmed') == '1')
        elif action == 'grant':
            self.db.grant_connection(self.tenant_id, account_id, str(form.get('user_id', '')), self.user_id, form.get('grant') != '0')
        elif action == 'owner':
            if self.user_role != 'admin':
                raise ValueError('仅管理员可调整账号关联负责人')
            owner = self.db.business_user(self.tenant_id, str(form.get('owner_user_id', '')))
            if not owner:
                raise ValueError('请选择本企业有效成员')
            self.db.conn.execute('UPDATE channel_accounts SET owner_user_id=? WHERE tenant_id=? AND id=?', (owner['id'], self.tenant_id, account_id))
            self.db._audit(self.tenant_id, 'channel', account_id, 'connection_owner_changed', self.actor, owner['id'])
            self.db.conn.commit()
        elif action in ('start', 'disconnect'):
            try:
                if a['channel'] == 'whatsapp_personal':
                    if action == 'start':
                        if not a['legacy']:
                            from channel_runtime import provision_personal_bridge
                            config = provision_personal_bridge(self.db.path, account_id, config)
                            self.db.conn.execute('UPDATE channel_accounts SET config_json=? WHERE tenant_id=? AND id=?', (json.dumps(config), self.tenant_id, account_id))
                            self.db.conn.commit()
                        config.setdefault('webhook_token', __import__('os').environ.get('WHATSAPP_PERSONAL_TOKEN', ''))
                        config['callback_url'] = self.business_callback_base() + '/api/v1/channels/whatsapp-personal/webhook/' + config['webhook_token']
                        whatsapp_personal_start_session(config)
                    else:
                        whatsapp_personal_logout_session(config, restart=False)
                elif a['channel'] == 'langbot' and action == 'disconnect' and config.get('bot_uuid'):
                    self.business_transport_request('DELETE', '/api/v1/platform/bots/' + config['bot_uuid'])
                    config.pop('bot_uuid', None)
                else:
                    raise ValueError('该账号需通过连接向导完成授权')
            except ChannelSendError:
                raise ValueError('连接服务暂不可用，请稍后重试或联系平台运营方')
            if action == 'disconnect':
                self.db.conn.execute("UPDATE channel_accounts SET ai_enabled=0,status='disabled',remote_account_id='' WHERE tenant_id=? AND id=?", (self.tenant_id, account_id))
            else:
                self.db.conn.execute("UPDATE channel_accounts SET status='enabled' WHERE tenant_id=? AND id=?", (self.tenant_id, account_id))
            self.db._audit(self.tenant_id, 'channel', account_id, 'connection_started' if action == 'start' else 'connection_disconnected', self.actor)
            self.db.conn.execute("UPDATE channel_accounts SET config_json=? WHERE tenant_id=? AND id=?", (json.dumps(config), self.tenant_id, account_id))
            self.db.conn.commit()
        elif action == 'connect':
            if a['platform'] != 'Telegram':
                raise ValueError('当前平台暂未提供此授权方式')
            credential = str(form.get('credential', '')).strip()
            if not credential:
                raise ValueError('请输入 Telegram 机器人授权令牌')
            import urllib.request
            try:
                # 先验证外部身份；令牌只交给连接服务，不保存或回显到业务库。
                req = urllib.request.Request('https://api.telegram.org/bot' + credential + '/getMe')
                with urllib.request.urlopen(req, timeout=10) as response:
                    identity = json.load(response)
                if not identity.get('ok'):
                    raise ValueError('授权失败，请检查令牌')
                if config.get('bot_uuid'):
                    raise ValueError('账号已连接，请先断开再连接')
                remote_id = str(identity['result']['id'])
                # 一个外部机器人只能关联一个业务连接，唯一索引阻止并发重复授权。
                self.db.conn.execute('UPDATE channel_accounts SET remote_account_id=? WHERE tenant_id=? AND id=?', (remote_id, self.tenant_id, account_id))
                self.db.conn.commit()
                self.business_provision_webhook(config)
                result = self.business_transport_request('POST', '/api/v1/platform/bots', {
                    'name': 'business-' + account_id, 'description': '企业业务账号', 'adapter': 'telegram', 'enable': True,
                    'adapter_config': {'token': credential, 'markdown_card': False, 'enable-stream-reply': False},
                })
                bot_id = str((result.get('data') or {}).get('uuid') or '')
                if not bot_id:
                    raise ValueError('连接服务未返回账号标识')
                config['bot_uuid'] = bot_id
                config['account_name'] = '@' + str(identity['result'].get('username', ''))
            except Exception:
                self.db.conn.rollback()
                if not config.get('bot_uuid'):
                    self.db.conn.execute("UPDATE channel_accounts SET remote_account_id='' WHERE tenant_id=? AND id=?", (self.tenant_id, account_id))
                    self.db.conn.commit()
                # 上游错误可能含机器人令牌/请求地址，企业界面不能展示原始异常。
                raise ValueError('账号连接未完成，请检查授权令牌；仍失败时联系平台运营方') from None
            self.db.conn.execute("UPDATE channel_accounts SET config_json=?,status='enabled' WHERE tenant_id=? AND id=?", (json.dumps(config), self.tenant_id, account_id))
            self.db._audit(self.tenant_id, 'channel', account_id, 'connection_connected', self.actor)
            self.db.conn.commit()
        self.redirect('/admin/channels/accounts/' + account_id + '?notice=saved')
        return True

    def business_callback_base(self):
        import os
        base = os.environ.get('CHANNEL_CALLBACK_BASE', '').strip().rstrip('/')
        if base:
            return base
        # 本地连接服务运行在 Docker 中；地址不展示给企业成员。
        return 'http://host.docker.internal:' + str(self.server.server_port)

    def business_transport_request(self, method, path, payload=None, user_token=False):
        import os
        import urllib.request
        from app import channel_api_base, ChannelSendError
        key = os.environ.get('LANGBOT_USER_TOKEN' if user_token else 'LANGBOT_API_KEY', '').strip()
        if not key:
            raise ChannelSendError('连接服务未就绪')
        headers = {'Content-Type': 'application/json'}
        headers['Authorization' if user_token else 'X-API-Key'] = 'Bearer ' + key if user_token else key
        raw = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(channel_api_base('langbot', {}) + path, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.load(response)
        except Exception:
            raise ChannelSendError('连接服务暂不可用') from None
        if result.get('code') != 0:
            raise ChannelSendError('连接服务拒绝请求')
        return result

    def business_provision_webhook(self, config):
        if config.get('webhook_registered'):
            return
        self.business_transport_request('POST', '/api/v1/webhooks', {
            'name': 'business-' + config['webhook_token'][:8],
            'url': self.business_callback_base() + '/api/v1/channels/langbot/webhook/' + config['webhook_token'],
            'enabled': True,
        }, user_token=True)
        config['webhook_registered'] = True

    def business_channels_page(self, query):
        from app import esc
        users = {u['id']: u for u in self.db.list_users(self.tenant_id)}
        cards = []
        for a in self.db.list_business_connections(self.tenant_id, self.user_id):
            config = json.loads(a['config_json'])
            platform = a['platform'] or ('WhatsApp' if a['channel'] != 'langbot' else 'Telegram')
            owner = users.get(a['owner_user_id'])
            cards.append(f'''<section class="card"><h2>{esc(platform)} · {esc(a['label'] or config.get('display_name') or '企业业务账号')}</h2>
                <p>负责人：{esc(owner['display_name'] if owner else '待管理员关联')} · 企业所有</p>
                <p>AI 自动回复：{'已开启' if a['ai_enabled'] else '未开启' if not a['legacy'] else '原连接设置（待核对）'}</p>
                <a class="button secondary" href="/admin/channels/accounts/{a['id']}">管理连接</a></section>''')
        owner_options = ''.join(f'<option value="{u["id"]}">{esc(u["display_name"])}</option>' for u in users.values() if u['active'])
        owner_field = f'<label>关联负责人<select name="owner_user_id">{owner_options}</select></label>' if self.user_role == 'admin' else ''
        body = f'''<div class="toolbar"><div><h1>渠道账号</h1><p class="muted">连接企业业务账号，按账号独立接待；新连接默认关闭 AI 自动回复。</p></div></div>
            <div class="grid">{''.join(cards) or '<p>还没有你可管理的连接。</p>'}</div>
            <section class="card" style="margin-top:16px"><h2>添加业务账号</h2><form method="post" action="/admin/channels/accounts">
            <label>平台<select name="platform"><option>WhatsApp</option><option>Telegram</option></select></label>
            <label>账号名称<input name="label" required maxlength="100" placeholder="例如 张三业务号"></label>{owner_field}
            <p><button>添加并连接</button></p></form></section>'''
        self.send_html(self.console_page('渠道账号', body, 'channels'))

    def business_account_page(self, a, query):
        from app import esc
        config = json.loads(a['config_json'])
        base = '/admin/channels/accounts/' + a['id']
        title = a['label'] or config.get('display_name') or '企业业务账号'
        notice = '<div class="notice">操作已保存。</div>' if query.get('notice') == ['saved'] else ''
        if a['channel'] == 'whatsapp_personal':
            connection = f'''<p id="connection-state">正在检查账号连接…</p><img id="connection-qr" alt="手机扫码连接业务账号" style="display:none;max-width:260px">
                <form method="post" action="{base}/start"><button>连接或重新连接账号</button></form>
                <p class="muted">打开 WhatsApp → 已连接的设备 → 连接设备，扫描二维码。</p>
                <script>(function(){{var timer,qrUrl;async function poll(){{try{{var r=await fetch('{base}/state');var d=await r.json();var state=document.getElementById('connection-state');
                state.textContent=d.ok?(d.status==='WORKING'?'已连接 · '+(d.name||'业务账号'):d.status==='SCAN_QR_CODE'?'等待手机扫码':'账号尚未连接'):'连接暂不可用，请稍后重试';
                var img=document.getElementById('connection-qr');img.style.display=d.status==='SCAN_QR_CODE'?'':'none';
                if(d.status==='SCAN_QR_CODE'){{var q=await fetch('{base}/qr');if(q.ok){{var next=URL.createObjectURL(await q.blob());img.src=next;if(qrUrl)URL.revokeObjectURL(qrUrl);qrUrl=next;}}}}
                }}catch(e){{document.getElementById('connection-state').textContent='连接暂不可用，请稍后重试';}}timer=setTimeout(poll,5000);}}poll();window.addEventListener('pagehide',function(){{clearTimeout(timer);if(qrUrl)URL.revokeObjectURL(qrUrl);}});}})();</script>'''
        elif a['channel'] == 'langbot':
            connection = f'''<p>{'已完成授权 · ' + esc(config.get('account_name', '企业机器人')) if config.get('bot_uuid') else '尚未连接'}</p>
                <p class="muted">Telegram 使用企业机器人接待。先在 Telegram 的 BotFather 创建机器人，再填入其授权令牌。</p>
                <form method="post" action="{base}/connect"><label>机器人授权令牌<input type="password" name="credential" required autocomplete="off"></label>
                <p><button>授权连接</button></p></form>'''
        else:
            connection = '<p>官方业务账号的开通条件仍需满足，当前连接方式暂未提供自助授权。</p>'
        grants = ''
        if self.user_role == 'admin':
            users = [u for u in self.db.list_users(self.tenant_id) if u['active']]
            options = ''.join(f'<option value="{u["id"]}">{esc(u["display_name"])}</option>' for u in users)
            grants = f'''<section class="card" style="margin-top:16px"><h2>成员授权</h2>
                <form method="post" action="{base}/owner"><label>关联负责人<select name="owner_user_id">{options}</select></label><p><button>保存负责人</button></p></form>
                <form method="post" action="{base}/grant"><label>协作成员<select name="user_id">{options}</select></label>
                <select name="grant"><option value="1">授予账号使用权限</option><option value="0">撤销账号使用权限</option></select><p><button>保存授权</button></p></form>
                <p class="muted">账号授权不会自动开放功能模块，请在成员管理中分配所需模块权限。</p></section>'''
        ai_action = f'<form method="post" action="{base}/ai"><input type="hidden" name="enabled" value="0"><button class="secondary">暂停 AI 自动回复</button></form>' if a['ai_enabled'] else f'<button type="button" onclick="document.getElementById(\'ai-confirm\').showModal()">开启 AI 自动问答</button>'
        body = f'''<p><a href="/admin/channels">← 返回渠道账号</a></p>{notice}<section class="card"><h1>{esc(title)}</h1><p>企业业务账号 · 企业所有</p>{connection}</section>
            <section class="card" style="margin-top:16px"><h2>AI 自动接待</h2><p>暂停 AI 后消息仍进入收件箱，可人工回复。</p>{ai_action}
            <dialog id="ai-confirm"><h2>开启前请确认企业资料</h2><p>AI 仅使用已核准、获准对外问答的企业知识。请核对资料内容、版本和授权范围。</p>
            <p><a href="/admin/knowledge" target="_blank" rel="noopener">查看企业知识</a></p><form method="post" action="{base}/ai"><input type="hidden" name="enabled" value="1">
            <label><input type="checkbox" name="confirmed" value="1" required>我已确认用于对外问答的企业资料</label><p><button>确认并开启</button>
            <button type="button" class="secondary" onclick="document.getElementById('ai-confirm').close()">取消</button></p></form></dialog></section>
            {grants}<section class="card" style="margin-top:16px"><h2>断开连接</h2><p>仅影响本账号，历史按访问权限保留。</p>
            <form method="post" action="{base}/disconnect" onsubmit="return confirm('确认断开此业务账号？AI 将同时关闭。')"><button class="warn">断开账号连接</button></form></section>'''
        self.send_html(self.console_page(title, body, 'channels'))

    def business_transfer_page(self, query):
        from app import esc
        users = [u for u in self.db.list_users(self.tenant_id) if u['active']]
        options = ''.join(f'<option value="{u["id"]}">{esc(u["display_name"])}</option>' for u in users)
        customers = self.db.conn.execute('SELECT * FROM conversations WHERE tenant_id=? AND owner_user_id<>\'\' ORDER BY updated_at DESC', (self.tenant_id,)).fetchall()
        names = {u['id']: u['display_name'] for u in users}
        customer_options = ''
        for c in customers:
            account = self.db.get_business_connection(self.tenant_id, c['channel_account_id']) if c['channel_account_id'] else None
            identity = self.db.get_channel_identity_by_conversation(self.tenant_id, c['source'], c['id'])
            platform = account['platform'] if account and account['platform'] else '网站聊天' if c['source'] in ('web', 'website_api') else '渠道客户'
            name = identity['display_name'] if identity else '客户会话 ' + c['id'][:8]
            if account and account['label']:
                name += '（' + account['label'] + '）'
            customer_options += f'<option value="{c["id"]}">{esc(platform)} · {esc(name)} · {esc(names.get(c["owner_user_id"], "原负责人"))}</option>'
        independent = self.db.conn.execute("SELECT * FROM leads WHERE tenant_id=? AND conversation_id IS NULL AND merged_into IS NULL AND owner IS NOT NULL ORDER BY updated_at DESC", (self.tenant_id,)).fetchall()
        customer_options += ''.join(f'<option value="lead:{l["id"]}">独立客户 · {esc(l["company"] or l["contact_email"] or l["id"][:8])} · {esc(l["owner"])}</option>' for l in independent)
        notice = '<div class="notice">客户、关联询价及未完成跟进已转交。业务历史权限已授予接替人。</div>' if query.get('notice') == ['transferred'] else ''
        body = f'''<h1>转交客户</h1>{notice}<section class="card"><p>转交保留原负责人和历史操作记录，不自动转移原外部账号的连接与发送权限。</p>
            <form method="post" action="/admin/customer-transfers"><label>原负责人<select name="previous_user_id">{options}</select></label>
            <label>接替人<select name="successor_user_id">{options}</select></label><label>客户<select name="conversation_id" required>{customer_options}</select></label>
            <p><button>转交客户及关联事项</button></p></form></section>'''
        self.send_html(self.console_page('转交客户', body, 'sales'))

    def business_knowledge_page(self, query):
        from app import esc, knowledge_sync_status
        sections = []
        for product in self.db.list_products(self.tenant_id):
            fields = self.db.get_fields(self.tenant_id, product['id'])
            rows = ''.join(f'<tr><td>{esc(f["label"])}</td><td>{esc(f["value"])} {esc(f["unit"])}</td><td>{"对外" if f["audience"] == "external" else "对内"}</td><td>{esc(f["state"])}</td></tr>' for f in fields)
            sources = ''.join(f'<li>{esc(s["filename"])} · {esc(s["usage"])}' + (f' · <a href="/admin/knowledge/sources/{s["id"]}">阅读原始资料</a>' if s['storage_path'] else ' · 未附文件') + '</li>' for s in self.db.get_sources(self.tenant_id, product['id']))
            sections.append(f'<section class="card" style="margin-top:16px"><h2>{esc(product["model"])} · {esc(product["name"])}</h2><table><tr><th>知识字段</th><th>内容</th><th>使用范围</th><th>核准状态</th></tr>{rows}</table><h3>来源资料</h3><ul>{sources}</ul></section>')
        body = '<h1>企业知识</h1><p>同企业成员可阅读对内和对外知识；AI 对外问答只使用已核准且允许对外的内容。修改和核准仍需相应权限。</p>'
        body += '<p>AI 资料状态：' + esc(knowledge_sync_status(self.db, self.tenant_id)) + '</p>' + ''.join(sections)
        self.send_html(self.console_page('企业知识', body, 'knowledge'))
