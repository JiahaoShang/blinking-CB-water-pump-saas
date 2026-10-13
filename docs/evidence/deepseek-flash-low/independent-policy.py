import sys,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[:1]
from ai_client import AIConfig,DeepSeekChatClient
from model_settings import EnterpriseModels
class Response:
 status=200
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"ok": true}','reasoning_content':'private-reasoning-marker'}}],'usage':{'prompt_tokens':4,'completion_tokens':8}}).encode()
class PolicyTests(unittest.TestCase):
 def request(self,config):
  sent=[]
  def capture(req,**kw):sent.append(json.loads(req.data));return Response()
  with patch('urllib.request.urlopen',side_effect=capture),patch('model_settings.custom_urlopen',side_effect=capture):
   result,meta=DeepSeekChatClient(config).call_json('Return JSON','test')
  self.assertEqual(result,{'ok':True});self.assertNotIn('private-reasoning-marker',json.dumps((result,meta)))
  return sent[0]
 def assert_low(self,b):
  self.assertEqual(b['model'],'deepseek-flash');self.assertEqual(b.get('thinking'),{'type':'enabled'});self.assertEqual(b.get('reasoning_effort'),'low')
 def test_platform_constructed_and_mutated_config_cannot_change_model(self):
  c=AIConfig(mode='deepseek',model='deepseek-v4-pro',api_key='fake');c.model='deepseek-v4-pro';self.assert_low(self.request(c))
 def test_environment_model_cannot_override(self):
  c=AIConfig.from_env({'AI_MODE':'deepseek','DEEPSEEK_API_KEY':'fake','DEEPSEEK_MODEL':'deepseek-v4-pro'})
  self.assertEqual(c.model,'deepseek-flash');self.assert_low(self.request(c))
 def test_official_custom_endpoint_also_forced_low(self):
  c=AIConfig(mode='deepseek',provider='custom',base_url='https://api.deepseek.com/v1',model='deepseek-v4-pro',api_key='fake')
  c.model='deepseek-v4-pro';self.assert_low(self.request(c))
 def test_other_custom_endpoint_unchanged(self):
  b=self.request(AIConfig(mode='deepseek',provider='custom',base_url='https://example.com/v1',model='company-model',api_key='fake'))
  self.assertEqual(b['model'],'company-model');self.assertNotIn('thinking',b);self.assertNotIn('reasoning_effort',b)
 def test_saved_and_old_official_custom_public_config_is_flash(self):
  with tempfile.TemporaryDirectory() as d:
   m=EnterpriseModels(Path(d)/'db.sqlite',None);m.save('a','https://api.deepseek.com/v1','deepseek-v4-pro','fake')
   self.assertEqual(m.public('a')['model'],'deepseek-flash')
   old=json.loads(m.path('a').read_text());old['model']='deepseek-v4-pro';m.path('a').write_text(json.dumps(old))
   self.assertEqual(m.public('a')['model'],'deepseek-flash');self.assert_low(self.request(m.service('a').config))
unittest.main()
