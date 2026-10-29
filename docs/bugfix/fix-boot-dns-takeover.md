# 修复：开机时 TUN 接管系统 DNS 偶发失败且不重试

> **2026-10-29 落地注记**：代码已落地，本机与编译机回归通过（M-08 按用户决定不做）。验证位置：等默认路由 `src/mihomo_cli/actions.py:1673`（`_wait_default_iface`），接管后回读与重设 `:1697`（`_verify_iface_dns`），主流程 `:1727`（`tun_dns_apply`），无终端 sudo 提示 `:1252`（`sudo_error`）；`src/mihomo_cli/platform/linux.py:165`（`service_root_journal`）、`:193`（子进程超时 90 秒）、`:309`（`dns_relink`）；`src/mihomo_cli/doctor.py:315`、`:426`（D13 新提示）。本机 pytest 175 项与 ruff 通过；编译机（Python 3.8.10）用快照 e07d0dad…c826 在临时目录验证：M-09 显示新的无终端提示，M-13 按 root 过滤后剩 3 行且含 dns-apply 失败行，M-07 中 0.1.2 在 0.15 秒内失败，修复版在网络晚到 5 秒时等 4.6 秒后接管（总耗时 6.9 秒），晚到 60 秒时 25.3 秒后以新 ERROR 行收尾（退出 1），不晚到时 2.3 秒接管；M-10 收尾 doctor 14 项通过（D11 跳过），临时目录已删。真机开机后的效果待自然开机观察（§12）。
>
> 2026-10-29 注记（方案阶段）：问题来自编译机（Ubuntu 20.04，NetworkManager 1.22.10，systemd-resolved，WiFi 网卡 `wlp0s20f3`，mihomo-cli 0.1.2）开机后的排查。§13 三个问题用户已于当日决定：问题 1 选 A（NetworkManager 钩子）、问题 2 选 A（doctor 提醒 + README），均另开方案，不进本方案；问题 3 不做真机断网重启。

## 1. 背景

开机后 mihomo 内核、TUN 网卡 `mihomocli0`、`127.0.0.1:53` 都正常，但系统 DNS 仍是 WiFi 路由器 `192.168.0.1`，国外域名拿到的是污染地址（`www.google.com` → `104.244.42.197` / `2001::1`），走系统解析的程序上不了外网，`doctor` 只有 D13 失败。

装上 mihomo-cli 后编译机开机 3 次，失败 2 次（时间取自用户提供的 journal）：

| 开机 | WiFi 连好 | dns-apply 执行 | 结果 |
| ---- | ---- | ---- | ---- |
| 10-28 09:09 | 09:09:55 | 09:09:55 | 失败 no default route interface |
| 10-28 17:33 | 17:33:07 | 17:33:13 | 成功 takeover iface=wlp0s20f3 |
| 10-29 00:19 | 00:19:44.33 | 00:19:44 | 失败 no default route interface |

10-29 这次开机的关键日志（`journalctl -b -u mihomo-cli` 与 `-u NetworkManager`，2026-10-29 只读核对）：

```text
00:19:39-00:19:40 NetworkManager-wait-online.service（nm-online -s -q --timeout=30）运行并成功退出
00:19:40 NetworkManager: device (wlp0s20f3): state change: unavailable -> disconnected
00:19:40 mihomo: [TUN] default interface lost by monitor
00:19:44 python[1428]: [tun] dns-apply ERROR no default route interface
00:19:44 systemd: Started mihomo-cli managed mihomo kernel.
00:19:44.15 NetworkManager: wlp0s20f3 Activation: starting connection
00:19:44.33 NetworkManager: wlp0s20f3 Activation: successful, device activated.
00:19:45 mihomo: [TUN] default interface changed by monitor, => wlp0s20f3
```

## 2. 目标 / 非目标

目标：

1. 开机时 dns-apply 遇到“暂时没有默认路由”，在启动耗时预算内等待默认路由出现后再接管，不再一次失败就放弃。
2. 等满仍没有默认路由时，留下可 grep 的日志，`doctor` D13 能明确说出“开机时没等到网络，接管没做”以及怎么恢复。
3. 无终端（如 `ssh host mihomo-cli doctor --fix`）时 sudo 拿不到密码，提示说清原因和做法；`doctor --fix` 修 D13 失败时不再显示“已自动关闭 TUN 并还原”这句与 doctor 无关的话。

非目标：

- 网络在开机后很久才连上（超过等待上限）时的自动接管、WiFi 重连或 NetworkManager 改写网卡 DNS 后的自动重新接管：见 §13 问题 1，用户已决定另开功能方案。
- IPv6 绕开代理：见 §13 问题 2，用户已决定另开方案。
- 不处理 0.1.2 已有的“多条系统命令同时卡满各自 15 秒超时时启动超过 180 秒”的问题（量化见 §8.1 预算），本方案只保证自己新增的等待计入预算。
- 不改 systemd 单元文本（`After=` / `TimeoutStartSec` 等），不改 DNS 接管的方式本身。

## 3. 假设与约束

- systemd 单元的 ExecStartPost 调的是安装环境里的 `<python> -m mihomo_cli`（`src/mihomo_cli/platform/linux.py:77`、`:91-92`），所以本修复随包升级即生效，不需要重新 `install` 写单元。
- `TimeoutStartSec=180` 覆盖 ExecStartPre 到 ExecStartPost 全程（方案 `docs/feature/feature-mihomo-cli-v1.md` 5.1.7「启动耗时预算」）。新增等待必须算进这 180 秒。
- `default_iface()` 读的是主路由表 `ip -j route show default`（`linux.py:262-271`）。TUN 的 auto-route 走策略路由，不往主表加默认路由，所以“主表无默认路由”就等于物理网络还没就绪——这一点由本次日志佐证（TUN 网卡已在、主表仍无默认路由），mihomo 源码层面未核。

## 4. 根因分析

**现象**：开机后系统 DNS 未被接管，且此后一直不接管。

**触发条件**：mihomo-cli 服务的 `_tun-dns-apply` 执行时，物理网卡还没拿到默认路由（WiFi 尚未连上）。

**失效路径**：

1. 单元写的是 `After=network-online.target` + `Wants=network-online.target`（`linux.py:81-82`）。但 Ubuntu 20.04 上该目标由 `NetworkManager-wait-online.service`（`nm-online -s`）满足，它只等 NetworkManager“启动完成”。本次开机 00:19:40 WiFi 网卡刚进入 `disconnected`、还没开始连接，`nm-online` 就已成功退出（00:19:39-00:19:40），4 秒后 WiFi 才开始连接。所以 `network-online.target` 不保证有默认路由（本机日志实证；`nm-online -s` 的精确判定条件未读 NetworkManager 源码核对）。
2. `tun_dns_apply()` 只在 `DNS_APPLY_WAIT_SEC = 10` 秒内等两件事：TUN 网卡存在、`127.0.0.1:53` 可连（`src/mihomo_cli/actions.py:66-67`、`:1671-1677`），**不等默认路由**。
3. 两者就绪后立即调 `dns_takeover()`（`actions.py:1678`），它第一步取默认路由网卡，取不到就抛 `PlatformError("no default route interface")`（`linux.py:274-276`）。
4. 异常被 `actions.py:1681-1683` 捕获，打印 `[tun] dns-apply ERROR no default route interface`，退出码 1。
5. 单元里该行带 `-` 前缀（`linux.py:92` `ExecStartPost=-+...`），失败被忽略，服务照常报 Started。
6. 之后没有任何机制再调 `_tun-dns-apply`：全仓只有 `tun on`（`actions.py:1298`）、`tun off` 回滚路径（`actions.py:1353`）、`doctor --fix`（`doctor.py:327`）和单元的 ExecStartPost 会调它（`grep -n "_dns_apply(" src/mihomo_cli/*.py` 全集）。所以一次失败就一直失败到下次重启服务。

**错误代码点**：`src/mihomo_cli/actions.py:1671-1678`（等待条件缺默认路由）。

**附带问题（目标 3）**：

- `doctor --fix` 修 D13 先调 `sudo_validate()`（`doctor.py:324-325` → `linux.py:169-173`，即 `sudo -v`）。2026-10-29 在编译机上经 ssh（无 `-t`）实测：`sudo -v </dev/null` 退出码 1，输出 `sudo: a terminal is required to read the password`；`sudo -n true` 退出码 1（编译机 sudo 需要密码）。于是抛 `err.sudo_failed`（“需要 sudo 权限，但没有拿到。”，`i18n.py:106`），`cli.py:625` 显示 `[fix failed]`。用户问的原因（拿不到 sudo 密码）属实，但提示没说“因为没有终端”，也没说怎么做。同一提示还用在 `actions.py:719`、`:751`、`:1285`、`:1336`。
- `fix_d13` 在 dns-apply 失败时抛 `err.dns_apply_failed`（`doctor.py:327-328`），文案是“接管系统 DNS 失败，已自动关闭 TUN 并还原。”（`i18n.py:107`）。这句是给 `tun on` 回滚用的，doctor 并不会关 TUN，提示与事实不符。

## 5. 复现步骤

**真机自然复现**（用户提供，概率约 2/3）：编译机开机，WiFi 自动连接晚于 mihomo-cli 服务启动完成 → `journalctl -b -u mihomo-cli | grep dns-apply` 见 `ERROR no default route interface`，`mihomo-cli doctor` D13 报 `DNS of wlp0s20f3 is 192.168.0.1, not 127.0.0.1`（或“系统 DNS 没有被接管”）。

**不断网的确定复现**（编译机，root）：让 `_tun-dns-apply` 看到的 `ip` 命令在前 N 秒返回空的默认路由，其余调用转给真实 `ip`：

1. 在临时目录放一个假的 `ip` 脚本：参数为 `-j route show default` 且距首次调用不足 N 秒时输出 `[]`，否则 `exec /sbin/ip "$@"`。
2. `sudo env PATH=<临时目录>:$PATH <python> -m mihomo_cli --home ~/.local/share/mihomo-cli _tun-dns-apply`。
3. 0.1.2：N=5 时立即报 `ERROR no default route interface`、退出码 1（100% 复现）。

**单元测试复现**：假平台的 `default_iface()` 前几次返回 `None`，调 `actions.tun_dns_apply()`，0.1.2 返回 `EXIT_ERROR`。

**无终端 sudo**：`ssh <编译机> 'mihomo-cli doctor --fix'`（不带 `-t`、sudo 需要密码、D13 处于失败）→ D13 显示 `[fix failed]` 与“需要 sudo 权限，但没有拿到。”

## 6. 影响版本

- 0.1.0、0.1.1、0.1.2 全部受影响：`tun_dns_apply` 的等待条件从 0.1.0 起就是这样（`git log -L1671,1678:src/mihomo_cli/actions.py` 只有首发提交 268cb6c）。
- 触发需同时满足：Linux、TUN 开着、开机（或服务启动）时物理网络还没拿到默认路由。典型是 WiFi 自动连接的机器；有线网卡上电即拿到地址的机器很少触发。服务正常运行中执行 `tun on` / `service restart` 时网络早已就绪，不受影响。

## 7. 涉及模块

| 区域 | 行号锚点（main 205da2d） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/mihomo_cli/actions.py` 常量 | `:66-67` | 修改 | `DNS_APPLY_WAIT_SEC` 10 → 25，注释改为“等网卡、53 端口与默认路由”；新增 `DNS_NO_ROUTE_MARK = "no default route interface"`、`DNS_SETTLE_SEC = 2.0` |
| `src/mihomo_cli/actions.py` `tun_dns_apply` | `:1656`、`:1669-1679` | 修改 | 同一个 25 秒窗口里依次等 TUN 网卡、53 端口、默认路由；接管后等 2 秒回读网卡 DNS，被改回就重设网卡级 DNS 一次（失败只 WARN）；等不到默认路由时打可 grep 的 ERROR 并返回 `EXIT_ERROR` |
| `src/mihomo_cli/actions.py` sudo 失败 | `:719`、`:751`、`:1285`、`:1336` | 修改 | `CliError(EXIT_SUDO, t("err.sudo_failed"))` 改为 `raise sudo_error()` |
| `src/mihomo_cli/actions.py` 新增函数 | 邻近 `:1246`（`_preserve_env` 之后） | 新增 | `sudo_error()`：没有控制终端时返回带 `err.sudo_no_tty` 的 CliError，否则原提示 |
| `src/mihomo_cli/platform/linux.py` `run_self_as_root` | `:181` | 修改 | `timeout=60` → `timeout=90`（见 §10 正向第 1 条） |
| `src/mihomo_cli/platform/linux.py` `default_iface` | `:262-271` | 修改 | 新增可选参数 `timeout`，透传给 `_run`（默认仍是 `CMD_TIMEOUT_SEC`），让轮询那次调用不越过窗口 |
| `src/mihomo_cli/platform/linux.py` 新增方法 | 邻近 `:163`（`service_journal` 之后） | 新增 | `service_root_journal()`：`journalctl -u mihomo-cli -b _UID=0 -n 200 --no-pager -o cat`，只取以 root 身份运行的 ExecStartPost / ExecStopPost 输出 |
| `src/mihomo_cli/platform/base.py` | 邻近 `:131`（`dns_takeover` 的装饰器之前） | 新增 | 非抽象方法 `default_iface(timeout=None)` 返回 `None`、`service_root_journal()` 返回 `""`、`dns_relink(iface)` 抛 `NotSupported`（macOS 等不支持 TUN 的平台不用实现） |
| `src/mihomo_cli/platform/linux.py` 新增方法 | 邻近 `:296`（`dns_takeover` 之后） | 新增 | `dns_relink(iface)`：先核还原标记（`read_marker_iface()`，`:344`）记的是该网卡，否则抛 `PlatformError`；再执行 `resolvectl dns <iface> 127.0.0.1` 与 `resolvectl domain <iface> ~.`（与 `:293-294` 相同） |
| `src/mihomo_cli/doctor.py` `d13` | `:308-320` | 修改 | 系统 DNS 未接管时，读 `service_root_journal()` 最后一条 `[tun] dns-apply` 行，若含 `DNS_NO_ROUTE_MARK`，用新提示替换 `d13_no_dropin` |
| `src/mihomo_cli/doctor.py` `fix_d13` | `:323-328` | 修改 | sudo 失败改用 `actions.sudo_error()`；dns-apply 失败改用 `err.doctor_dns_apply_failed` |
| `src/mihomo_cli/i18n.py` | 邻近 `:106-107`、`:285-291` | 新增 | `err.sudo_no_tty`、`err.doctor_dns_apply_failed`、`doc.d13_no_route` 中英文案 |
| `tests/conftest.py` | 邻近 `:231-239` | 新增 / 修改 | 假平台补 `default_iface`、`service_root_journal`、`dns_relink`（记录调用），可设“前几次返回 None”与日志内容；`dns_status` 的默认返回改为接管网卡 `eth0`、`iface_dns` 含 `127.0.0.1`（现默认 `iface_dns=[]`，`:238-239`），旧用例走“正常”分支；M-11 各例另行配置 |
| `tests/test_actions.py`、`tests/test_linux.py` | 末尾追加 | 新增 | §11 M-01～M-05、M-11、M-12 |
| `docs/feature/feature-mihomo-cli-v1.md` | `:324` | 修改 | 启动耗时预算按 §8.1 重写 |
| `CHANGELOG.md` | 顶部 | 新增 | Unreleased 条目 |

## 8. 方案

### 8.1 实现要点

**1）dns-apply 等默认路由并回读**（替换 `actions.py:1671-1678`；`:1656` 的函数内导入一并导入 `CMD_TIMEOUT_SEC`（定义在 `linux.py:31`）；函数开头 `:1658-1668` 的 state 读取与“TUN 未生效则还原”逻辑不变，只给 `:1669` 的输出行加 `flush=True`，`:1679` 成功行同样加）：

```python
deadline = time.monotonic() + DNS_APPLY_WAIT_SEC
# 依次等 TUN 网卡、53 端口、默认路由，共用一个窗口。
# 默认路由：开机时 network-online.target 不保证 WiFi 已连上（fix-boot-dns-takeover.md §4），
# 没有默认路由就不知道该接管哪块网卡，必须等它出现，否则一次失败后再也不会重试。
```

- 网卡与 53 端口的等待逻辑不变，只是窗口从 10 秒变 25 秒。
- 之后循环调 `ctx.platform.default_iface(timeout=min(CMD_TIMEOUT_SEC, max(1, deadline - now)))`，拿不到就睡 0.5 秒，直到返回网卡名或窗口用完。循环先查后判断时间（do-while），即使网卡或 53 端口在窗口末尾才就绪，也至少查一次默认路由，与现有 53 端口等待的 `max(0.5, …)` 兜底（`actions.py:1674`）同一用意。第一次拿不到时打一行 `[tun] dns-apply waiting for default route (25s window)`（交互执行 `tun on` / `doctor --fix` 时用户也能看到在等什么）。
- 等到了且确实等过：打 `[tun] dns-apply default route ready iface=<网卡> waited=<秒>s`；一开始就有默认路由则不打。然后照旧 `dns_takeover()`。
- 窗口用完仍没有：stderr 打 `[tun] dns-apply ERROR no default route interface within 25s window; run 'mihomo-cli doctor --fix' once the network is up`，返回 `EXIT_ERROR`。
- **接管后回读**：等 `DNS_SETTLE_SEC`（2 秒）后调 `ctx.platform.dns_status()`（`linux.py:314`，已有方法，返回默认路由网卡与它的 DNS 列表；它内部吞掉命令错误，`default_iface` 出错得 None、`resolvectl` 出错得空列表，`linux.py:266-267`、`:318-323`）。按结果分三种：
  - 网卡 DNS 含 `127.0.0.1`：正常，不打日志。
  - 拿不到网卡或 DNS 列表为空：无法判断，打 `[tun] dns-apply WARN iface dns unknown after takeover`，不重试。
  - 回读到的默认网卡不等于第一次 `dns_takeover()` 返回的网卡：打 `[tun] dns-apply WARN iface changed <旧>-><新>, skip re-link`，不重设。原因：还原标记只记了第一次的网卡，`dns_revert` 只还原标记里的网卡（`linux.py:298-303`）；若重设新网卡，关 TUN 或停服务后它的 DNS 会一直指向已无人监听的 127.0.0.1，整机解析中断。
  - 同一块网卡、列表非空且不含 `127.0.0.1`：打 `[tun] dns-apply WARN iface dns overwritten (<列表>), re-linking <网卡>`，调新增的 `ctx.platform.dns_relink(<网卡>)` 只重做两条网卡级 `resolvectl`（`linux.py:293-294`）；`dns_relink` 先用 `read_marker_iface()`（`linux.py:344`）读还原标记，标记不存在或记的不是这块网卡就抛 `PlatformError("marker iface mismatch")`、不执行命令，走下面的 WARN 分支。原因：`tun on` 等满 90 秒超时后子进程可能还活着，回滚已把标记删掉、网卡还原，若此时再重设，留下的设置既无标记可还原、doctor 也查不出；不再重启 resolved——若覆盖正是 resolved 重启后 NetworkManager 重新下发引起的（未核实），再重启一次会把同一场竞争再触发一次。只重试这一次，之后不再回读。
  - 回读与重做放在单独的 `try` 里，捕获 `(PlatformError, NotSupported, OSError)`（`NotSupported` 与 `PlatformError` 互不继承，`base.py:14`、`:18`；外层只捕 `(PlatformError, OSError)`，`actions.py:1681`）：失败只打 `[tun] dns-apply WARN re-link failed: <原因>`，函数仍按第一次接管的结果返回 0。原因：第一次已接管成功，若把重做失败交给外层 `except`（`actions.py:1681-1683`）返回 `EXIT_ERROR`，`tun on` 会把已成功的接管整个回滚（`actions.py:1298-1316`），而且失败原因若是 `no default route interface`，doctor 还会误报成“启动时没等到网络”。doctor 判定看的是最后一条 dns-apply 行是否含 `DNS_NO_ROUTE_MARK`，所以 WARN 行里不得出现这段字样：原因文本含它时替换为 `no default route`。
  - 原因：开机时默认路由出现于 NetworkManager 激活网卡的过程中（本次 00:19:44.15 开始激活、.33 成功），NetworkManager 随后向 resolved 下发网卡 DNS 的时刻与我们接管的先后未核实，可能刚接管就被覆盖，症状与本 bug 相同。2 秒取值无源码依据，覆盖不了更晚的覆盖，更晚的由 §13 问题 1 的钩子兜底。
- **输出顺序**：`tun_dns_apply()` 里所有写标准输出的行（新增的 `waiting`、`ready`、WARN 行，以及原有成功行 `actions.py:1679`）一律 `print(..., flush=True)`；ERROR 行照旧写 stderr。原因：经 `run_self_as_root` 执行时标准输出不是终端，Python 会整块缓存到进程退出才写出，而 stderr 在 Python 3.9 起按行写出；不 flush 时 `waiting` 行会排到 ERROR 行之后，服务日志里最后一条 dns-apply 就不是失败行，doctor 的新提示失效。2026-10-29 本机 Python 3.11 实测：两路重定向到同一文件，不加 flush 时顺序为 `ERROR`、`waiting`，加 flush 后为 `waiting`、`ERROR`。编译机是 Python 3.8（stderr 也整块缓存），测不出这个差别，由 M-02 的单元测试兜住。
- `dns_takeover()` 内部仍保留原有“取不到默认路由就抛错”的兜底（`linux.py:274-276`，不改）。

**启动耗时预算**（改写 feature 方案 `:324`）：

- 只计主动等待（各条系统命令按正常秒级返回）：preflight ≤ 10 + reapply 等待就绪 ≤ 60 及其后重试共 ≤ 90 + dns-apply 窗口 ≤ 25 + 回读等待 2 + 接管与可能的一次网卡级重设的命令执行（正常合计约 2 秒）≈ 130 秒 < 180 秒。本方案相对 0.1.2 新增的最长等待是 15 + 2 = 17 秒。
- 各条系统命令卡满超时的情形（`_run` 每次 15 秒，`linux.py:31`）：0.1.2 已存在、本方案不处理。0.1.2 的 dns-apply 本身就有 `service_status` 15 + 等待 10 + `default_iface` 15 + 三条命令 45 = 85 秒，叠上 preflight 10 与 reapply 90（reapply 只在每次尝试开始前查预算，`actions.py:1611`，单次尝试还能再超出约 20 秒）已经超过 180 秒。原 feature 方案写的“dns-apply ≤ 25 秒、合计 125 秒”只算了命令合计 15 秒，与代码不符，本次一并改正。本方案的轮询调用超时受剩余窗口限制，不增加这部分；回读与重设最坏再加 `dns_status` 30 + 两条 `resolvectl` 30 秒。这些情形需要 systemctl、ip、resolvectl 多次各卡 15 秒，只会在 systemd 或 resolved 自身卡死时出现，三次开机日志里没有出现过。超时后 systemd 按启动失败处理并按 `Restart=on-failure` 重启（未实测）。
- 等待取 25 秒的依据：失败的两次开机里，WiFi 都在 dns-apply 失败的同一秒内连好（09:09:55、00:19:44），10-29 那次 mihomo 在 1 秒后（00:19:45）就看到了默认网卡；25 秒覆盖常见 WiFi 连接（含 DHCP），又给预算留出余量。更晚连网的情况见 §13 问题 1。

**2）doctor D13 提示**（`doctor.py:308-320`）：仅在 `not dns.get("dropin")`（系统 DNS 未接管）时，调新增的 `service_root_journal()`，找最后一条含 `[tun] dns-apply` 的行；若它含 `DNS_NO_ROUTE_MARK`（新旧两种失败行都含这段字样），把 `d13_no_dropin` 换成 `doc.d13_no_route`：

- 中文：“服务启动时一直没等到网络，系统 DNS 没有被接管；网络连上后运行 mihomo-cli doctor --fix”
- 英文：“the network was not up when the service started, so system DNS was not taken over; run mihomo-cli doctor --fix once the network is up”

文案不写秒数，旧版留下的失败行（没有等待）也适用。

- 为什么单独查、按 `_UID=0` 过滤：内核与 dns-apply 写在同一个单元日志里。2026-10-29 编译机实测，本次开机该单元 816 行，内核（`_UID=1000`）占 813 行，`-n 300` 的窗口里已经找不到 dns-apply 那一行；加 `_UID=0` 只剩 3 行（ExecStartPost 带 `+` 以 root 运行，`linux.py:92`），其中就有 `[tun] dns-apply ERROR no default route interface`。reapply 不带 `+`，以安装用户运行，不在其中。
- 只在“未接管”时看日志：手动以 root 跑 `_tun-dns-apply` 成功不会进服务日志，若不加此条件，接管已恢复后 doctor 仍会引用旧的失败行。
- 读不到日志（普通用户不在可读 journal 的组里，输出为空）时退回原提示。

**3）sudo 无终端提示**：新增 `actions.sudo_error()`，只在 `sudo_validate()` 已经返回失败之后调用（5 处调用点都是 `if not ...sudo_validate(): raise ...`）。判断方法：`os.open("/dev/tty", os.O_RDWR)` 抛 `OSError`（ssh 不带 `-t` 时没有控制终端）即视为没有终端，打开成功立即 `os.close`。sudo 读密码用的是终端而不是标准输入，所以不用 `sys.stdin.isatty()`。用户配置了 `SUDO_ASKPASS` 等不靠终端取密码的方式时，`sudo -v` 不会因为没终端而失败，也就不会走到这里（sudo 默认不自动用 askpass，未核实各发行版配置）。文案 `err.sudo_no_tty`：

- 中文：“需要输入 sudo 密码，但当前没有终端（例如用 ssh 直接执行命令）。请用 ssh -t 登录后再运行，或在终端里先执行 sudo -v。”
- 英文：“a sudo password is needed but there is no terminal to type it (e.g. a command run directly over ssh). Run it in an ssh -t session, or run sudo -v in a terminal first.”

**4）doctor 修 D13 失败的提示**：`fix_d13` 改用 `err.doctor_dns_apply_failed`：“重新接管系统 DNS 失败，原因见上方以 [tun] 开头的输出。” / “Failed to take over system DNS again; see the [tun] lines above.”（`_tun-dns-apply` 由 `subprocess.call` 直接输出到终端，`linux.py:181`。）

### 8.2 接口变更

- 无命令、参数、配置字段、state 字段、systemd 单元变更。平台类新增 `service_root_journal()`、`default_iface()` 加可选参数，均为包内接口。
- 日志：新增 `[tun] dns-apply waiting for default route (25s window)`、`[tun] dns-apply default route ready iface=... waited=...s`、`[tun] dns-apply WARN iface dns unknown after takeover`、`[tun] dns-apply WARN iface changed <旧>-><新>, skip re-link`、`[tun] dns-apply WARN iface dns overwritten (...), re-linking ...`、`[tun] dns-apply WARN re-link failed: ...`；失败行由 `[tun] dns-apply ERROR no default route interface` 变为 `[tun] dns-apply ERROR no default route interface within 25s window; run ...`，保留原字样前缀，按旧字样 grep 仍能命中。
- `doctor` D13 的 detail 新增一种文案；`--json` 输出结构不变。
- 退出码不变：sudo 失败仍是 `EXIT_SUDO`（5），只换 message。
- 本方案不涉及 `docs/reference/*`。

## 9. 备选方案与决策

- **加长 `After=` / 依赖 `NetworkManager-wait-online` 的配置**：§4 已证明 `network-online.target` 不保证有默认路由；改单元还要求老用户重新 `install`。否决。
- **dns-apply 失败时改为非 `-` 前缀让服务失败重启**：网络没好时服务会反复重启、内核反复拉起，`StartLimitBurst=5` 很快耗尽，反而把代理整个停掉。否决。
- **等待放进 `dns_takeover()` 内部**：它也被 `tun on`、`doctor --fix` 调用，等待逻辑放在 `tun_dns_apply()` 与已有的网卡、53 端口等待共用一个窗口更清楚。否决。

## 10. 影响分析

**正向**：

- `tun_dns_apply()` 被三处调用（`cli.py:581-584` 隐藏命令 ← 单元 ExecStartPost `linux.py:92`、`tun on` `actions.py:1298`、`doctor --fix` `doctor.py:327`，均经 `run_self_as_root` `linux.py:175-185`）。
  - 单元路径：网络正常时默认路由早已存在，第一次查询就拿到；每次接管后多 2 秒回读。网络迟到时最多多等 15 秒（窗口 10 → 25），服务报 Started 相应推迟，主动等待合计约 130 秒 < 180 秒（§8.1 预算）。ExecStartPost 按顺序执行，等待发生在 reapply 之后，不影响 reapply；`service start/restart` 的 systemctl 超时是 200 秒（`linux.py:131`），不会因此误判失败。
  - `tun on` / `doctor --fix`：经 `run_self_as_root`，`subprocess.call(..., timeout=60)`（`linux.py:181`）。修复后正常路径约 2～5 秒；没联网时 25 秒等满即返回，不执行接管命令；网卡或 53 端口临近窗口末才就绪时，之后还要接管 + 回读 + 可能的重设，正常 ≈ 25 + 2 + 2 = 29 秒，余量只有约 31 秒，约两条及以上命令卡满 15 秒就会触到 60 秒，被掐断后 `tun on` 误判失败并回滚。把超时改为 90 秒，可再容纳约 4 条命令卡满。全部命令都卡满的情形 0.1.2 也超 60 秒（85 秒），属同类既有风险。该超时也用于 `_tun-dns-revert`（最坏 `resolvectl revert` + 重启 resolved = 30 秒），放宽无影响。
  - `tun on` 时 dns-apply 失败会自动关 TUN 回滚（`actions.py:1298-1316`）。没联网时执行 `tun on`，0.1.2 立即失败回滚，修复后先显示“正在等默认路由”，25 秒后失败回滚，结论相同。
- `sudo_error()` 替换 5 处提示，只换文案、退出码不变；`--json` 下 message 字段文案变化。
- D13 新提示只在“系统 DNS 未接管”时触发，不影响 OK 判定与 `fixable`；doctor 多一次 `journalctl` 调用（只在未接管时）。

**反向**：

- `default_iface()` 无副作用（只读 `ip route`），多调几次无影响；新增的 `timeout` 参数有默认值，`dns_status()`（`linux.py:314-315`）与 `dns_takeover()`（`linux.py:274`）的调用不变。
- 回读用的 `dns_status()` 只读；重设只在回读网卡等于第一次接管的网卡时进行，只改该网卡的两项设置，不重启 resolved；该网卡已由第一次接管写进还原标记（`linux.py:277-286`），`dns_revert` 照常能还原它。网卡在 2 秒内变了就不重设（新网卡未接管，由全局 drop-in 兜底，D13 会报出）。此后用户若跑 `doctor --fix`，会以新网卡重新接管并把还原标记覆盖成新网卡，旧网卡上的 127.0.0.1 设置就不会再被还原——这是 0.1.2 已有的行为（`linux.py:277-286` 每次接管都覆盖标记），本方案不改，列为已知限制；旧网卡断开后其设置随之失效，仍连着时手动 `resolvectl revert <旧网卡>` 即可恢复。重设失败不影响返回值，`tun on` 不会因此回滚。
- 服务日志、`dns.state`、drop-in 的读写方不变。
- `base.py` 新增非抽象方法，macOS 平台（`platform/darwin.py`）与测试假平台不实现也能用。

**运行时**：无新进程、新线程；等待期每 0.5 秒一次 `ip -j route show default`，最多约 50 次；每次接管多 1 次 `ip` + 1 次 `resolvectl dns` 读取。部署形态：使用 systemd-networkd、有线网的机器同样受益（只依赖主路由表）；回读对它们无害。

**验证覆盖不到的风险**：§5 的假 `ip` 复现时网络早已就绪，复现不了“刚接管就被 NetworkManager 覆盖”的竞争；回读逻辑只在单元测试里用假平台覆盖（M-11），真机效果要等以后自然开机观察（§12 日志里出现 `iface dns overwritten` 即发生过）。重设后 NetworkManager 是否还会再覆盖一次，未核实。

## 11. 回归测试

单元测试的共同前置：假平台 `dns_status` 回读默认返回含 `127.0.0.1`（M-11 另行配置）；state 里 TUN 生效（`tun_effective_in_config` 为真）且假平台 `service_status().main_pid` 非 0（`actions.py:1664-1665`）；`mihomo_cli.platform.linux.wait_port` 打桩为立即返回真（`actions.py:1656` 在函数内导入，需打桩模块属性）；`time.sleep` 与 `time.monotonic` 打桩为假时钟，不真等。

| 编号 | 环境 | 内容 | 判据 |
| ---- | ---- | ---- | ---- |
| M-01 | 本机单元测试 | 假平台 `default_iface()` 前 3 次返回 None、之后返回 `eth0`，调 `tun_dns_apply` | 返回 0，调用了 `dns_takeover`，输出含 `waiting for default route` 与 `default route ready iface=eth0` |
| M-02 | 本机单元测试 | `default_iface()` 一直返回 None；另一例让 53 端口就绪时假时钟已越过窗口末尾、此时默认路由已在 | 前者返回 `EXIT_ERROR`，未调 `dns_takeover`，stderr 含 `no default route interface within 25s window`；另核 `tun_dns_apply` 内每次写标准输出的 `print` 都带 `flush=True`（打桩 `print` 记录参数）；后者仍查到默认路由、接管成功返回 0 |
| M-03 | 本机单元测试 | 默认路由一开始就在，回读含 127.0.0.1 | 不打 `waiting` / `ready` 行，`dns_takeover` 只调一次 |
| M-11 | 本机单元测试 | 回读结果分别为：`["192.168.0.1"]`；`["192.168.0.1"]` 且 `dns_relink` 抛 `PlatformError("no default route interface")`；`iface_dns=[]`；`default_iface=None` | 第一种打 `overwritten`、`dns_relink` 调一次、`dns_takeover` 只调一次、返回 0；第二种打 `re-link failed`、返回 0、输出里最后一条 dns-apply 行不含 `no default route interface`；后两种打 `unknown`、不调 `dns_relink`、返回 0；再加一例：标记记的是 `wlan1` 而回读网卡为 `eth0`，`LinuxPlatform.dns_relink` 抛 `PlatformError` 且不执行 `resolvectl`（打桩 `_run` 核对）；另加一例：回读默认网卡为 `wlan1`（接管返回 `eth0`）且 DNS 不含 127.0.0.1 → 打 `iface changed`、不调 `dns_relink`、返回 0 |
| M-04 | 本机单元测试 | D13：drop-in 不存在且 root 日志最后一条 dns-apply 为新格式失败行 / 旧格式失败行 → 新提示；最后一条是 takeover → 原提示；root 日志为空 → 原提示；drop-in 存在 → 不调 `service_root_journal` | 五种情况文案正确 |
| M-05 | 本机单元测试 | `sudo_error()`：打桩 `os.open` 抛 `OSError` → `err.sudo_no_tty`；返回 fd → `err.sudo_failed` 且 fd 被关闭；退出码都是 5 | 通过 |
| M-12 | 本机单元测试 | `LinuxPlatform.run_self_as_root` 传给 `subprocess.call` 的 `timeout` 为 90；`tun_on` 在 `root_rc["_tun-dns-apply"]=1` 时仍回滚（复用 `tests/test_actions.py:239-247`） | 通过 |
| M-06 | 本机 | `ruff` 与全量测试 | 全部通过 |
| M-09 | 编译机（先于 M-07，此时 D13 仍是失败现状） | `ssh <编译机> '<临时环境>/bin/python -m mihomo_cli --lang en --home ~/.local/share/mihomo-cli doctor --fix'`（无 `-t`） | D13 显示 `[fix failed]` 与 `a sudo password is needed but there is no terminal` 一行；系统状态未变 |
| M-13 | 编译机 | `journalctl -u mihomo-cli -b _UID=0 -n 200 --no-pager -o cat` 以安装用户执行 | 输出含本次开机的 `[tun] dns-apply` 行，不含内核日志 |
| M-07 | 编译机，临时环境（`/home/unitree/tmp/<任务>-<时间戳>/` 下用快照建 venv，不动已装的 0.1.2） | §5 假 `ip` 复现：N=5 秒；先用已装 0.1.2 跑一次，再用临时环境跑一次；再把 N 设为 60 用临时环境跑；最后 N=0 用临时环境跑一次 | 0.1.2 立即 `ERROR no default route interface`；修复版约 5 秒后 `default route ready` + `takeover iface=wlp0s20f3` 退出 0；N=60 时约 25 秒后打新 ERROR 行退出 1，且输出里 ERROR 行是最后一条 dns-apply 行；最后一次接管成功，doctor 14 项通过 |
| M-08 | 编译机真机开机 | 不执行（§13 问题 3 用户决定不做） | — |
| M-10 | 编译机 | 收尾：已装 0.1.2 的服务未被改动，`systemctl is-active mihomo-cli`、`mihomo-cli doctor` 14 项通过，临时目录已删 | 通过 |

## 12. 日志 / 观测点

- `journalctl -b -u mihomo-cli _UID=0 -o cat | grep 'dns-apply'`：成功时 `takeover iface=...`；网络迟到时前面多 `waiting for default route` 与 `default route ready iface=... waited=...s`；接管后被改回时有 `WARN iface dns overwritten`；2 秒内默认网卡变了时有 `WARN iface changed`；回读或重设出错时有 `WARN re-link failed`；等满失败时 `ERROR no default route interface within 25s window`。
- `mihomo-cli doctor`：D13 detail 的新提示。
- `resolvectl status <网卡>`：网卡 DNS 应为 `127.0.0.1`；`/run/systemd/resolved.conf.d/mihomo-cli.conf` 与 `/run/mihomo-cli/dns.state` 存在。
- `getent ahostsv4 www.google.com` 返回 `198.18.x.x`。

## 13. 开放问题（2026-10-29 用户已决定，均不进本方案）

### 问题 1：WiFi 重连、换 WiFi、NetworkManager 改配置后，接管会被冲掉

**已核实的事实**（2026-10-29 编译机只读核查）：

- 用户 00:32:57 手动接管成功（drop-in 与 `dns.state` 的修改时间都是 00:32:57）。
- 00:33:28 用户用 `nmcli` 修改了全部 WiFi 连接的 IPv6 设置（NetworkManager 审计日志 `op="connection-update" ... args="ipv6.method,..."`）。此后 WiFi **没有断开重连，也没有 DHCP 续租**（租期 86399 秒，NetworkManager 日志在 00:20 之后无激活、断开记录）。
- 02:37 再查：全局 DNS 仍是 `127.0.0.1`、`~.`（drop-in 还在），但 `wlp0s20f3` 的网卡级 DNS 已变回 `192.168.0.1`、域 `~.`；`doctor` D13 报 `DNS of wlp0s20f3 is 192.168.0.1, not 127.0.0.1`。`getent ahostsv4 www.google.com` 当时返回 `198.18.0.4`（代理的地址），说明解析仍大多走 mihomo。
- 编译机 `/etc/NetworkManager/dispatcher.d/` 只有系统自带的 `01-ifupdown`、`20-chrony`，没有任何重新接管的钩子。

**结论与未验证项**：

- 网卡级设置会被 NetworkManager 重新下发覆盖；不只是重连，改连接配置也会触发（具体是哪一下触发的、NetworkManager 在哪些事件后重新下发，未读源码核对）。WiFi 断开重连、切换到另一个 WiFi 会不会同样覆盖：按同一机制推断会，**未实测**（即待办里的 R-15b）。
- 覆盖后全局 drop-in 仍生效，但网卡也带着 `~.` 指向路由器。systemd-resolved 手册（systemd-resolved.service(8)「Protocols and Routing」）说一个查询同时匹配多处时会并发发出、取第一个成功的回答，所以国外域名可能偶尔拿到路由器给的污染地址。**本机未实测发生频率**。
- 切换到另一块网卡（如插网线后默认路由换成有线网卡）时，新网卡从未被接管，情况同上。

**可选做法**：

| 选项 | 做法 | 好处 | 代价 |
| ---- | ---- | ---- | ---- |
| A | `install` 时额外写一个 NetworkManager 钩子，网络连上、DHCP 变化等事件后自动重新接管 | 也兜住“开机 25 秒后才连上网” | 只对 NetworkManager 管网的机器有效；改配置那种情况会不会触发钩子未验证；`uninstall` 要一并删除 |
| B | 服务里加一个每分钟检查一次的定时任务，发现被改回就重新接管 | 不管什么原因被改都能补回 | 常驻定时任务，最长约 1 分钟的空窗 |
| C | 不自动处理，D13 失败时提示运行 `doctor --fix` | 不加新机制 | 每次重连都要人工修 |

建议：A，另开功能方案（涉及 `install` / `uninstall` 新增系统文件，按功能单独评审）；本修复先落地。

**用户决定（2026-10-29）**：选 A，另开功能方案。

### 问题 2：随身 WiFi 给公网 IPv6 时，浏览器绕开代理

**已核实的事实**：mihomo-cli 生成的 TUN / DNS 配置里没有任何 `ipv6` 相关项（`src/mihomo_cli/render.py:164-182`）。用户已在系统侧关掉全部 WiFi 的 IPv6（备份 `/etc/NetworkManager/system-connections.bak.20261029_003328`），本方案不改回。

**未验证**：mihomo 在未配 `ipv6` 时 TUN 是否完全不接管 IPv6 路由（用户观察如此，未读 mihomo 源码核对）；Chrome 安全 DNS 拿到 IPv6 地址后直连的路径是用户现场观察，未复现。

**可选做法**：

| 选项 | 做法 | 好处 | 代价 |
| ---- | ---- | ---- | ---- |
| A | `doctor` 新增一项：TUN 开着且系统有公网 IPv6 默认路由时报警告，提示关 IPv6 或关浏览器安全 DNS | 改动小，只提示不改系统 | 不自动解决 |
| B | 让 TUN 同时接管 IPv6 | 根本解决 | 要核 mihomo 的 IPv6 配置项与节点是否支持 IPv6 出站，改动和验证都大 |
| C | 不处理，只在 README 常见问题里写一段 | 零代码 | 用户自己排查 |

建议：A + README 写一段，另开小方案。

**用户决定（2026-10-29）**：选 A + README，另开方案。

### 问题 3：真机开机复现要不要做

需要把编译机的 WiFi 自动连接推迟（或拔掉随身 WiFi）再重启编译机，会断网约 1～2 分钟，且 WiFi 连不回来时需要有人到机器前。默认不做，用 §5 的假 `ip` 方式在不断网的情况下等效复现；用户同意时再做 M-08。

**用户决定（2026-10-29）**：不做，M-08 记为不执行。
