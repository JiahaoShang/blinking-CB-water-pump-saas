import tempfile,json,os
from pathlib import Path
from app import Database,answer_knowledge_question,answer_question_prefer_dify
from test_knowledge_phase2_ai_gates import StubKnowledgeAI,choose_source
with tempfile.TemporaryDirectory() as tmp:
 os.environ['B2B_KB_STORAGE_ROOT']=str(Path(tmp)/'uploads')
 d=Database(Path(tmp)/'db.sqlite');d.seed_demo_users();t='demo-tenant';u=d.authenticate_user('demo-admin@example.invalid','demo-admin')['id']
 topic=d.kb2_create_topic(t,u,'Customer service')
 d.kb2_save_topic_draft(t,u,topic,paragraphs=[{'id':'p1','text':'Service inquiries are answered within two business days.','origin':'manual','checked':True,'source':{'kind':'manual'}}],usage='external',ai_authorized=True)
 d.kb2_publish_topic_draft(t,u,topic,confirm_ai=True)
 a=StubKnowledgeAI(on_call=choose_source('topic'));direct=answer_knowledge_question(d,a,t,'','When is the response?')
 b=StubKnowledgeAI(on_call=choose_source('topic'));unified=answer_question_prefer_dify(d,b,t,'','When is the response?',None,'QA')
 print(json.dumps({'topic_evidence_count':len(d.kb2_list_topic_qa_evidence(t)),'legacy_knowledge_count':len(d.list_ai_answerable_knowledge(t)),'direct_status':direct['status'],'unified_status':unified[0],'unified_model_calls':len(b.calls)},ensure_ascii=False));d.conn.close()
