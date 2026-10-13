import os,sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from ai_client import AIService,AIConfig
from app import process_channel_message
from test_enterprise_knowledge_task4 import Task4Base,TENANT
x=Task4Base();x.setUp();results=[]
try:
 x.publish_prepared(title='T2 warranty',body='The test pump warranty lasts 18 months.')
 ai=AIService(AIConfig.from_env())
 for channel,platform in [('langbot','Telegram'),('whatsapp','WhatsApp')]:
  a=x.db.create_business_connection(TENANT,channel,platform,'T2 synthetic account',x.admin['id'],x.admin['email'])
  r=process_channel_message(x.db,ai,TENANT,channel,'t2-customer','T2','How long is the warranty?','t2-disabled',a)
  assert r['action']=='ai_disabled',r
  try:x.db.set_connection_ai(TENANT,a,True,x.admin['id'],confirmed=False)
  except ValueError:pass
  else:raise AssertionError('unconfirmed AI enable accepted')
  assert not x.db.get_business_connection(TENANT,a)['ai_enabled']
  x.db.set_connection_ai(TENANT,a,True,x.admin['id'],confirmed=True)
  with patch('app.dispatch_outbound',return_value=(False,'T2 outgoing deliberately blocked')):
   r=process_channel_message(x.db,ai,TENANT,channel,'t2-customer','T2','How long is the warranty?','t2-enabled',a)
  answer=dict(x.db.conn.execute('SELECT * FROM answers WHERE conversation_id=?',(r['conversation_id'],)).fetchone())
  assert r['action']=='answered' and 'The test pump warranty lasts 18 months.' in answer['answer_text'],r
  results.append({'platform':platform,'AI_default_off':True,'confirmation_required':True,'action':r['action'],'answer':answer['answer_text'],'citations':json.loads(answer['citations_json']),'external_delivery':'not_attempted_blocked'})
 logs=[dict(row) for row in x.db.conn.execute('SELECT task,provider,model,status,duration_ms,prompt_tokens,completion_tokens FROM ai_call_log')]
 print(json.dumps({'results':results,'calls':logs,'passed':True},ensure_ascii=False,indent=2))
finally:x.tearDown()
