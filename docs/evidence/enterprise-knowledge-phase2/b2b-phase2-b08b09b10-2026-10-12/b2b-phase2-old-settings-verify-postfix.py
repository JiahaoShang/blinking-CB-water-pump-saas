# -*- coding: utf-8 -*-
"""B2-B08 设置补充 · 修复后验证（对照 /tmp/b2b-phase2-old-settings-review.py）。

旧批次保存设置现在在任何写入之前被拒绝；当前草稿的用途 / AI / 到期、正文与出处、
线上版本全部不变；当前草稿所在批次（替换批次）的保存设置保持可用。
"""
import tempfile, json, os
from pathlib import Path
from app import Database

with tempfile.TemporaryDirectory() as tmp:
    os.environ['B2B_KB_STORAGE_ROOT'] = str(Path(tmp) / 'uploads')
    d = Database(Path(tmp) / 'db.sqlite')
    d.seed_demo_users()
    t = 'demo-tenant'
    u = d.authenticate_user('demo-admin@example.invalid', 'demo-admin')['id']
    b1 = d.kb2_create_batch(t, u, [{'kind': 'file', 'filename': 'old.txt', 'data': b'Power supply 220V'}])
    item = d.kb2_get_batch(t, b1)['sources'][0]['item_id']
    for p in d.kb2_get_batch(t, b1)['sources'][0]['paragraphs']:
        d.kb2_update_paragraph(t, u, p['id'], checked=True)
    d.kb2_publish_batch(t, u, b1)
    replacement = d.kb2_replace_file(t, u, item, 'new.txt', b'Power supply 240V')
    b2 = replacement['batch_id']
    for p in d.kb2_get_batch(t, b2)['sources'][0]['paragraphs']:
        d.kb2_update_paragraph(t, u, p['id'], checked=True)
    d.kb2_publish_batch(t, u, b2)
    tv = d.kb2_save_template(t, u, fields=[{'key': 'note', 'label': 'Note', 'type': 'text'}], name='QA')
    d.kb2_adopt_template(t, u, item, tv, confirmed=True)
    # 当前批次（替换批次）保存设置：允许（草稿属于当前原件）。
    d.kb2_save_batch_settings(t, u, b2, usage='external', ai_authorized=True, expires_at='2030-01-01')
    before = dict(d.get_knowledge_draft_version(t, item))
    rejected = None
    try:
        d.kb2_save_batch_settings(t, u, b1, usage='internal', ai_authorized=False)
        outcome = 'old settings saved'
    except ValueError as error:
        rejected = str(error)
        outcome = 'rejected before writes'
    after = dict(d.get_knowledge_draft_version(t, item))
    print(json.dumps({
        'outcome': outcome,
        'rejected': rejected,
        'draft_unchanged': all(before[k] == after[k] for k in
                               ('usage', 'ai_authorized', 'expires_at', 'content_json', 'source_id')),
        'draft_usage_after': after['usage'],
        'draft_ai_after': after['ai_authorized'],
        'draft_expires_after': after['expires_at'],
        'published_version': d.get_published_knowledge_version(t, item)['version'],
    }, ensure_ascii=False))
    d.conn.close()
