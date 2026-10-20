"""Pure/mocked rehearsal checks: no database connections or migrations executed."""
import copy
import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

path=Path(__file__).with_name('verify-credit-migration.py')
spec=importlib.util.spec_from_file_location('private_migration_verifier',path)
verifier=importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def snapshot():
    return {
        'intelligence_sources': {'columns':['id','type','fetch_interval_minutes','last_checked_at','last_successful_fetch_at','pending_execution_id'],
            'rows':[{'id':'source','type':'LINKEDIN','fetch_interval_minutes':60,'last_checked_at':'checked','last_successful_fetch_at':None,'pending_execution_id':'pending'}]},
        'intelligence_items':{'columns':['id','source_id'],'rows':[{'id':'post','source_id':'source'}]},
        'intelligence_analyses':{'columns':['news_item_id','result'],'rows':[{'news_item_id':'post','result':'retained'}]},
        'intelligence_user_states':{'columns':['id','news_item_id','saved'],'rows':[{'id':'state','news_item_id':'post','saved':True}]},
        'intelligence_brightdata_executions':{'columns':['id','reserved_records'],'rows':[{'id':'execution','reserved_records':6}]},
    }


def migrated(before):
    result=copy.deepcopy(before)
    row=result['intelligence_sources']['rows'][0]
    row['fetch_interval_minutes']=1440
    row['last_successful_fetch_at']='checked'
    return result


def test_import_does_not_load_application_or_connect():
    assert 'app.main' not in sys.modules and 'app.db' not in sys.modules


@pytest.mark.parametrize('url',[
    'postgresql+asyncpg://news_intel@127.0.0.1:5432/news_intel',
    'postgresql+asyncpg://credit_rehearsal@127.0.0.1:55441/postgres',
    'postgresql+asyncpg://credit_rehearsal@192.0.2.1:55440/postgres',
    'postgresql+asyncpg://credit_rehearsal@127.0.0.1:55440/postgres?host=production',
])
def test_refuses_non_private_target_before_import(url):
    with pytest.raises(ValueError):verifier.assert_target_url(url)


def test_requires_exact_live_private_identity_and_directory(tmp_path):
    directory=tmp_path/'private-pg18-data'
    identity={'role':'credit_rehearsal','database':'postgres','port':55440,'version_num':180000,
              'listen_addresses':'127.0.0.1','data_directory':str(directory)}
    verifier.assert_private_identity(identity,directory)
    for field,value in [('port',5432),('role','postgres'),('version_num',160000),('data_directory',str(tmp_path/'production')),('listen_addresses','*')]:
        altered={**identity,field:value}
        with pytest.raises(ValueError):verifier.assert_private_identity(altered,directory)


def test_preserves_existing_rows_including_usage_ledger():
    before=snapshot();after=migrated(before)
    verifier.assert_preserved(before,after)


@pytest.mark.parametrize('table,column,value',[
    ('intelligence_sources','pending_execution_id',None),
    ('intelligence_sources','last_successful_fetch_at','wrong'),
    ('intelligence_items','source_id','another-source'),
    ('intelligence_analyses','result','changed'),
    ('intelligence_user_states','saved',False),
    ('intelligence_brightdata_executions','reserved_records',0),
])
def test_detects_content_relationship_and_ledger_mutation(table,column,value):
    before=snapshot();after=migrated(before);after[table]['rows'][0][column]=value
    with pytest.raises(AssertionError):verifier.assert_preserved(before,after)


def test_allows_new_columns_but_not_row_loss():
    before=snapshot();after=migrated(before)
    after['intelligence_items']['columns'].append('linkedin_data')
    after['intelligence_items']['rows'][0]['linkedin_data']=None
    verifier.assert_preserved(before,after)
    after['intelligence_items']['rows']=[]
    with pytest.raises(AssertionError):verifier.assert_preserved(before,after)


def test_idempotency_includes_full_schema_and_all_tables():
    first={'tables':snapshot(),'schema':[{'table':'intelligence_items','column':'id'}]}
    verifier.assert_idempotent(first,copy.deepcopy(first))
    second=copy.deepcopy(first);second['schema'][0]['column']='modified'
    with pytest.raises(AssertionError):verifier.assert_idempotent(first,second)
    second=copy.deepcopy(first);second['tables']['intelligence_brightdata_executions']['rows'][0]['reserved_records']=0
    with pytest.raises(AssertionError):verifier.assert_idempotent(first,second)


def test_idempotency_ignores_select_row_order():
    first={'tables':snapshot(),'schema':[]}
    first['tables']['intelligence_items']['rows'].append({'id':'second','source_id':'source'})
    second=copy.deepcopy(first)
    second['tables']['intelligence_items']['rows'].reverse()
    verifier.assert_idempotent(first,second)


@pytest.mark.parametrize('category',['columns','constraints','indexes','ownership'])
def test_first_migration_preserves_original_schema_definitions(category):
    before={k:[{'definition':'original'}] for k in ['columns','constraints','indexes','ownership']}
    after=copy.deepcopy(before)
    verifier.assert_schema_preserved(before,after)
    after[category][0]['definition']='changed'
    with pytest.raises(AssertionError):verifier.assert_schema_preserved(before,after)


def test_optimized_python_is_rejected():
    tree=ast.parse(path.read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='require_enabled_checks')
    namespace={}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec',optimize=1),namespace)
    with pytest.raises(ValueError):namespace['require_enabled_checks']()


def test_private_network_guard_covers_connect_and_connect_ex(monkeypatch):
    calls=[]
    class FakeSocket:
        def connect(self,address):calls.append(('connect',address))
        def connect_ex(self,address):calls.append(('connect_ex',address));return 0
    monkeypatch.setattr(verifier.socket,'socket',FakeSocket)
    monkeypatch.setattr(verifier.socket,'getaddrinfo',lambda *a,**k: calls.append(('dns',a)))
    restore=verifier.install_private_network_guard()
    try:
        for method in ('connect','connect_ex'):
            with pytest.raises(RuntimeError):getattr(FakeSocket(),method)(('192.0.2.1',443))
        with pytest.raises(RuntimeError):verifier.socket.getaddrinfo('outside.invalid',443)
        assert calls==[]
        FakeSocket().connect(('127.0.0.1',55440))
        FakeSocket().connect_ex(('127.0.0.1',55440))
        assert len(calls)==2
    finally:restore()


def test_windows_policy_uses_selector_not_connectex(monkeypatch):
    policies=[]
    monkeypatch.setattr(verifier.sys,'platform','win32')
    marker=object()
    monkeypatch.setattr(verifier.asyncio,'WindowsSelectorEventLoopPolicy',lambda:marker,raising=False)
    monkeypatch.setattr(verifier.asyncio,'set_event_loop_policy',policies.append)
    verifier.configure_private_loop()
    assert policies==[marker]


def test_full_startup_ddl_sequence_repeats_with_mock_connection():
    calls=[]
    class Connection:
        async def run_sync(self,fn):calls.append('create_all')
        async def execute(self,query):calls.append(str(query))
    class Metadata:
        create_all=object()
    class Base:
        metadata=Metadata()
    async def migrate(connection):calls.append('credit_migration')
    async def run():
        await verifier.migrate_all(Connection(),Base,migrate)
        await verifier.migrate_all(Connection(),Base,migrate)
    asyncio.run(run())
    assert len(calls)==10 and calls[:5]==calls[5:]
    assert calls[0]=='create_all' and calls[4]=='credit_migration'
