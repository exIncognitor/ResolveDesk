"""Explicit reviewed synthetic corpus for tests outside import-governance tests."""
def publish_fixture(kb,p,document_id,title,content,visibility='tenant'):
    metadata={'material_type':'simulated_policy','source_urls':[],'applicability':['Synthetic test environment'],
        'domains':['network_endpoint'],'source_checked_on':'2026-10-28','effective_from':1,'expires_at':4102444700}
    result=kb.add(p,document_id,title,content,visibility,metadata=metadata)
    document=next(d for d in kb.publications(p) if d['document_id']==document_id)
    kb.publication_action(p,document_id,'publish',document['version'],document['revision'],'Explicit synthetic fixture review')
    return result
