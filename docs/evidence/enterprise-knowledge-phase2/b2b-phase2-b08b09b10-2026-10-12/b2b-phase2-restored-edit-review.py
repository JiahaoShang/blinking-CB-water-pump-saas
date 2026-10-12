import tempfile,json,os
from pathlib import Path
from app import Database
with tempfile.TemporaryDirectory() as tmp:
 os.environ['B2B_KB_STORAGE_ROOT']=str(Path(tmp)/'uploads')
 d=Database(Path(tmp)/'db.sqlite');d.seed_demo_users();t='demo-tenant';u=d.authenticate_user('demo-admin@example.invalid','demo-admin')['id']
 item=d.create_knowledge_item(t,'article','Service',u,content={'body':'Service details'})
 tv=d.kb2_save_template(t,u,fields=[{'key':'price','label':'Price','type':'number'}],name='QA')
 d.kb2_adopt_template(t,u,item,tv,confirmed=True)
 d.kb2_set_item_field(t,u,item,'price',value=1,source={'kind':'manual'},checked=True)
 d.publish_knowledge_draft(t,item,u)
 d.kb2_set_item_field(t,u,item,'price',value=2);d.kb2_set_item_field(t,u,item,'price',checked=True);d.publish_knowledge_draft(t,item,u)
 d.restore_knowledge_version(t,item,1,u)
 d.kb2_set_item_field(t,u,item,'price',value=3);d.kb2_set_item_field(t,u,item,'price',checked=True)
 before=json.loads(d.get_knowledge_draft_version(t,item)['content_json'])['kb2_fields'][0]['value']
 d.publish_knowledge_draft(t,item,u)
 after=json.loads(d.get_published_knowledge_version(t,item)['content_json'])['kb2_fields'][0]['value']
 print(json.dumps({'restored_draft_field_value':before,'republished_field_value':after},ensure_ascii=False));d.conn.close()
