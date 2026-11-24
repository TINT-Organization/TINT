import json
import threading
import urllib.error
import urllib.request

import pytest

from audit_log.health import create_server


@pytest.fixture
def base_url():
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_healthz_returns_ok(base_url):
    with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "ok", "service": "audit-log"}
        assert response.headers["Cache-Control"] == "no-store"


def test_any_other_path_is_not_found(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base_url}/api/v1/audit", timeout=2)
    assert exc.value.code == 404
