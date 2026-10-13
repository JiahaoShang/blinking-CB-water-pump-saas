import os,sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from ai_client import AIService,AIConfig
from app import answer_question_prefer_dify
from test_knowledge_phase2_ai_gates import FieldEvidenceGateTests,TENANT
from test_enterprise_knowledge import EnterpriseKnowledgeTask1Tests
results=[];logs=[]
for kind in ('knowledge','item_field','product_field'):
 x=FieldEvidenceGateTests();x.setUp()
 try:
  ai=AIService(AIConfig.from_env());product=None
  if kind=='knowledge':
   item=x.db.create_knowledge_item(TENANT,'article','T2 synthetic service',x.admin['id'],content={'body':'Test service is available Monday to Friday.'},usage='external',ai_authorized=True)
   x.db.publish_knowledge_draft(TENANT,item,x.admin['id'],confirm_ai=True)
   version=x.db.get_published_knowledge_version(TENANT,item)
   x.db.prepare_knowledge_version(TENANT,version['id'])
   question='On which weekdays is the test service available?';expected='Monday to Friday'
  elif kind=='item_field':
   item,batch=x.make_item_with_fields([{'label':'Test warranty','type':'number','unit':'months','key':'warranty'}],[('warranty','27',{'checked':True,'allow_customer':True})])
   question='How many months is the Test warranty?';expected='27'
  else:
   product=x.db.create_product(TENANT,'T2-PUMP','T2 synthetic pump','clean water')
   source=x.db.add_source(TENANT,product,'T2-test.txt','TXT','Test specification')
   for key,value,unit in [('flow_range','20-60','m³/h'),('head_range','25-45','m'),('media','清水','')]:
    x.db.save_field(TENANT,product,key,value,unit,source,'Test specification');x.db.approve_field(TENANT,product,key)
   x.db.finalize_product_review(TENANT,product);x.db.publish_product(TENANT,product)
   x.db.set_knowledge_product_authorization(TENANT,product,x.admin['id'],authorized=True,confirmed=True)
   question='What is the flow range?';expected='20-60'
  r=answer_question_prefer_dify(x.db,ai,TENANT,'',question,product,'T2')
  assert r[0]=='grounded' and expected in r[1],r
  assert r[4] and (r[4][0].get('kind')==kind if kind!='product_field' else r[4][0].get('product_version')=='1'),r
  results.append({'case':kind,'status':r[0],'answer':r[1],'citations':r[4]})
  logs.extend(dict(row) for row in x.db.conn.execute('SELECT provider,model,status,duration_ms,prompt_tokens,completion_tokens FROM ai_call_log'))
 finally:x.tearDown()
print(json.dumps({'results':results,'calls':logs,'passed':True},ensure_ascii=False,indent=2))
