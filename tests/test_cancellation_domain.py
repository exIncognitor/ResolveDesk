"""Literal task cancellation takes precedence over a wrong inferred domain."""
from resolvedesk.entity_state import EntityState
from resolvedesk.store import Store
from test_it_workflows import USER


def test_cancel_current_request_does_not_depend_on_router_domain(tmp_path):
    store=Store(tmp_path/'cancel-domain.sqlite3');entities=EntityState(store)
    run,_=store.create_run(USER,'申请IDEA Ultimate商业席位，期限未定','domain-start')
    store.claim(USER,run['id'])
    state=entities.merge(USER,run['id'],'申请IDEA Ultimate商业席位，期限未定','software_license')
    assert state['task']['consent']
    store.finish(USER,run['id'],'awaiting_input','请补充天数')
    store.reply(USER,run['id'],'请取消这份申请','domain-cancel')
    state=entities.merge(USER,run['id'],'请取消这份申请','account_access')
    assert state['cancelled'] and not state['task']['consent']
    assert state['entities']=={} and state['domain']=='general'
    store.close()
