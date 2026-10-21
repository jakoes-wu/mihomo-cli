"""mihomo 管理接口客户端（unix 套接字）。

只做协议层：拼路径、设超时、把失败分成“连不上”和“接口返回错误”两类。
不做业务判断，也不重试——切节点、重载等都有副作用，是否重来由 actions 决定（规范 11.2）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import httpx

# 各调用的超时（秒），方案 5.1.5「管理接口超时」：
# PUT /configs 同步执行完整个应用配置并等所有 provider 加载完（hub/executor/executor.go:350），给 120 秒；
# 手动更新订阅 60 秒；测延迟为查询参数 timeout 加 5 秒；其余 10 秒。
TIMEOUT_RELOAD_SEC = 120.0
TIMEOUT_PROVIDER_UPDATE_SEC = 60.0
TIMEOUT_DEFAULT_SEC = 10.0
DELAY_URL = "https://www.gstatic.com/generate_204"
DELAY_TIMEOUT_MS = 5000
# 内核用 16 位有符号整数解析 timeout（hub/route/proxies.go strconv.ParseInt(..., 10, 16)），超过即 400。
DELAY_TIMEOUT_MAX_MS = 32767


class ApiUnavailable(Exception):
    """套接字不存在、拒绝连接或超时：内核不可达（退出码 3）。"""


class ApiError(Exception):
    """接口返回了非 2xx。status 为 HTTP 状态码，message 为内核给的 message 字段（已截断）。"""

    def __init__(self, method: str, path: str, status: int, message: str):
        super().__init__("%s %s -> %d %s" % (method, path, status, message))
        self.method = method
        self.path = path
        self.status = status
        self.message = message


def _q(name: str) -> str:
    # 节点名可能含空格、中文、emoji、斜杠；内核侧会 PathUnescape（hub/route/common.go:21）。
    return quote(name, safe="")


class Api:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self._client = httpx.Client(transport=httpx.HTTPTransport(uds=sock_path), base_url="http://mihomo")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Api":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, timeout: float = TIMEOUT_DEFAULT_SEC,
                 params: Optional[Dict[str, Any]] = None, body: Any = None) -> Any:
        try:
            resp = self._client.request(method, path, params=params, json=body, timeout=httpx.Timeout(timeout))
        except (httpx.ConnectError, httpx.TimeoutException, FileNotFoundError, ConnectionError) as exc:
            raise ApiUnavailable("%s %s: %s" % (method, path, type(exc).__name__)) from exc
        except httpx.TransportError as exc:
            raise ApiUnavailable("%s %s: %s" % (method, path, type(exc).__name__)) from exc
        if resp.status_code >= 400:
            message = ""
            try:
                message = str(resp.json().get("message", ""))
            except (ValueError, AttributeError):
                message = resp.text
            raise ApiError(method, path, resp.status_code, message[:300])
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ---- 只读查询 ----
    def version(self) -> Dict[str, Any]:
        return self._request("GET", "/version")

    def get_configs(self) -> Dict[str, Any]:
        return self._request("GET", "/configs")

    def get_proxies(self) -> Dict[str, Any]:
        return self._request("GET", "/proxies")["proxies"]

    def get_proxy(self, name: str) -> Dict[str, Any]:
        return self._request("GET", "/proxies/" + _q(name))

    def get_providers(self) -> Dict[str, Any]:
        return self._request("GET", "/providers/proxies")["providers"]

    def get_provider(self, name: str) -> Optional[Dict[str, Any]]:
        """provider 不存在时返回 None（重载核验按 0 个节点计，方案 5.1.5 第 5 步）。"""
        try:
            return self._request("GET", "/providers/proxies/" + _q(name))
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def provider_node_names(self, name: str) -> List[str]:
        prov = self.get_provider(name)
        if prov is None:
            return []
        return [p.get("name", "") for p in (prov.get("proxies") or [])]

    def connections(self) -> Dict[str, Any]:
        return self._request("GET", "/connections")

    # ---- 有副作用的操作 ----
    def select(self, group: str, name: str) -> None:
        self._request("PUT", "/proxies/" + _q(group), body={"name": name})

    def patch_configs(self, patch: Dict[str, Any]) -> None:
        self._request("PATCH", "/configs", body=patch)

    def reload(self, config_path: str) -> None:
        """PUT /configs?force=true，同步返回（等 provider 加载完）。超时按失败处理进入回滚。"""
        self._request("PUT", "/configs", timeout=TIMEOUT_RELOAD_SEC, params={"force": "true"},
                      body={"path": config_path})

    def update_provider(self, name: str) -> None:
        self._request("PUT", "/providers/proxies/" + _q(name), timeout=TIMEOUT_PROVIDER_UPDATE_SEC)

    def restart(self) -> None:
        self._request("POST", "/restart", body={"path": ""})

    def update_geo(self) -> None:
        self._request("POST", "/configs/geo", timeout=TIMEOUT_PROVIDER_UPDATE_SEC, body={})

    def proxy_delay(self, name: str, url: str = DELAY_URL, timeout_ms: int = DELAY_TIMEOUT_MS) -> int:
        """单节点测速。节点都在 provider（main / manual）里，不能用 /proxies/<名>/delay：
        那个路由只查顶层节点表（hub/route/proxies.go:46-55），对 provider 节点一律 404。
        provider 节点路由与它共用 getProxyDelay（hub/route/provider.go:30-36），参数与 404/503/504 语义相同。
        两个 provider 都找不到时抛 404，调用方据此判“没有该节点”。先查 main：手工节点名带 "[M] " 前缀，
        只有订阅节点本身以它开头才会重名。"""
        timeout_ms = min(int(timeout_ms), DELAY_TIMEOUT_MAX_MS)
        for provider in ("main", "manual"):
            if name in self.provider_node_names(provider):
                data = self._request("GET", "/providers/proxies/%s/%s/healthcheck" % (_q(provider), _q(name)),
                                     timeout=timeout_ms / 1000.0 + 5, params={"url": url, "timeout": timeout_ms})
                return int(data.get("delay", 0))
        raise ApiError("GET", "/providers/proxies/*/" + _q(name), 404, "Resource not found")

    def group_delay(self, group: str, url: str = DELAY_URL, timeout_ms: int = DELAY_TIMEOUT_MS) -> Dict[str, int]:
        timeout_ms = min(int(timeout_ms), DELAY_TIMEOUT_MAX_MS)
        data = self._request("GET", "/group/%s/delay" % _q(group), timeout=timeout_ms / 1000.0 + 5,
                             params={"url": url, "timeout": timeout_ms})
        return {k: int(v) for k, v in (data or {}).items()}

    def stream_logs(self, level: str = "info") -> Iterator[Dict[str, Any]]:
        """GET /logs 是长连接，每行一个 JSON；读超时设为无限，连接超时仍是 10 秒。"""
        timeout = httpx.Timeout(TIMEOUT_DEFAULT_SEC, read=None)
        try:
            with self._client.stream("GET", "/logs", params={"level": level}, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    raise ApiError("GET", "/logs", resp.status_code, "")
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except (httpx.ConnectError, FileNotFoundError, ConnectionError) as exc:
            raise ApiUnavailable("GET /logs: %s" % type(exc).__name__) from exc
