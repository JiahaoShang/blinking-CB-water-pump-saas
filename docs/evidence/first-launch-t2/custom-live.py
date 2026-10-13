import os,sys,json,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
for line in Path('/tmp/b2b-t2-live-private.env').read_text().splitlines():
 k,v=line.split('=',1);os.environ[k]=v
from ai_client import AIService,AIConfig,AIUnavailableError
from model_settings import EnterpriseModels
with tempfile.TemporaryDirectory(prefix='b2b-t2-custom-') as d:
 platform=AIService(AIConfig.from_env());models=EnterpriseModels(Path(d)/'db.sqlite',platform)
 models.save('t2-a',platform.config.base_url,platform.config.model,platform.config.api_key)
 try:
  candidates,meta=models.service('t2-a').extract_requirements('We need a clean water pump with flow 30 m3/h and head 25 m.')
  result={'status':'ok','candidates':candidates,'meta':meta}
 except AIUnavailableError as e:result={'status':e.kind,'duration_ms':e.duration_ms}
 result['other_company_uses_platform']=models.service('t2-b') is platform
 result['public_has_key']=platform.config.api_key in json.dumps(models.public('t2-a'))
 models.disconnect('t2-a');result['disconnect_restores_platform']=models.service('t2-a') is platform
 print(json.dumps(result,ensure_ascii=False,indent=2))
