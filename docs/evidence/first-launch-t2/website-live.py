import os,sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from test_console_presentation import ConsolePresentationTests
x=ConsolePresentationTests();x.setUp();results=[]
try:
 t='demo-tenant';a=x.db.authenticate_user('demo-admin@example.invalid','demo-admin')['id']
 tid=x.db.kb2_create_topic(t,a,'T2 HTTP synthetic warranty')
 x.db.kb2_save_topic_draft(t,a,tid,paragraphs=[{'id':'p1','text':'The test pump warranty lasts 18 months.','origin':'manual','checked':True,'source':{'kind':'manual'}}],usage='external',ai_authorized=True)
 x.db.kb2_publish_topic_draft(t,a,tid,confirm_ai=True)
 x.db.register_buyer_site(t,'t2-site','https://t2.example.invalid')
 for stage in ('platform','custom'):
  if stage=='custom':x.server.RequestHandlerClass.models.save(t,os.environ['DEEPSEEK_BASE_URL'],os.environ['DEEPSEEK_MODEL'],os.environ['DEEPSEEK_API_KEY'])
  status,_,body=x.request('POST','/api/v1/buyer/conversations',json_body={'site_key':'t2-site'})
  c=json.loads(body);headers={'X-Site-Key':'t2-site','Authorization':'Bearer '+c['access_token'],'Idempotency-Key':'t2-question'}
  status,_,body=x.request('POST','/api/v1/buyer/conversations/'+c['conversation_id']+'/questions',json_body={'question':'How long is the test pump warranty?'},extra_headers=headers)
  r=json.loads(body);assert status==201 and r['status']=='grounded' and r['answer']=='The test pump warranty lasts 18 months.',(status,r)
  assert r['citations'][0]['topic_id']==tid,r
  results.append({'stage':stage,'http_status':status,'answer':r['answer'],'citations':r['citations']})
 logs=[dict(row) for row in x.db.conn.execute('SELECT task,provider,model,status,duration_ms,prompt_tokens,completion_tokens FROM ai_call_log')]
 print(json.dumps({'results':results,'calls':logs,'passed':True},ensure_ascii=False,indent=2))
finally:x.tearDown()
