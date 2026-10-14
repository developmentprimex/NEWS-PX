"""Repair only reversible encoding damage in curated repost-watch names."""
import argparse
import http.cookiejar
import json
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("url", "type", "status", "priority", "categories", "ai_instructions",
          "fetch_interval_minutes", "email_enabled", "repost_groups", "repost_guidance")

def damaged_versions(name):
    versions = {name}
    pending = {name}
    for _ in range(2):
        next_round = set()
        for value in pending:
            for encoding in ("latin1", "cp1252"):
                try:
                    next_round.add(value.encode("utf-8").decode(encoding))
                except UnicodeDecodeError:
                    pass
        versions.update(next_round)
        pending = next_round
    return versions - {name}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--server-settings", action="store_true")
    args = parser.parse_args()
    settings_path = Path(r"\\192.168.10.8\e$\websites\news-intelligence\.env") if args.server_settings else ROOT / ".env"
    env = dotenv_values(settings_path)
    origin = "https://news.primexeu.com"
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def api(route, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(origin + "/api/" + route, data=data, method=method,
                          headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "Mozilla/5.0", "Origin": origin, "Referer": origin + "/login"})
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    api("auth/login", "POST", {"email": env["NEWS_ADMIN_EMAIL"], "password": env["NEWS_ADMIN_PASSWORD"]})
    manifest = json.loads((ROOT / "data/repost-watch-sources.json").read_text(encoding="utf-8"))
    from urllib.parse import unquote, urlparse
    def identity(url):
        return unquote(urlparse(url).path).strip("/").casefold()
    canonical = {identity(row["url"]): row["name"] for row in manifest["sources"]}
    sources = api("intelligence/sources")
    changes = []
    for source in sources:
        expected = canonical.get(identity(source["url"])) if source["type"] == "LINKEDIN" else None
        if expected and source["name"] in damaged_versions(expected):
            changes.append({"id": source["id"], "before": source["name"], "after": expected})
            if args.apply:
                payload = {field: source[field] for field in FIELDS}
                payload["name"] = expected
                result = api("intelligence/sources/" + source["id"], "PUT", payload)
                assert result["name"] == expected
                assert all(result[field] == source[field] for field in FIELDS)
    if args.apply:
        verified = {source["id"]: source for source in api("intelligence/sources")}
        for change in changes:
            assert verified[change["id"]]["name"] == change["after"]
    print(json.dumps({"applied": args.apply, "changes": changes}, ensure_ascii=True, indent=2))

if __name__ == "__main__":
    main()
