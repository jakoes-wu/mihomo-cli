"""下载、校验并安装 mihomo 内核与地区数据库（方案 5.1.5「下载地址拼法」）。

内核是 GPL-3.0，不随包分发，由 install / core upgrade 从官方发布页（或镜像）下载。
摘要不符一律拒绝安装、原内核不变；拿不到摘要时降级为“完整解压 + -v 版本对得上”并给出警告。
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

import httpx

from .render import GITHUB

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
MIN_CORE_VERSION = (1, 19, 0)
GEODATA_FILES = ("geoip.metadb", "geosite.dat")


def geodata_path(home: str, name: str) -> Optional[str]:
    """按文件名不区分大小写找主目录里已有的地区数据库，找不到返回 None。

    内核同样不区分大小写查找（constant/path.go 的 GeoSite / MMDB 用 EqualFold），且文件缺失时
    经 /configs/geo 下载会存成 GeoSite.dat。只按小写名判断会把内核下载的文件当成缺失，
    再另下一份小写的，目录里出现两份，内核仍读先列出的旧文件。
    """
    try:
        entries = os.listdir(home)
    except OSError:
        return None
    for entry in sorted(entries):
        if entry.lower() == name.lower() and os.path.isfile(os.path.join(home, entry)):
            return os.path.join(home, entry)
    return None
# 下载超时（秒）：连接 15 秒；内核约 36 MB，读超时按“单次读等待”而非总时长计。
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_HEX64 = re.compile(r"\b([0-9a-fA-F]{64})\b")


class DownloadError(Exception):
    """下载失败或校验不通过；原内核保持不变。"""


def normalize_version(raw: str) -> str:
    """`1.19.21` 与 `v1.19.21` 等价；格式不对抛 ValueError（调用方映射为退出码 2）。"""
    raw = (raw or "").strip()
    if raw and not raw.startswith("v"):
        raw = "v" + raw
    if not VERSION_RE.match(raw):
        raise ValueError(raw)
    return raw


def version_tuple(version: str) -> Tuple[int, int, int]:
    nums = re.findall(r"\d+", version)[:3]
    while len(nums) < 3:
        nums.append("0")
    return int(nums[0]), int(nums[1]), int(nums[2])


def core_url(base: str, version: str, asset: str) -> str:
    return "%s/MetaCubeX/mihomo/releases/download/%s/%s" % (base, version, asset)


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT)


def latest_version(base: str) -> str:
    url = "%s/MetaCubeX/mihomo/releases/latest/download/version.txt" % base
    try:
        with _client() as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise DownloadError("version.txt: %s" % type(exc).__name__) from exc
    if resp.status_code != 200:
        raise DownloadError("version.txt: HTTP %d" % resp.status_code)
    try:
        return normalize_version(resp.text.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise DownloadError("version.txt: unexpected content") from exc


def expected_digest(base: str, version: str, asset: str) -> Optional[str]:
    """取资产的 sha256：GitHub 用接口的 digest 字段，镜像读同目录 <资产名>.sha256；都取不到返回 None。"""
    try:
        with _client() as client:
            if base == GITHUB:
                resp = client.get("https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/%s" % version,
                                  headers={"Accept": "application/vnd.github+json"})
                if resp.status_code != 200:
                    return None
                for item in resp.json().get("assets", []):
                    if item.get("name") == asset:
                        digest = item.get("digest") or ""
                        if digest.startswith("sha256:"):
                            return digest.split(":", 1)[1].lower()
                return None
            resp = client.get(core_url(base, version, asset) + ".sha256")
            if resp.status_code != 200:
                return None
            match = _HEX64.search(resp.text)
            return match.group(1).lower() if match else None
    except (httpx.HTTPError, ValueError):
        return None


def download_to(url: str, dest: str) -> str:
    """流式下载到 dest，返回 sha256。失败时删除不完整文件。"""
    sha = hashlib.sha256()
    try:
        with _client() as client, client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise DownloadError("%s: HTTP %d" % (url, resp.status_code))
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    sha.update(chunk)
    except httpx.HTTPError as exc:
        _unlink(dest)
        raise DownloadError("%s: %s" % (url, type(exc).__name__)) from exc
    except BaseException:
        _unlink(dest)
        raise
    return sha.hexdigest()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def core_version(core_path: str) -> Tuple[Optional[str], str]:
    """运行 `mihomo -v`，返回 (版本号或 None, 原始输出)。"""
    try:
        proc = subprocess.run([core_path, "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    out = proc.stdout or ""
    match = re.search(r"\bv\d+\.\d+\.\d+\b", out)
    return (match.group(0) if match else None), out


def install_core(core_path: str, workdir: str, asset: str, base: str, version: str) -> Optional[str]:
    """下载并安装指定版本的内核到 core_path。

    返回 None 表示已校验摘要；返回字符串为降级警告（未能取得摘要）。
    任何失败都抛 DownloadError，且 core_path 原文件不变（新文件先写到 workdir，校验通过才 os.replace）。
    """
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(os.path.dirname(core_path), exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="core-", dir=workdir)
    try:
        gz_path = os.path.join(tmp_dir, asset)
        actual = download_to(core_url(base, version, asset), gz_path)
        expected = expected_digest(base, version, asset)
        warning = None
        if expected is not None:
            if actual != expected:
                raise DownloadError("sha256 mismatch for %s: expected %s, got %s" % (asset, expected, actual))
        else:
            warning = "digest unavailable for %s; only gzip integrity and version were checked" % asset
        bin_tmp = os.path.join(tmp_dir, "mihomo")
        try:
            with gzip.open(gz_path, "rb") as src, open(bin_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, EOFError) as exc:
            raise DownloadError("corrupted archive %s: %s" % (asset, exc)) from exc
        os.chmod(bin_tmp, 0o755)
        got, _ = core_version(bin_tmp)
        if got != version:
            raise DownloadError("downloaded core reports version %r, expected %s" % (got, version))
        os.replace(bin_tmp, core_path)
        return warning
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def install_geodata(home: str, base: str) -> None:
    """下载地区数据库到主目录；内核按文件名（不区分大小写）查找 geoip.metadb 与 GeoSite.dat。"""
    for name in GEODATA_FILES:
        url = "%s/MetaCubeX/meta-rules-dat/releases/download/latest/%s" % (base, name)
        # 已有其它大小写的同名文件（如内核下载的 GeoSite.dat）就覆盖它，不另建一份
        dest = geodata_path(home, name) or os.path.join(home, name)
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", dir=home)
        os.close(fd)
        try:
            download_to(url, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        finally:
            _unlink(tmp)
