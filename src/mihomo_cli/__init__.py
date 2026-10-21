"""mihomo-cli：以 mihomo 为内核的命令行代理客户端。"""

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
except ImportError:  # pragma: no cover - Python 3.8 起标准库自带 importlib.metadata
    _pkg_version = None
    PackageNotFoundError = Exception

try:
    # 版本号由 hatch-vcs 从 git 标签写进包元数据，代码里不写死（方案 5.1.11）。
    __version__ = _pkg_version("mihomo-cli") if _pkg_version else "0.0.0"
except PackageNotFoundError:
    __version__ = "0.0.0"
