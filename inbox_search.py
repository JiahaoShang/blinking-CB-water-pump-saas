"""Literal keyword search, constrained by the inbox's existing tenant/channel/ACL filter."""
import re
from html import escape


def highlight(text, query):
    if not query:
        return escape(str(text or ''), quote=True)
    return ''.join('<mark>'+escape(part, quote=True)+'</mark>' if i % 2 else escape(part, quote=True)
                   for i, part in enumerate(re.split('('+re.escape(query)+')', str(text or ''), flags=re.IGNORECASE)))


def snippet(text, query):
    text = str(text or '')
    match = re.search(re.escape(query), text, re.IGNORECASE)
    start = max(0, match.start()-35) if match else 0
    end = min(len(text), (match.end()+70) if match else 100)
    return ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')


def search(db, tenant, source_where, args, query):
    needle = query.lower()
    # instr treats %, _ and quotes literally; all user input remains bound parameters.
    names = ['instr(lower(c.id),?)>0']
    for table, fields in [('channel_identities', ['display_name','external_user_id']),
                          ('rfqs', ['contact_name','contact_email','contact_phone','company']),
                          ('leads', ['contact_name','contact_email','contact_phone','company'])]:
        terms = ' OR '.join(f'instr(lower(COALESCE(x.{f},\'\')),?)>0' for f in fields)
        names.append(f'EXISTS (SELECT 1 FROM {table} x WHERE x.tenant_id=c.tenant_id AND x.conversation_id=c.id AND ({terms}))')
    names.append("EXISTS (SELECT 1 FROM channel_accounts x WHERE x.tenant_id=c.tenant_id AND x.id=c.channel_account_id AND instr(lower(x.label),?)>0)")
    identity = ' OR '.join(names)
    rows = db.conn.execute(f"""SELECT c.id,
        (SELECT m.id FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND instr(lower(m.content),?)>0 ORDER BY m.rowid DESC LIMIT 1) AS hit_id,
        (SELECT m.content FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND instr(lower(m.content),?)>0 ORDER BY m.rowid DESC LIMIT 1) AS hit_content,
        (SELECT MAX(m.created_at) FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND instr(lower(m.content),?)>0) AS hit_time,
        (SELECT COUNT(*) FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND instr(lower(m.content),?)>0) AS hit_count
        FROM conversations c WHERE c.tenant_id=? AND ({source_where}) AND (({identity}) OR
        EXISTS (SELECT 1 FROM messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id AND instr(lower(m.content),?)>0))
        ORDER BY COALESCE(hit_time,c.updated_at) DESC,c.id DESC LIMIT 50""",
        (needle,needle,needle,needle,tenant,*args,*([needle]*(identity.count("?")+1)))).fetchall()
    return {r['id']:dict(r) for r in rows}
