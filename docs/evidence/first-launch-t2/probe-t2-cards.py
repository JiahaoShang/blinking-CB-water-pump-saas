# -*- coding: utf-8 -*-
"""从修复后的隔离副本中渲染 /admin/knowledge/qa 的引用卡，输出真实片段（只读，不写业务库）。"""
from test_first_launch_t2 import T2QACitationLinkTests, StubKnowledgeAI, choose_source, QUESTION


class Probe(T2QACitationLinkTests):
    def runTest(self):
        pass


t = Probe()
t.setUp()
try:
    topic_id = t.publish_topic()
    t.use_stub(StubKnowledgeAI(on_call=choose_source("topic")))
    page = t.ask(QUESTION)
    print("TOPIC_CARD=" + t.citation_card(page))
    item_id = t.publish_item_with_field()
    t.use_stub(StubKnowledgeAI(on_call=choose_source("item_field")))
    page = t.ask("支持定制吗？")
    print("ITEM_FIELD_CARD=" + t.citation_card(page))
finally:
    t.tearDown()
