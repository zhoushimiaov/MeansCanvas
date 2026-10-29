"""Deterministic regressions for credential redirects and canvas save versions."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import security
from core.canvas_cache import CanvasCache
from routers import canvas


@pytest.mark.parametrize("destination", [
    "https://cdn.example/image.png",
    "https://provider.example:8443/image.png",
    "http://provider.example/image.png",
])
def test_download_drops_caller_headers_across_origins(monkeypatch, destination):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": destination})
        if len(requests) == 2:
            return httpx.Response(302, headers={"Location": "https://provider.example/final.png"})
        return httpx.Response(200, content=b"image", headers={"Content-Type": "image/png"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(handler), trust_env=False))
    monkeypatch.setattr(security, "validate_safe_url", lambda url, **kw: url)
    headers = {"aUtHoRiZaTiOn": "Bearer synthetic-secret", "Cookie": "session=synthetic",
               "X-Api-Key": "synthetic-key", "X-Custom-Secret": "synthetic-custom"}
    result = asyncio.run(security.safe_download_bytes(
        "https://provider.example/start", headers=headers))
    assert result == (b"image", "image/png")
    assert requests[0].headers["authorization"] == headers["aUtHoRiZaTiOn"]
    for redirected in requests[1:]:
        for name in headers:
            assert name not in redirected.headers
    assert requests[1].headers["user-agent"]
    assert headers["X-Api-Key"] == "synthetic-key"


def test_download_preserves_headers_for_relative_same_origin_redirect(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "/image.png"})
        return httpx.Response(200, content=b"image")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(handler), trust_env=False))
    monkeypatch.setattr(security, "validate_safe_url", lambda url, **kw: url)
    asyncio.run(security.safe_download_bytes(
        "https://provider.example:443/start", headers={"Authorization": "Bearer synthetic"}))
    assert requests[1].headers["authorization"] == "Bearer synthetic"


@pytest.mark.parametrize("clock_offset", [0, -1000])
def test_save_versions_advance_and_reject_old_snapshot(monkeypatch, tmp_path, clock_offset):
    monkeypatch.setattr(canvas, "CANVAS_DIR", str(tmp_path))
    monkeypatch.setattr(canvas, "canvas_cache", CanvasCache(str(tmp_path), canvas.canvas_record))
    tick = 1800000000000
    monkeypatch.setattr(canvas, "now_ms", lambda: tick)
    record = canvas.new_canvas("original")
    base = record["updated_at"]
    monkeypatch.setattr(canvas, "now_ms", lambda: tick + clock_offset)
    app = FastAPI()
    app.include_router(canvas.router)
    with TestClient(app) as client:
        first = client.put(f"/api/canvases/{record['id']}", json={
            "title": "first writer", "base_updated_at": base})
        assert first.status_code == 200
        assert first.json()["canvas"]["updated_at"] > base
        stale = client.put(f"/api/canvases/{record['id']}", json={
            "title": "stale writer", "base_updated_at": base})
        assert stale.status_code == 409
        assert json.loads((tmp_path / f"{record['id']}.json").read_text(
            encoding="utf-8"))["title"] == "first writer"
