"""知识索引就绪与对外授权验证；上传成功不等于问答可用。"""
import hashlib
import threading


def authorized_documents(db, tenant_id):
    from app import collect_kb_documents
    return {d['product_id']: hashlib.sha256(d['text'].encode()).hexdigest() for d in collect_kb_documents(db, tenant_id)}


def usable_document_ids(db, tenant_id):
    current = authorized_documents(db, tenant_id)
    return {r['document_id'] for r in db.conn.execute('SELECT * FROM kb_sync_state WHERE tenant_id=?', (tenant_id,))
            if r['document_id'] and r['content_hash'] == current.get(r['product_id'])
            and r['indexing_status'] == 'completed' and not r['last_error']}


def knowledge_sync_status(db, tenant_id):
    from app import dify_kb_config
    current = authorized_documents(db, tenant_id)
    if not current:
        return '暂无可用于对外问答的已发布知识'
    if not dify_kb_config()[1] or not dify_kb_config()[2]:
        return '本地核准知识可用；远程问答资料服务未启用'
    rows = db.conn.execute('SELECT * FROM kb_sync_state WHERE tenant_id=?', (tenant_id,)).fetchall()
    if any(r['last_error'] for r in rows):
        return '更新失败，系统将自动重试；未就绪的远程资料不用于回答'
    ready = len(usable_document_ids(db, tenant_id))
    if ready == len(current) and all(r['product_id'] in current for r in rows):
        return '已生效，AI 可使用当前获准对外知识'
    return f'准备中：{ready}/{len(current)} 份知识已生效'


def refresh_indexing_status(db, tenant_id):
    from app import dify_kb_config, _dify_kb_request, ChannelSendError
    base, dataset_id, key = dify_kb_config()
    if not dataset_id or not key:
        return
    rows = db.conn.execute("SELECT * FROM kb_sync_state WHERE tenant_id=? AND document_id<>'' AND batch<>'' AND indexing_status<>'completed'", (tenant_id,)).fetchall()
    for r in rows:
        try:
            payload = _dify_kb_request(base, key, 'GET', f"/v1/datasets/{dataset_id}/documents/{r['batch']}/indexing-status") or {}
            item = next((i for i in payload.get('data', []) if i.get('id') == r['document_id']), None)
            if item:
                status = str(item.get('indexing_status') or 'pending')
                error = '资料索引处理失败' if status == 'error' else ''
                db.conn.execute('UPDATE kb_sync_state SET indexing_status=?,last_error=? WHERE tenant_id=? AND product_id=?', (status, error, tenant_id, r['product_id']))
        except ChannelSendError:
            # 保留 pending，不能因状态接口暂不可达假报 ready。
            pass
    db.conn.commit()


def start_knowledge_worker(db_path, interval=30):
    """独立数据库连接，随服务关闭而停止；业务库保留重试状态，重启后继续。"""
    stop = threading.Event()

    def run():
        from app import Database, sync_kb_to_dify, dify_kb_config
        db = Database(db_path, initialize=False)
        try:
            while not stop.wait(interval):
                if not all(dify_kb_config()[1:]):
                    continue
                for tenant in db.conn.execute('SELECT id FROM tenants').fetchall():
                    if stop.is_set():
                        return
                    try:
                        sync_kb_to_dify(db, tenant['id'], actor='system:knowledge-sync')
                        refresh_indexing_status(db, tenant['id'])
                    except Exception:
                        # 一家企业失败不阻断其他企业；下一轮从持久台账重试。
                        continue
        finally:
            db.conn.close()

    thread = threading.Thread(target=run, daemon=True, name='enterprise-knowledge-sync')
    thread.start()
    return stop
