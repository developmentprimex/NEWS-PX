"""Tests never load provider credentials or use an application database."""
import os
import socket

import pytest

os.environ.update({
    "NEWS_DATABASE_URL": "postgresql+asyncpg://credit_test@127.0.0.1:55439/postgres",
    "NEWS_ADMIN_EMAIL": "test@example.com", "NEWS_ADMIN_PASSWORD": "test-password",
    "NEWS_SESSION_SECRET": "local-test-secret", "NEWS_ENABLE_COLLECTION": "false",
    "NEWS_AUTO_IMPORT_SNAPSHOT": "false", "NEWS_LINKEDIN_FETCH_ENABLED": "false",
    "NEWS_LINKEDIN_HIGH_PRIORITY_EXTRA": "false", "BRIGHTDATA_API_TOKEN": "test-token",
    "NEWS_BRIGHTDATA_CANARY_LIVE_ENABLED": "false",
    "OPENAI_API_KEY": "", "EMAIL_USER": "", "EMAIL_PASSWORD": "",
    "NEWS_TRIAL_EMAIL_USER": "", "NEWS_TRIAL_EMAIL_PASSWORD": "", "NEWS_TRIAL_BILLING_BINDING_FILE": "",
})


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(f"External network forbidden in tests: {address[0]}")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    original_dns = socket.getaddrinfo
    def dns(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            raise AssertionError("External DNS forbidden in tests")
        return original_dns(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
