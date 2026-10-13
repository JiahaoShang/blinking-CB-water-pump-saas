import os,sys,json,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from ai_client import AIService,AIConfig
from app import answer_knowledge_question
from test_enterprise_knowledge_task4 import Task4Base,TENANT
x=Task4Base();x.setUp()
try:
 item,version=x.publish_prepared(title='T2 warranty synthetic',body='The test pump warranty lasts 18 months.')
 ai=AIService(AIConfig.from_env());original=ai.client.call_json;events={};threads=[]
 def revoke():
  time.sleep(.15);x.db.set_knowledge_authorization(TENANT,item,x.admin['id'],False)
  events['revoked_at']=time.monotonic()
 def call(system,user):
  events['started_at']=time.monotonic();thread=threading.Thread(target=revoke);threads.append(thread);thread.start()
  result=original(system,user);events['returned_at']=time.monotonic();thread.join();return result
 ai.client.call_json=call
 result=answer_knowledge_question(x.db,ai,TENANT,'','How long is the test pump warranty?')
 assert events['started_at']<events['revoked_at']<events['returned_at'],events
 assert result['status']=='no_evidence' and not result['citations'],result
 logs=[dict(r) for r in x.db.conn.execute('SELECT status,duration_ms,prompt_tokens,completion_tokens FROM ai_call_log')]
 print(json.dumps({'passed':True,'case':'real_inflight_revocation','status':result['status'],'reason':result['reason'],'citations':result['citations'],'events':events,'calls':logs},ensure_ascii=False,indent=2))
finally:
 for t in threads:t.join()
 x.tearDown()
