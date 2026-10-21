"""与操作系统打交道的接口（方案 5.1.7）。

actions / doctor 只通过 Platform 调用服务管理、TUN 冲突探测、DNS 接管等系统能力；
首版只有 Linux 实现，macOS 占位只为把接口形状固定下来。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional


class NotSupported(Exception):
    """当前平台没有实现该能力（退出码 6）。"""


class PlatformError(Exception):
    """系统命令执行失败；message 已包含失败的命令与底层原因。"""


@dataclass
class ServiceStatus:
    """systemctl show 的关键字段。active 的判据见方案 5.1.5「内核是否运行的判据」。"""

    load_state: str = "not-found"     # loaded | not-found | ...
    active_state: str = "inactive"    # active | activating | deactivating | reloading | inactive | failed
    sub_state: str = ""
    main_pid: int = 0
    n_restarts: int = 0
    unit_file_state: str = ""         # enabled | disabled | ...

    @property
    def installed(self) -> bool:
        return self.load_state == "loaded"

    @property
    def transitioning(self) -> bool:
        # ExecStartPre 期间 MainPID 仍为 0，不能据此判为“未运行”，一律当作“正在启动或停止”。
        return self.active_state in ("activating", "deactivating", "reloading")

    @property
    def running(self) -> bool:
        return self.active_state == "active" and self.main_pid != 0


@dataclass
class Check:
    """doctor 的一项结果。status: ok | warn | fail | skip。"""

    code: str
    title: str
    status: str
    detail: str = ""
    hint: str = ""
    fixable: bool = False
    fixed: Optional[bool] = None
    extra: dict = field(default_factory=dict)


class Platform(abc.ABC):
    name = "unknown"

    @abc.abstractmethod
    def core_asset_name(self, version: str) -> str:
        """内核下载文件名；version 带 v 前缀。"""

    @abc.abstractmethod
    def unit_text(self, home: str, user: str, python_exe: str) -> str:
        """当前版本应生成的服务单元全文（install 写入、doctor D02 比对共用）。"""

    @abc.abstractmethod
    def install_service(self, home: str, user: str, python_exe: str) -> None:
        """写入并启用后台服务（需要 sudo）。调用方必须先释放 state.lock。"""

    @abc.abstractmethod
    def uninstall_service(self) -> None:
        """停止、禁用并删除后台服务（需要 sudo）。"""

    @abc.abstractmethod
    def service_action(self, action: str) -> None:
        """start | stop | restart（需要 sudo）。调用方必须先释放 state.lock。"""

    @abc.abstractmethod
    def service_status(self) -> ServiceStatus:
        """只读查询，不需要 sudo。"""

    @abc.abstractmethod
    def service_logs(self, follow: bool, lines: int = 200) -> int:
        """把服务日志输出到终端，返回子进程退出码。"""

    def installed_unit_text(self) -> Optional[str]:
        """已安装的服务单元全文；未安装返回 None（doctor D02 比对用）。"""
        return None

    def service_journal(self, lines: int = 200) -> str:
        """本次开机以来服务日志的最后若干行（纯文本），doctor D13 找 preflight 锁超时用；取不到返回空串。"""
        return ""

    @abc.abstractmethod
    def sudo_validate(self) -> bool:
        """预先取得 sudo 凭据；失败（密码错 / 无权限）返回 False。"""

    @abc.abstractmethod
    def run_self_as_root(self, home: str, python_exe: str, subcommand: str, preserve_env: List[str]) -> int:
        """以 sudo 执行 `<python> -m mihomo_cli --home <home> <subcommand>`，返回退出码。"""

    @abc.abstractmethod
    def other_tun_instances(self) -> List[str]:
        """其它 TUN 网卡与 mihomo / clash / sing-box 进程的描述列表；空表示没有。"""

    @abc.abstractmethod
    def listening_conflicts(self, port: int, host: str = "127.0.0.1", own_listener: bool = False) -> List[str]:
        """端口被其它进程监听时返回占用者描述；本服务 MainPID 不算。

        own_listener 为真表示调用方已从本实例管理接口确认内核在听该端口，看不到属主的监听不再算占用。
        """

    def tun_conflicts(self) -> List[str]:
        """TUN 冲突口径：其它 TUN 实例 + 127.0.0.1:53 被占（方案 5.1.8）。tun on 与 preflight 共用。"""
        return self.other_tun_instances() + self.listening_conflicts(53)

    @abc.abstractmethod
    def tun_device_exists(self, name: str) -> bool:
        """网卡是否存在。"""

    @abc.abstractmethod
    def tun_supported(self) -> Optional[str]:
        """不支持 TUN 时返回原因，支持返回 None。"""

    @abc.abstractmethod
    def dns_takeover(self) -> str:
        """root 身份：把系统 DNS 指向 127.0.0.1:53，返回被接管的网卡名。"""

    @abc.abstractmethod
    def dns_revert(self) -> bool:
        """root 身份：还原 dns_takeover 留下的改动；什么也没做返回 False。"""

    @abc.abstractmethod
    def dns_status(self) -> dict:
        """只读：drop-in 是否存在、标记里的网卡、默认路由网卡及其 DNS（doctor D13 用）。"""
