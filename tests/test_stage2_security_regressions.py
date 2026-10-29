import os
import pytest
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from core.providers import env_quote, BASE_DIR
from routers.online_image import normalize_local_image_path
from routers.chat import clean_cli_raw

client = TestClient(app)

def test_t1_auth_token_status():
    resp = client.get("/api/auth/token-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "auth_required" in data

def test_t4_download_output_ssrf_blocked():
    resp = client.get("/api/download-output", params={"url": "http://127.0.0.1:8000/secret"})
    assert resp.status_code in (400, 403, 502)

def test_t5_env_quote_newline_injection():
    with pytest.raises(ValueError, match="换行或空字符"):
        env_quote("foo\nHTTP_PROXY=http://127.0.0.1:9999")
    with pytest.raises(ValueError, match="换行或空字符"):
        env_quote("bar\r\nEVIL=1")
    assert env_quote("normal_key_123") == "normal_key_123"

def test_t8_view_path_traversal():
    resp = client.get("/api/view", params={"filename": "../../../../etc/passwd"})
    assert resp.status_code == 400
    resp2 = client.get("/api/view", params={"filename": "test.png", "subfolder": "../secret"})
    assert resp2.status_code == 400

def test_t9_clean_cli_raw():
    raw_with_secrets = {
        "text": "hello",
        "_stdout": "secret token in stdout",
        "_stderr": "/path/to/profile.json",
        "usage": {"total_tokens": 10},
    }
    cleaned = clean_cli_raw(raw_with_secrets)
    assert "_stdout" not in cleaned
    assert "_stderr" not in cleaned
    assert cleaned["text"] == "hello"
    assert cleaned["usage"] == {"total_tokens": 10}

def test_t10_normalize_local_image_path_boundary():
    with pytest.raises(HTTPException) as excinfo:
        normalize_local_image_path("C:/Windows/System32/notepad.exe")
    assert excinfo.value.status_code == 400

    with pytest.raises(HTTPException) as excinfo_posix:
        normalize_local_image_path("/etc/passwd")
    assert excinfo_posix.value.status_code == 400

def test_runninghub_upload_asset_blocks_private_url():
    """upload-asset 不得请求内网地址（真实网络行为验证，断言内网服务零连接）"""
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    hits = []
    def serve():
        try:
            srv.settimeout(2.0)
            conn, _ = srv.accept()
            hits.append(conn.recv(256))
            conn.close()
        except Exception:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    try:
        with patch("routers.integrations.runninghub_api_key", return_value="mock-key"):
            resp = client.post("/api/runninghub/upload-asset", json={"url": f"http://127.0.0.1:{port}/secret.txt"})
            assert resp.status_code == 400
            assert "SSRF" in resp.text or "回环" in resp.text or "本地" in resp.text or "受限" in resp.text
            assert hits == [], "FAIL: 内网服务收到了请求 —— SSRF 未拦截，连接已穿透到本地端口"
    finally:
        srv.close()

