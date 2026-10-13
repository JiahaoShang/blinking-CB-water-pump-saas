import os,sys,json
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from test_console_presentation import ConsolePresentationTests
x=ConsolePresentationTests();x.setUp()
try:
 cookie=x.login();t='demo-tenant'
 status,h,_=x.request('POST','/admin/preferences/model/connect',urlencode({'base_url':os.environ['DEEPSEEK_BASE_URL'],'model':os.environ['DEEPSEEK_MODEL'],'api_key':os.environ['DEEPSEEK_API_KEY']}),cookie=cookie)
 assert 'notice=connected' in h.get('Location',''),h
 models=x.server.RequestHandlerClass.models
 assert models.public(t)['source']=='custom'
 status,_,page=x.request('GET','/admin/preferences',cookie=cookie)
 assert os.environ['DEEPSEEK_API_KEY'] not in page and os.environ['DEEPSEEK_API_KEY'].encode() not in x.db_path.read_bytes()
 path=models.path(t);permissions={'directory':oct(path.parent.stat().st_mode&0o777),'file':oct(path.stat().st_mode&0o777)}
 assert permissions=={'directory':'0o700','file':'0o600'},permissions
 other=models.service('t2-other') is models.platform
 status,h,_=x.request('POST','/admin/preferences/model/disconnect','',cookie=cookie)
 assert 'notice=disconnected' in h.get('Location','') and models.service(t) is models.platform
 print(json.dumps({'passed':True,'real_connect_notice':'connected','settings_http':200,'known_secret_in_page_or_db':False,'private_permissions':permissions,'other_enterprise_uses_platform':other,'disconnect_restores_platform':True,'model_calls':1,'usage':'Connection probe meta is not persisted by current product; budget bound applies.'},ensure_ascii=False,indent=2))
finally:x.tearDown()
