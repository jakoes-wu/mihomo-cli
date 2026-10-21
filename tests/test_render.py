"""R-01：render 对三种来源与各开关组合生成的配置。"""

import copy
import re

import pytest
import yaml

from mihomo_cli import render
from mihomo_cli.state import Paths, default_state, new_profile


@pytest.fixture
def paths(tmp_path):
    return Paths(str(tmp_path))


def _state(kind, manual=(), hidden=(), **kw):
    st = default_state()
    prof = new_profile(kind, url="https://sub.example.com/x?token=t" if kind == "subscription" else None,
                       file="/tmp/p.yaml" if kind == "file" else None, interval=3600,
                       user_agent=kw.pop("ua", None))
    prof["manual"] = [{"link": link, "name": None} for link in manual]
    prof["hidden"] = list(hidden)
    st["profiles"]["p"] = prof
    st["active_profile"] = "p"
    st.update(kw)
    return st


def test_vps_only_manual(paths):
    cfg = render.render(_state("nodes", manual=["ss://a@1.2.3.4:8388#a"]), paths)
    assert list(cfg["proxy-providers"]) == ["manual"]
    proxy = cfg["proxy-groups"][0]
    assert proxy["use"] == ["manual"] and proxy["proxies"] == ["REJECT"]
    assert cfg["proxy-providers"]["manual"]["override"]["additional-prefix"] == "[M] "


def test_subscription_without_manual_has_no_manual_provider(paths):
    cfg = render.render(_state("subscription", ua="clash-verge"), paths)
    assert list(cfg["proxy-providers"]) == ["main"]
    main = cfg["proxy-providers"]["main"]
    assert main["type"] == "http" and main["interval"] == 3600
    assert main["header"] == {"User-Agent": ["clash-verge"]}
    assert cfg["proxy-groups"][0]["use"] == ["main"]


def test_file_with_manual(paths):
    cfg = render.render(_state("file", manual=["vless://u@h:443#x"]), paths)
    assert cfg["proxy-providers"]["main"]["type"] == "file"
    assert cfg["proxy-groups"][0]["use"] == ["main", "manual"]


def test_hidden_filter_on_proxy_group(paths):
    names = ["HK 01 (IPLC)", "日本.东京", "🇺🇸 US"]
    cfg = render.render(_state("subscription", hidden=names), paths)
    flt = cfg["proxy-groups"][0]["exclude-filter"]
    for n in names:
        assert re.search(flt, n)
    assert not re.search(flt, "HK 01")
    assert "exclude-filter" not in cfg["proxy-providers"]["main"]


def test_groups_rules_and_global(paths):
    cfg = render.render(_state("subscription", mode="global", cn_route="proxy"), paths)
    groups = {g["name"]: g for g in cfg["proxy-groups"]}
    assert groups["GLOBAL"]["proxies"] == ["PROXY"]
    assert groups["CN"]["proxies"] == ["DIRECT", "PROXY"]
    assert cfg["mode"] == "global"
    assert cfg["rules"][-1] == "MATCH,PROXY"
    assert "GEOSITE,cn,CN" in cfg["rules"]
    assert cfg["external-controller"] == ""


def test_tun_and_suspended(paths):
    st = _state("subscription", tun=True)
    cfg = render.render(st, paths)
    assert cfg["tun"]["device"] == render.TUN_DEVICE
    assert cfg["dns"]["listen"] == "127.0.0.1:53"
    servers = yaml.safe_dump(cfg["dns"])
    assert "system" not in servers and "dhcp" not in servers  # R-15 ⑥
    st2 = copy.deepcopy(st)
    st2["tun_suspended"] = "other tun: tun0"
    cfg2 = render.render(st2, paths)
    assert "tun" not in cfg2 and "dns" not in cfg2


def test_mirror_geox(paths):
    st = _state("subscription", mirror="http://10.0.0.2")
    cfg = render.render(st, paths)
    assert cfg["geox-url"]["mmdb"].startswith("http://10.0.0.2/MetaCubeX/meta-rules-dat/")
    assert "geox-url" not in render.render(_state("subscription"), paths)


def test_no_profile(paths):
    cfg = render.render(default_state(), paths)
    assert "proxy-providers" not in cfg
    assert cfg["proxy-groups"][0]["use"] == []


def test_render_text_header(paths):
    assert render.render_text(_state("nodes", manual=["ss://a@b:1#c"]), paths).startswith(render.HEADER)


def test_split_links_same_line():
    from mihomo_cli.nodes import split_links

    text = "ss://a@1.1.1.1:1#a  vless://u@2.2.2.2:443#HK 01\n# 注释\n\ntrojan://p@3.3.3.3:443#c"
    assert split_links(text) == ["ss://a@1.1.1.1:1#a", "vless://u@2.2.2.2:443#HK 01", "trojan://p@3.3.3.3:443#c"]
