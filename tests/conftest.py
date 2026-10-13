import importlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope='session')
def provider():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'tests.mcp_fixture:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:' + str(port)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('MCP fixture failed to start')
            try:
                if requests.get(base + '/fixture/state', timeout=.5).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(.05)
        else:
            raise RuntimeError('MCP fixture startup timed out')
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def portal(tmp_path, monkeypatch, provider):
    import mcp_connections
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DEMO_MODE', '0')
    monkeypatch.setenv('NOTION_DATA_SOURCE_ID','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    monkeypatch.setenv('GOOGLE_DRIVE_FOLDER_ID','fixture_contracts_folder')
    monkeypatch.setenv('GOOGLE_UPLOAD_FOLDER_ID','fixture_uploads_folder')
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://localhost:3000')
    monkeypatch.setenv('PORTAL_PASSWORD', 'fixture-password')
    monkeypatch.delenv('FERNET_KEY', raising=False)
    monkeypatch.setitem(mcp_connections.ENDPOINTS, 'notion', provider + '/provider/mcp')
    monkeypatch.setitem(mcp_connections.ENDPOINTS, 'google', provider + '/provider/mcp')
    requests.post(provider + '/fixture/reset').raise_for_status()
    sys.modules.pop('app', None)
    module = importlib.import_module('app')
    for name in ('notion', 'google'):
        module.connections.write(name + ':grant', {'access_token': 'fixture-secret-token', 'refresh_token': 'fixture-refresh-token', 'expires_at': time.time() + 3600})
    with TestClient(module.app) as client:
        client.auth = ('finance', 'fixture-password')
        yield module, client, provider
