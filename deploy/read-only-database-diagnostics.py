"""Run on the DB server; SELECT-only, no app import, scheduler or provider calls.

Usage: python -B read-only-database-diagnostics.py --env-file E:/websites/news-intelligence/.env
For another candidate DB use --database NAME --port PORT --role ROLE --prompt-password.
Only localhost targets are accepted. Send stdout JSON; never send .env files.
"""
import argparse
import asyncio
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, unquote
from urllib.request import Request, build_opener, HTTPCookieProcessor
import http.cookiejar

import asyncpg
from dotenv import dotenv_values


async def main(args):
    values = dotenv_values(args.env_file)
    url = urlsplit(values["NEWS_DATABASE_URL"].replace("postgresql+asyncpg:", "postgresql:"))
    if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Run on the database server with a verified localhost target")
    role, port, database = args.role or unquote(url.username or ""), args.port or url.port or 5432, args.database or url.path.lstrip("/")
    password = getpass.getpass("Database password (not printed): ") if args.prompt_password else unquote(url.password or "")
    result = {"captured_at": datetime.now(timezone.utc).isoformat(), "target": {"host": "127.0.0.1", "port": port, "role": role, "database": database},
              "production_modified": False, "provider_requests": 0}
    # Loopback only. SSL may be disabled here without sending credentials on the LAN.
    conn = await asyncpg.connect(host="127.0.0.1", port=port, user=role, password=password,
        database=database, timeout=10, ssl=False, server_settings={"application_name": "primex_readonly_diagnostics", "default_transaction_read_only": "on", "statement_timeout": "10000"})
    try:
        async with conn.transaction(readonly=True):
            queries = {
                "identity": "SELECT current_database() database,current_user role,current_setting('server_version') version,current_setting('TimeZone') timezone,current_setting('ssl') ssl,current_setting('transaction_read_only') readonly,inet_server_port() port,current_setting('search_path') search_path",
                "databases": "SELECT datname,pg_get_userbyid(datdba) owner,has_database_privilege(current_user,oid,'CONNECT') can_connect FROM pg_database WHERE NOT datistemplate",
                "tables": "SELECT schemaname,tablename,tableowner FROM pg_tables WHERE tablename LIKE 'intelligence_%'",
                "permissions": "SELECT has_schema_privilege(current_user,'public','USAGE') schema_usage,has_schema_privilege(current_user,'public','CREATE') schema_create",
                "connections": "SELECT application_name,client_addr,state,count(*) count FROM pg_stat_activity WHERE datname=current_database() GROUP BY 1,2,3",
                "row_security": "SELECT n.nspname schema,c.relname table_name,c.relrowsecurity enabled,c.relforcerowsecurity forced FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname LIKE 'intelligence_%' AND c.relkind='r'",
                "foreign_keys": "SELECT conrelid::regclass::text table_name,conname,pg_get_constraintdef(oid) definition FROM pg_constraint WHERE contype='f' AND conrelid::regclass::text LIKE '%intelligence_%' ORDER BY 1,2",
                "table_permissions": "SELECT schemaname,tablename,tableowner=current_user role_owns_table,has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'SELECT') can_select FROM pg_tables WHERE tablename LIKE 'intelligence_%'",
            }
            for name, query in queries.items():
                result[name] = [dict(r) for r in await conn.fetch(query)]
            if await conn.fetchval("SELECT to_regclass('public.intelligence_sources')"):
                result["sources"] = [dict(r) for r in await conn.fetch("SELECT s.id,s.name,s.type,s.status,s.url,s.fetch_interval_minutes,s.pending_snapshot_id,count(i.id) stored_posts,count(a.news_item_id) posts_with_analysis FROM intelligence_sources s LEFT JOIN intelligence_items i ON i.source_id=s.id LEFT JOIN intelligence_analyses a ON a.news_item_id=i.id GROUP BY s.id ORDER BY s.type,s.name")]
                result["linkedin_post_ids"] = [dict(r) for r in await conn.fetch("SELECT i.id,i.source_id,i.external_id,i.url FROM intelligence_items i JOIN intelligence_sources s ON s.id=i.source_id WHERE s.type='LINKEDIN' ORDER BY i.source_id,i.id")]
                result["counts"] = dict(await conn.fetchrow("SELECT (SELECT count(*) FROM intelligence_sources) sources,(SELECT count(*) FROM intelligence_items) posts,(SELECT count(*) FROM intelligence_analyses) analyses,(SELECT count(*) FROM intelligence_user_states) user_states"))
                result["schema"] = [dict(r) for r in await conn.fetch("SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name LIKE 'intelligence_%' ORDER BY table_name,ordinal_position")]
    finally:
        await conn.close()
    # Stored-data-only API comparison; no collection/check endpoints are used.
    if args.compare_api and not args.database and not args.role and not args.port:
        for origin in ("http://127.0.0.1:8001", "http://127.0.0.1:3101", values.get("NEWS_PUBLIC_ORIGIN", "")):
            parsed = urlsplit(origin)
            if parsed.scheme != "https" and parsed.hostname != "127.0.0.1":
                continue
            opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def api(path, payload=None):
                req = Request(origin + "/api/" + path, data=json.dumps(payload).encode() if payload else None,
                    headers={"Content-Type": "application/json", "Origin": values.get("NEWS_PUBLIC_ORIGIN", origin), "User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
                with opener.open(req, timeout=10) as response:
                    return json.load(response)
            try:
                api("auth/login", {"email": values["NEWS_ADMIN_EMAIL"], "password": values["NEWS_ADMIN_PASSWORD"]})
                sources = api("intelligence/sources")
                result.setdefault("api_sources", {})[origin] = [{k: s.get(k) for k in ("id", "name", "type", "status")} for s in sources]
            except Exception as exc:
                result.setdefault("api_errors", {})[origin] = {"type": type(exc).__name__, "http_status": getattr(exc, "code", None)}
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--database")
    parser.add_argument("--port", type=int)
    parser.add_argument("--role")
    parser.add_argument("--prompt-password", action="store_true")
    parser.add_argument("--compare-api", action="store_true")
    asyncio.run(main(parser.parse_args()))
