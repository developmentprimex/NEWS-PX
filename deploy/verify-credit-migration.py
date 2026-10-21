"""Explicitly approved, private PG18-only migration/preservation rehearsal.

No work occurs on import. CLI requires an approval reference and the unique new
private cluster's data directory; production endpoints are refused before app import.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

from sqlalchemy.engine import make_url


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def row_fingerprint(rows):
    return fingerprint(sorted(json.dumps(row,sort_keys=True,default=str) for row in rows))


def require_enabled_checks():
    if not __debug__:
        raise ValueError('Optimized Python would remove preservation checks; refusing rehearsal')


def configure_private_loop():
    # Windows Proactor uses native ConnectEx, bypassing socket.connect wrappers.
    if sys.platform=='win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def install_private_network_guard():
    original_connect=socket.socket.connect
    original_connect_ex=socket.socket.connect_ex
    original_dns=socket.getaddrinfo
    def allowed(address):
        if not isinstance(address,tuple) or address[0]!='127.0.0.1' or address[1]!=55440:
            raise RuntimeError('Network blocked outside private rehearsal endpoint')
    def connect(sock,address):
        allowed(address);return original_connect(sock,address)
    def connect_ex(sock,address):
        allowed(address);return original_connect_ex(sock,address)
    def dns(host,port,*args,**kwargs):
        allowed((host,port));return original_dns(host,port,*args,**kwargs)
    socket.socket.connect=connect
    socket.socket.connect_ex=connect_ex
    socket.getaddrinfo=dns
    def restore():
        socket.socket.connect=original_connect
        socket.socket.connect_ex=original_connect_ex
        socket.getaddrinfo=original_dns
    return restore


def assert_target_url(value):
    url=make_url(value)
    if (url.drivername,url.host,url.port,url.username,url.database)!=(
        'postgresql+asyncpg','127.0.0.1',55440,'credit_rehearsal','postgres') or url.query or url.password:
        raise ValueError('Refusing non-private rehearsal target')


def assert_private_identity(identity,expected_directory):
    actual=Path(identity['data_directory']).resolve()
    expected=Path(expected_directory).resolve()
    if (identity['role'],identity['database'],identity['port'],identity['listen_addresses'])!=(
        'credit_rehearsal','postgres',55440,'127.0.0.1'):
        raise ValueError('Unexpected private runtime identity')
    if not 180000<=int(identity['version_num'])<190000 or actual!=expected:
        raise ValueError('Unexpected private runtime version or directory')


def assert_preserved(before,after):
    for table,baseline in before.items():
        assert table in after, 'Pre-existing table lost'
        original=baseline['rows']
        expected=[dict(row) for row in original]
        if table=='intelligence_sources':
            for row in expected:
                if row['type']=='LINKEDIN':
                    row['fetch_interval_minutes']=max(1440,row['fetch_interval_minutes'])
                    if 'last_successful_fetch_at' in row and row['last_successful_fetch_at'] is None:
                        row['last_successful_fetch_at']=row.get('last_checked_at')
            by_id={str(row['id']):row for row in after[table]['rows']}
            for old in original:
                updated=by_id.get(str(old['id']))
                assert updated is not None, 'Source identity lost'
                target=old.get('last_successful_fetch_at')
                if old['type']=='LINKEDIN' and target is None:target=old.get('last_checked_at')
                assert updated.get('last_successful_fetch_at')==target, 'Unexpected watermark change'
        assert set(baseline['columns'])<=set(after[table]['columns']), 'Original columns lost'
        projected=[{key:row[key] for key in baseline['columns']} for row in after[table]['rows']]
        assert row_fingerprint(expected)==row_fingerprint(projected), 'Existing data/count/relationship changed'


def assert_idempotent(first,second):
    def stable(state):
        return {'schema':state['schema'],'tables':{name:{'columns':table['columns'],'rows_sha256':row_fingerprint(table['rows'])}
            for name,table in state['tables'].items()}}
    assert fingerprint(stable(first))==fingerprint(stable(second)), 'Second full migration changed data or schema'


def assert_schema_preserved(before,after):
    for category in ('columns','constraints','indexes','ownership'):
        for definition in before[category]:
            assert definition in after[category], 'Existing column/constraint/index/ownership definition changed'


async def capture(connection):
    from sqlalchemy import text
    tables={}
    names=(await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars()
    for name in names:
        quoted='"'+name.replace('"','""')+'"'
        result=await connection.execute(text('SELECT * FROM public.'+quoted))
        tables[name]={'columns':list(result.keys()),'rows':[dict(row) for row in result.mappings()]}
    columns=[dict(r) for r in (await connection.execute(text("""SELECT table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default
        FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position"""))).mappings()]
    constraints=[dict(r) for r in (await connection.execute(text("""SELECT c.conrelid::regclass::text AS table_name,c.conname,c.contype,c.convalidated,
        pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname='public' ORDER BY table_name,c.conname"""))).mappings()]
    indexes=[dict(r) for r in (await connection.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname"))).mappings()]
    ownership=[dict(r) for r in (await connection.execute(text("SELECT tablename,tableowner FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).mappings()]
    return {'tables':tables,'schema':{'columns':columns,'constraints':constraints,'indexes':indexes,'ownership':ownership}}


async def migrate_all(connection,base,migrate):
    from sqlalchemy import text
    from app.intelligence.collection_cycles import migrate_trial_schema
    # Exact schema-migration order in app.main.lifespan. Never run lifespan,
    # ensure_* installers, snapshot import, digest or collection schedulers.
    await connection.run_sync(base.metadata.create_all)
    await migrate_trial_schema(connection)
    await connection.execute(text('ALTER TABLE intelligence_items ADD COLUMN IF NOT EXISTS linkedin_data JSONB'))
    await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_groups JSONB NOT NULL DEFAULT '[]'::jsonb"))
    await connection.execute(text("ALTER TABLE intelligence_sources ADD COLUMN IF NOT EXISTS repost_guidance JSONB NOT NULL DEFAULT '{}'::jsonb"))
    await migrate(connection)


async def validate_relationships(connection,state):
    from sqlalchemy import text
    for name in ('intelligence_sources','intelligence_items','intelligence_analyses','intelligence_user_states'):
        assert name in state['tables'], 'Required history table missing'
    for name in ('intelligence_brightdata_account','intelligence_brightdata_executions','intelligence_brightdata_canary_runs','intelligence_linkedin_source_identities'):
        assert name in state['tables'], 'Required credit/ownership table missing'
    assert all(c['convalidated'] for c in state['schema']['constraints'] if c['contype']=='f'), 'Unvalidated FK requires DBA review'
    assert all(t['tableowner']=='credit_rehearsal' for t in state['schema']['ownership']), 'Unexpected private ownership'
    orphans=(await connection.execute(text("""SELECT
        (SELECT count(*) FROM intelligence_items i LEFT JOIN intelligence_sources s ON i.source_id=s.id WHERE s.id IS NULL) AS posts,
        (SELECT count(*) FROM intelligence_analyses a LEFT JOIN intelligence_items i ON a.news_item_id=i.id WHERE i.id IS NULL) AS analyses,
        (SELECT count(*) FROM intelligence_user_states u LEFT JOIN intelligence_items i ON u.news_item_id=i.id WHERE i.id IS NULL) AS states"""))).mappings().one()
    assert not any(orphans.values()), 'Orphaned relationships require DBA review'


async def rehearse(args):
    require_enabled_checks()
    if sys.platform=='win32' and not isinstance(asyncio.get_running_loop(),asyncio.SelectorEventLoop):
        raise ValueError('Private rehearsal requires the guarded Windows selector loop')
    value=os.environ.get('NEWS_DATABASE_URL','postgresql+asyncpg://invalid@invalid/invalid')
    assert_target_url(value)
    directory=Path(args.private_data_directory).resolve()
    if directory.name!='private-pg18-data' or 'private' not in {part.lower() for part in directory.parts}:
        raise ValueError('Expected new isolated data directory below private root')
    if not args.approval_ref.strip() or args.approval_ref.startswith(('REPLACE','APPROVAL_','TODO')):
        raise ValueError('An actual explicit backup/rehearsal approval is required')
    os.environ.update({'NEWS_ENABLE_COLLECTION':'false','NEWS_LINKEDIN_FETCH_ENABLED':'false',
        'NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED':'false','NEWS_AUTO_IMPORT_SNAPSHOT':'false',
        'NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA':'false','BRIGHTDATA_API_TOKEN':'','OPENAI_API_KEY':'',
        'EMAIL_USER':'','EMAIL_PASSWORD':'','NEWS_ADMIN_EMAIL':'rehearsal@example.invalid',
        'NEWS_ADMIN_PASSWORD':'private-no-login','NEWS_SESSION_SECRET':'private-no-server'})
    restore_network=install_private_network_guard()
    candidate=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(candidate/'backend'))
    engine=None
    try:
        from sqlalchemy import text
        from app.db import Base,engine
        from app.intelligence import models  # registers metadata, no collector
        from app.intelligence.credit_migration import migrate_credit_controls
        async with engine.begin() as connection:
            identity=dict((await connection.execute(text("""SELECT current_user AS role,current_database() AS database,
                inet_server_port() AS port,current_setting('server_version_num')::int AS version_num,
                current_setting('listen_addresses') AS listen_addresses,current_setting('data_directory') AS data_directory"""))).mappings().one())
            assert_private_identity(identity,directory)
            before=await capture(connection)
            await migrate_all(connection,Base,migrate_credit_controls)
            first=await capture(connection)
            assert_preserved(before['tables'],first['tables'])
            assert_schema_preserved(before['schema'],first['schema'])
            await validate_relationships(connection,first)
        async with engine.begin() as connection:
            await migrate_all(connection,Base,migrate_credit_controls)
            second=await capture(connection)
            assert_idempotent(first,second)
            await validate_relationships(connection,second)
        return {'result':'PASS_REHEARSAL_ONLY','migration_runs':2,'all_startup_migrations_repeated':True,
            'data_hashes_unchanged_except_documented_source_normalization':True,'idempotent_data_and_schema':True,
            'existing_public_table_count':len(before['tables']),
            'preserved_counts':{name:len(t['rows']) for name,t in before['tables'].items()},
            'after_table_fingerprints':{name:row_fingerprint(t['rows']) for name,t in second['tables'].items()},
            'schema_sha256':fingerprint(second['schema']),'private_identity_verified':True,
            'ownership_and_foreign_keys':'PASS','production_changed':False,'provider_requests':0,
            'approval_ref':args.approval_ref}
    finally:
        try:
            if engine is not None:await engine.dispose()
        finally:
            restore_network()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approval-ref',required=True)
    parser.add_argument('--private-data-directory',required=True,type=Path)
    args=parser.parse_args()
    try:
        configure_private_loop()
        print(json.dumps(asyncio.run(rehearse(args)),indent=2))
    except Exception:
        raise SystemExit('Rehearsal failed closed. No production target is permitted; keep raw database errors private and request review.') from None


if __name__=='__main__':main()
