"""Run only with PrimeFlow's Python, from its backend directory.

The database transaction is read-only. Only a short-lived GA session is returned
over the private subprocess pipe; credentials never leave PrimeFlow's runtime.
"""
import asyncio
import json
import sys
from pathlib import Path


async def main(username):
    sys.path.insert(0, str(Path.cwd()))
    from sqlalchemy import text
    from app.db import SessionLocal, engine
    from app.auth.security import create_access_token

    try:
        async with SessionLocal() as db:
            await db.execute(text('SET TRANSACTION READ ONLY'))
            user = (await db.execute(text('''SELECT id, username, email, full_name, role,
                department_id FROM users WHERE lower(username)=:username AND is_active=true'''),
                {'username': username.casefold()})).mappings().one()
            token = create_access_token(user_id=user['id'], role=user['role'],
                                        department_id=user['department_id'])
            print(json.dumps({'token': token, 'user': dict(user)}, default=str))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(main(sys.argv[1]))
    except Exception:
        print('GA export session unavailable', file=sys.stderr)
        raise SystemExit(1)
