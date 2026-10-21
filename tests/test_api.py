"""Api.proxy_delay 走真实请求路径（MockTransport），覆盖 provider 节点测速路由（F-01～F-04b）。"""

import httpx
import pytest

from mihomo_cli.api import Api, ApiError, ApiUnavailable

MANUAL_NAME = "[M] 东京 01"


def _api(tmp_path, providers, delay_status=200, calls=None):
    """providers：{provider 名: [节点名]}；不在其中的 provider 返回 404。calls 收集请求的路径与参数。"""
    def handler(req):
        path = req.url.raw_path.decode().split("?")[0]
        if calls is not None:
            calls.append((path, dict(req.url.params)))
        parts = path.split("/")
        if path.endswith("/healthcheck"):
            if delay_status != 200:
                return httpx.Response(delay_status, json={"message": "x"})
            return httpx.Response(200, json={"delay": 123})
        if len(parts) == 4 and parts[1:3] == ["providers", "proxies"]:
            if parts[3] not in providers:
                return httpx.Response(404, json={"message": "Resource not found"})
            return httpx.Response(200, json={"proxies": [{"name": n} for n in providers[parts[3]]]})
        return httpx.Response(500)
    api = Api(str(tmp_path / "none.sock"))
    api._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mihomo")
    return api


def test_delay_main_provider(tmp_path):
    calls = []
    api = _api(tmp_path, {"main": ["JP 01"], "manual": []}, calls=calls)
    assert api.proxy_delay("JP 01", timeout_ms=3000) == 123
    path, params = calls[-1]
    assert path == "/providers/proxies/main/JP%2001/healthcheck"
    assert params["timeout"] == "3000" and params["url"].startswith("https://")


def test_delay_manual_provider_encoded(tmp_path):
    calls = []
    api = _api(tmp_path, {"main": ["JP 01"], "manual": [MANUAL_NAME]}, calls=calls)
    assert api.proxy_delay(MANUAL_NAME) == 123
    assert calls[-1][0] == "/providers/proxies/manual/%5BM%5D%20%E4%B8%9C%E4%BA%AC%2001/healthcheck"


@pytest.mark.parametrize("providers", [{"main": ["A"], "manual": ["B"]}, {"manual": ["B"]}])
def test_delay_missing_node_404(tmp_path, providers):
    calls = []
    api = _api(tmp_path, providers, calls=calls)
    with pytest.raises(ApiError) as ei:
        api.proxy_delay("nope")
    assert ei.value.status == 404
    assert not any(p.endswith("/healthcheck") for p, _ in calls)


@pytest.mark.parametrize("status", [504, 503])
def test_delay_error_status(tmp_path, status):
    api = _api(tmp_path, {"main": ["A"]}, delay_status=status)
    with pytest.raises(ApiError) as ei:
        api.proxy_delay("A")
    assert ei.value.status == status


def test_delay_unavailable(tmp_path):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)
    api = Api(str(tmp_path / "none.sock"))
    api._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mihomo")
    with pytest.raises(ApiUnavailable):
        api.proxy_delay("A")
