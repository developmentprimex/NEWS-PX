import asyncio
import httpx
import pytest
from app.intelligence import facebook_resolution as service

def transport(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(service.httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handler)))
    async def public(url):pass
    monkeypatch.setattr(service,'_check_public_dns',public)

def test_public_share_redirect_is_verified_without_credentials_or_provider(monkeypatch):
    def response(req):
        if '/share/' in req.url.path:return httpx.Response(302,headers={'location':'/EULEX.KOSOVO/'})
        return httpx.Response(200,text='<html><title>EULEX Kosovo | Pristina</title><link rel="canonical" href="https://www.facebook.com/EULEX.KOSOVO/"></html>')
    transport(monkeypatch,response)
    result=asyncio.run(service.resolve_facebook_url('https://www.facebook.com/share/abc/'))
    assert result=={'url':'https://www.facebook.com/eulex.kosovo/','name':'EULEX Kosovo - Facebook'}

@pytest.mark.parametrize('destination',['https://evil.test/page','http://127.0.0.1/','https://facebook.com:444/test','https://www.facebook.com/login/'])
def test_resolution_rejects_foreign_redirects_and_login_pages(monkeypatch,destination):
    transport(monkeypatch,lambda req:httpx.Response(302,headers={'location':destination}) if '/share/' in req.url.path else httpx.Response(200,text='<title>Log in to Facebook</title>'))
    with pytest.raises(ValueError):asyncio.run(service.resolve_facebook_url('https://www.facebook.com/share/abc/'))
