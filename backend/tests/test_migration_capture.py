import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import text

from test_brightdata_postgres_integration import database, private_postgres


def test_preservation_capture_survives_additive_ddl_on_cached_connection(database):
    engine,_=database
    path=Path(__file__).resolve().parents[2]/'deploy/verify-credit-migration.py'
    spec=importlib.util.spec_from_file_location('migration_verification',path)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    async def run():
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE verification_shape_test (id INTEGER PRIMARY KEY)'))
            await connection.execute(text('INSERT INTO verification_shape_test VALUES (1)'))
            before=await helper.capture(connection)
            await connection.execute(text('ALTER TABLE verification_shape_test ADD COLUMN note TEXT'))
            after=await helper.capture(connection)
            helper.assert_preserved(before['tables'],after['tables'])
            again=await helper.capture(connection)
            helper.assert_idempotent(after,again)
            assert after['tables']['verification_shape_test']['rows']==[{'id':1,'note':None}]
            await connection.execute(text('DROP TABLE verification_shape_test'))
    asyncio.run(run())
