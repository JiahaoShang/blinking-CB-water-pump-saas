import os,sys,json,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
import ai_client
from app import answer_knowledge_question,answer_question_prefer_dify
from test_knowledge_phase2_ai_gates import AiGateBase,TENANT
x=AiGateBase();x.setUp();results=[]
try:
 ai=ai_client.AIService(ai_client.AIConfig.from_env())
 for tenant,text in [(TENANT,'The test pump warranty lasts 18 months.'),('t2-company-b','The test pump warranty lasts 31 months.')]:
  x.db.ensure_tenant(tenant)
  actor=x.admin['id'] if tenant==TENANT else x.db.create_user(tenant,'t2-b@example.invalid','T2 B','admin','t2-password','T2 fixture')
  tid=x.db.kb2_create_topic(tenant,actor,'T2 synthetic warranty')
  x.db.kb2_save_topic_draft(tenant,actor,tid,paragraphs=[{'id':'p1','text':text,'origin':'manual','checked':True,'source':{'kind':'manual'}}],usage='external',ai_authorized=True)
  x.db.kb2_publish_topic_draft(tenant,actor,tid,confirm_ai=True)
  r=answer_question_prefer_dify(x.db,ai,tenant,'','How long is the test pump warranty?',None,'T2')
  assert r[0]=='grounded' and r[1]==text and r[4][0]['topic_id']==tid,r
  results.append({'tenant':tenant,'case':'topic_unified_entry','status':r[0],'answer':r[1],'citations':r[4]})
 r=answer_knowledge_question(x.db,ai,TENANT,'','What is the maximum operating pressure in bar?')
 assert r['status']=='no_evidence' and not r['citations'],r
 results.append({'case':'missing_fact','status':r['status'],'citations':r['citations']})
 print(json.dumps({'results':results,'passed':True},ensure_ascii=False,indent=2))
finally:x.tearDown()
