"""手工节点与隐藏名单的纯函数：生成节点文件内容、隐藏用正则、链接脱敏。

本项目不在 Python 里解析任何代理协议（方案 5.1.3）：节点文件交给 mihomo 的
ConvertsV2Ray 解析，节点名由重载后从管理接口回读。
"""

from __future__ import annotations

import base64
import re
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

# 手工节点由 provider 的 override.additional-prefix 统一加这个前缀（方案 5.1.4）。
MANUAL_PREFIX = "[M] "

# 组层正则只转义这些元字符：mihomo 用 regexp2（.NET 语法）编译 exclude-filter，
# 非法正则会让解析 panic（adapter/outboundgroup/groupbase.go:61），所以正则只能由本函数生成。
_REGEX_META = set("\\.^$|?*+()[]{}#")


class NodeNameError(ValueError):
    """节点名不能被隐藏（含反引号或名为 REJECT）。"""


def encode_manual_file(links: Iterable[str]) -> str:
    """把链接列表整体 base64 编码成一行。

    明文链接若含 ": " 可能被当成 YAML 映射解析成功、随后报“缺 proxies 字段”而不再走链接解析；
    base64 单行在 YAML 层只是字符串，稳定进入 ConvertsV2Ray（方案 5.1.3，R-03 用真内核核实）。
    """
    joined = "\n".join(link.strip() for link in links if link.strip())
    return base64.b64encode(joined.encode("utf-8")).decode("ascii") + "\n"


def escape_name(name: str) -> str:
    if "`" in name:
        # mihomo 按反引号切分 filter 字符串，名字里有反引号无法表达。
        raise NodeNameError(name)
    return "".join("\\" + ch if ch in _REGEX_META else ch for ch in name)


def hidden_filter(names: Iterable[str]) -> Optional[str]:
    """由隐藏名单生成 PROXY 组的 exclude-filter；名单为空返回 None（不写该字段）。"""
    items: List[str] = []
    for name in names:
        if name == "REJECT":
            # REJECT 是“无节点时阻断”的兜底成员，绝不能被过滤掉（方案 5.1.4）。
            raise NodeNameError(name)
        items.append(escape_name(name))
    if not items:
        return None
    return "^(?:" + "|".join(items) + ")$"


_LINK_START = re.compile(r"\s+(?=[A-Za-z][A-Za-z0-9+.-]*://)")


def split_links(text: str) -> List[str]:
    """从文本中取出分享链接：去空行、去首尾空白、跳过 # 注释行。

    同一行里的多条链接也要拆开：菜单的输入框只有一行，用户会用空格分隔粘贴多条。
    只在“空白后紧跟 协议://”处拆——有些客户端导出的链接 # 后的节点名带未编码空格（如 #HK 01），
    按任意空白拆会把名字截断。
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.extend(part for part in _LINK_START.split(line) if part)
    return out


def redact_url(url: Optional[str]) -> str:
    """订阅链接含令牌，展示与日志里只保留“协议://域名”（方案 §7 安全）。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        # .port 对非法端口（:99999、:abc）会抛 ValueError，必须在 try 内，否则一条坏链接让 status 崩溃。
        if parts.port:
            host = "%s:%d" % (host, parts.port)
    except ValueError:
        return "<invalid-url>"
    return "%s://%s/…" % (parts.scheme or "?", host) if host else "<redacted>"


def link_summary(link: str) -> str:
    """回读名字之前，手工节点只显示协议与服务器地址，不显示密码 / uuid。"""
    scheme = link.split("://", 1)[0] if "://" in link else "?"
    try:
        parts = urlsplit(link)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        host, port = "", None
    if not host:
        return "%s://…" % scheme
    return "%s://%s%s" % (scheme, host, (":%d" % port) if port else "")
