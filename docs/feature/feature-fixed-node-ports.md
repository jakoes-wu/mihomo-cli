# 固定节点端口：每个本机端口固定走一个节点

> 2026-11-03 注记：方案已与用户确认要点（节点单独存放、备份恢复一起恢复、status 一行汇总、命令 add / list / rm + 菜单入口），代码待落地。基线：main `7b6f56f`（0.1.3）。内核源码核对基于 mihomo `v1.19.32`（编译机所装版本，tag 提交 `88dcbf7`），文中内核源码行号均指该版本。

> 2026-11-04 注记：代码已落地（`state.py` `_validate_ports`、`render.py` pin provider / PORT 组 / listeners、`actions.py` `_verify_ports` 与 port_add / port_list / port_rm / `_restore_ports_precheck`、`doctor.py` `d16`、`cli.py` port 组、`menu.py` `_port_menu`）。L0：本机 pytest 221 个通过（`tests/test_ports.py`）。L1：编译机一次性容器 + 真内核 v1.19.32，P-10～P-16 全部通过（P-15 用 v1.19.26 / v1.19.27 对照）。P-17（L2）待发版后在编译机执行。代码评审追加的收紧：单端口只接受一条链接（多条报 `err.port_one_link`）；state 里 `ports:` 为空值按 `[]` 处理。

## 1. 背景

用户要求：在本机多开几个代理端口，每个端口固定走一个指定节点，与当前选中的节点、全局 / 规则 / 直连模式都无关；编译机上开 7901～7909 九个端口，端口与节点的对应关系与 macOS 上 Clash Verge 全局扩展脚本（`ADS_LISTENERS`）一致，只监听 `127.0.0.1`，类型 mixed（HTTP + SOCKS5），开 UDP。

现状（0.1.3）：

- `render()` 只写一个 `mixed-port`，不写 `listeners`（`src/mihomo_cli/render.py:83-183`）；`config.yaml` 每次由 state 重新生成，手改会被覆盖，doctor D04 也会报不一致（`src/mihomo_cli/doctor.py:147-158`）。
- 另起一个独立 mihomo 不可行：`other_tun_instances()` 把本机任何其它 mihomo 进程都算冲突（`docs/feature/feature-mihomo-cli-v1.md:284`），下次重启时 TUN 被暂停。必须在同一个内核里实现。

内核行为核对（mihomo v1.19.32）：

| 编号 | 事实 | 源码位置 |
| ---- | ---- | ---- |
| K1 | 监听端口带 `proxy` 时，连接直接用该出口，在模式分支之前返回，与 global / rule / direct 无关 | `tunnel/tunnel.go:318-324`（模式分支在 `:401-409`） |
| K2 | `proxy` 只在 `proxies` 表里查；该表只含内置出口、顶层 `proxies` 与策略组，不含 provider 里的节点；查不到时该连接报 `proxy %s not found` 失败 | `config/config.go:885-990`（parseProxies）、`tunnel/tunnel.go:320-322` |
| K3 | 组同时写 `proxies` 与 `use` 时，`proxies` 成员被放到最前；select 组未选过时取第一个成员。照搬 PROXY 组的“`proxies: [REJECT]` + `use`”写法，端口默认就是 REJECT | `adapter/outboundgroup/parser.go:155-184`、`selector.go:103-112` |
| K4 | 组成员为空时回落到 `empty-fallback`，默认 `COMPATIBLE`（即直连）；可设为 `REJECT`；`empty-fallback` 不能指向策略组 | `parser.go:74-83`、`groupbase.go:226-228`、`adapter/outbound/direct.go:96-104` |
| K5 | `empty-fallback` 从 v1.19.27 起才有（v1.19.26 的 parser.go 无该字段，v1.19.27 有）；旧内核的组解析按结构体字段取值，未知键被静默忽略，组空时回落直连 | v1.19.26 / v1.19.27 `adapter/outboundgroup/parser.go` 对照；`common/structure/structure.go:480-530`（按结构体字段逐个到配置里查键，配置里多出的键不会被访问） |
| K6 | 监听端口绑定失败只记日志 `Listener %s listen err`，不报错、不中断配置加载；失败的监听不记入表，下次重载会重试 | `listener/listener.go:639-642` |
| K7 | 重载时配置没变的监听端口原样保留，不关闭重开；但每次重载都会重建全部 provider，`Initial` 会关闭经过该 provider 节点的已建连接，重载窗口内 tunnel 暂停、新连接被关 | `listener/listener.go:631-636`、`hub/executor/executor.go:89-119,186-187`、`adapter/provider/provider.go:152-165`、`tunnel/tunnel.go:270-273` |
| K10 | 内核运行期间 provider 文件被改坏 / 改空：文件监视回调解析失败只记日志，保留旧节点；provider 初始化（重载 / 重启）时解析失败也只记日志（`initial proxy provider ... error`），配置照常生效，组为空 | `component/resource/fetcher.go:116-122,147-151,236-240`、`hub/executor/executor.go:327-331` |
| K11 | `mihomo -t` 只解析配置、不加载 provider，查不出坏链接 | `main.go:175-183` 只调 `executor.Parse` → `hub/executor/executor.go:79-81` 即 `config.Parse`；provider 的 `Initial` 只在 ApplyConfig 的 `loadProvider`（`executor.go:115,318-331`）里调用 |
| K8 | 管理接口没有列出监听端口的路由（`hub/route/` 下无 listeners 接口），`GET /configs` 只有 `mixed-port` 等通用端口 | `hub/route/`、`hub/route/configs.go` |
| K9 | mixed 监听的字段：`name`、`listen`、`port`、`proxy`、`udp` | `listener/inbound/base.go:93-97`、`listener/inbound/mixed.go:18` |

## 2. 目标 / 非目标

目标：

1. 新增“固定节点端口”：每个端口绑定一条节点链接，节点单独存放、不属于任何代理服务；切换 / 删除代理服务、切节点、切模式都不影响这些端口。
2. 绑定的节点不存在或无法解析时，该端口断网（REJECT），绝不直连。
3. 命令 `port add / list / rm`（支持从文件批量导入、按起始端口依次分配），菜单入口，status 一行汇总，doctor 新增检查，备份恢复一并恢复端口设置。

非目标：

- 引用代理服务里的节点名（用户已选“单独存放”）。
- 端口监听局域网地址、端口认证、端口类型可选（固定 mixed + `127.0.0.1` + UDP）。
- 修改节点的“换节点”命令（换节点 = 先 `rm` 再 `add`）。
- macOS 平台实现（沿用现有平台抽象，不新增平台方法）。

## 3. 假设与约束

- 内核版本 ≥ v1.19.27（K5）。编译机为 v1.19.32，满足。全局最低版本 `MIN_CORE_VERSION`（`src/mihomo_cli/core_dl.py:23`，1.19.0）不变；只有配置了固定端口时才要求 ≥ 1.19.27。
- 不在 Python 里解析代理协议：链接原样 base64 写入节点文件，由内核解析，名字从管理接口回读（与手工节点同一规则，`src/mihomo_cli/nodes.py:1-5`）。
- 链接含凭据：不进日志、不在 `port list` 里明文显示（只显示 `link_summary`，`src/mihomo_cli/nodes.py:89-100`）。
- state.yaml 版本号保持 1：新增字段由 `_validate` 的默认值合并兼容旧文件（`src/mihomo_cli/state.py:112-113`）。代价：退回旧版 mihomo-cli 时旧版会忽略该字段、下次写配置时端口消失，但服务照常运行；若升版本号，旧版会直接拒读 state，服务起不来。

## 4. 涉及模块

| 区域 | 行号锚点（main 7b6f56f） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/mihomo_cli/state.py` 默认状态 | `:75-87` | 修改 | 新增 `"ports": []` |
| `src/mihomo_cli/state.py` `_validate` | `:105-132`，在 `:130` 之前 | 修改 | 校验并规范 `ports` |
| `src/mihomo_cli/state.py` `Paths` | `:61-62` 之后 | 新增 | `port_file(port)` |
| `src/mihomo_cli/core_dl.py` | `:23` 之后 | 新增 | `PORTS_MIN_CORE_VERSION = (1, 19, 27)` |
| `src/mihomo_cli/render.py` | `:131` 之前、`:151` 之后、`:162` 之后 | 修改 | 写 pin provider、PORT 组、`listeners` |
| `src/mihomo_cli/actions.py` `port_conflicts` / `kernel_listens_on` | `:220-243` | 修改 | 两者加可选参数 `state=None`；新增 helper `port_listening(ctx, P, state)`（§5.1.4） |
| `src/mihomo_cli/actions.py` `reload` / `commit` | `:425-508`（提交段 `:485-490`）、`:606-622` | 修改 | 新增关键字参数 `ports_involved`；回读固定端口节点名 |
| `src/mihomo_cli/actions.py` `_verify` | `:511-557`，在 `:555` 之前 | 修改 | 逐个核验固定端口 |
| `src/mihomo_cli/actions.py` `status` | `:643-678` | 修改 | 增加 `ports` 汇总 |
| `src/mihomo_cli/actions.py` `install` | `:697-701` | 修改 | 新混合端口不得与固定端口重合 |
| `src/mihomo_cli/actions.py` `core_upgrade` | `:768-791` | 修改 | 有固定端口时拒绝装 < 1.19.27 的内核 |
| `src/mihomo_cli/actions.py` `backup_restore` | `:1491` | 修改 | 恢复键加 `ports`，恢复前查占用 |
| `src/mihomo_cli/actions.py` 新函数 | `node_restore` 之后（`:1158` 邻近）新增一节 | 新增 | `port_list` / `port_add` / `port_rm` |
| `src/mihomo_cli/doctor.py` | `:194` 之前新增 `d16`；`CHECK_ORDER` `:363` | 新增 / 修改 | D16 固定节点端口 |
| `src/mihomo_cli/cli.py` | node 组之后（`:389` 邻近）；`_print_status` `:598-618` | 新增 / 修改 | `port` 命令组；status 一行 |
| `src/mihomo_cli/menu.py` | `run_menu` `:60-78`；新增 `_port_menu` | 修改 / 新增 | 菜单入口 |
| `src/mihomo_cli/i18n.py` | 各分组末尾；`help.doctor`（`:434`）；`doc.D01…`（`:247` 起） | 新增 / 修改 | 中英文案；`help.doctor` 的“自检 15 项 / Run 15 checks”改为 16；新增 `doc.D16` |
| `tests/conftest.py` | `FakeKernel`（`:40` 起）、假内核版本 `:26-29` | 修改 | 支持 pin provider / PORT 组；版本可切换 |
| `tests/test_ports.py` | 新文件 | 新增 | P-01～P-09、P-18 的 L0 用例；帮助与文案键由 `tests/test_cli_help.py` 现有扫描自动覆盖 |
| `README.md` / `README.zh-CN.md` | 命令表 `:101-118` / `:96-113`；doctor 项数 `:117,152` / `:112,146` | 修改 | 新命令、16 项自检、新常见问题一段 |
| `docs/feature/feature-mihomo-cli-v1.md` | §5.1.6 命令清单、§5.1.10 doctor 表 | 修改 | 追加 `port` 命令与 D16，链回本方案 |
| `CHANGELOG.md` | 顶部 | 新增 | 新版本条目 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 state 结构

`state["ports"]`：按端口升序的列表，每项 `{"port": int, "link": str, "name": Optional[str]}`。`name` 与手工节点同一规则：只在重载核验通过后从接口回读；离线写入时为 `None`。

`_validate`（`state.py:130` 之前插入）：

- `ports` 不是列表 → `StateError`；
- 每项 `port` 为 1～65535 的整数、互不重复、不等于 `mixed_port`；`link` 为含 `://` 的字符串；
- 规范为 `[{"port", "link", "name"}]` 并按 `port` 排序。

节点文件：`Paths.port_file(port)` = `nodes/_port-<端口>.txt`，内容用 `nodes_mod.encode_manual_file([link])`（单行 base64，`nodes.py:26-33`）。代理服务名必须以字母数字开头（`state.py:25`），`_port-` 前缀不会与 `nodes/<代理服务名>.txt` 重名。放在 `nodes/` 下，`make_backup` 现有逻辑（`actions.py:294-296`，复制 `nodes/*.txt`）自动把它备份。

#### 5.1.2 生成的配置

每个端口 P 生成三样，名字固定由端口号派生：

```yaml
proxy-providers:
  pin-7901:
    type: file
    path: ./nodes/_port-7901.txt
proxy-groups:
  - name: PORT-7901
    type: select
    use: [pin-7901]
    empty-fallback: REJECT
listeners:
  - name: port-7901
    type: mixed
    listen: 127.0.0.1
    port: 7901
    udp: true
    proxy: PORT-7901
```

- 插入位置：pin provider 在 `render.py:131`（`if providers:`）之前追加到 `providers`，**不放在 `if prof is not None` 块内**——没有当前代理服务时固定端口也要生效；PORT 组追加在 GLOBAL（`render.py:150`）之后、测试注入（`:152`）之前；`listeners` 在 `rules`（`:156-162`）之后，仅 `ports` 非空时写，保证没有固定端口的用户生成的配置与 0.1.3 逐字相同（D04 不受升级影响）。
- 为什么一个端口一个 provider：组只装这一个 provider 的节点，不需要按名字过滤，同名节点（如 ads9 订阅里也有 `ads-lisa-90`）不会互相干扰。
- 为什么不写 `proxies: [REJECT]`：K3，会默认选中 REJECT。改用 `empty-fallback: REJECT`（K4）：重载或重启时链接无法解析、文件缺失，组为空，回落 REJECT 断网。内核运行中文件被改坏时热加载失败、保留旧节点（K10），同样不会直连，到下一次重载或重启才变为断网。
- pin provider 不写 `health-check`、不加前缀；PROXY 组 `use` 仍只列 `provider_names(state)`（`render.py:51-61`），pin provider 不会混进 PROXY；GLOBAL 组显式只含 PROXY（`render.py:150`），PORT 组不出现在全局模式里。
- `render()` 仍只读 state（`render.py:1-5` 约束）。内核版本不由 render 判断，由 §5.1.5 的命令侧与 D16 把关。

#### 5.1.3 重载核验与名字回读

`_verify`（在 `actions.py:555` 之前插入），对 `new_state["ports"]` 每一项：

`reload` / `commit` 新增关键字参数 `ports_involved: Tuple[int, ...] = ()`，只有 `port add`（新增的端口）与 `backup restore`（恢复后新增或链接有变的端口）传入。

1. `api.provider_node_names("pin-<P>")` 数量必须为 1：
   - P 在 `ports_involved` 中：不为 1 → `CliError(EXIT_ROLLED_BACK, t("err.port_unparsable"), detail="port P nodes N; link: <link_summary>")`，进入现有回滚（`actions.py:560-603`）；
   - P 不在 `ports_involved` 中（与本次动作无关的已有端口）：不为 1 只 `ctx.say("warn", t("warn.port_blocked", port=P))`，不回滚。理由：回滚恢复的旧配置照样引用这个空 provider，再重载仍为空，若作为回滚条件，`node add`、`profile use`、D04 修复等无关动作会全部卡死，只能先 `port rm`。该端口此时回落 REJECT 断网（不直连），由 D16 报失败。
2. 端口“在听”（口径见 §5.1.4 末）对 `ports_involved` 中的端口为必须条件，否则 `CliError(EXIT_ROLLED_BACK, t("err.verify_failed"), detail="port P not listening")`（§9 日志 reason 同此写法）；对其它端口不满足只 warn。K6：绑定失败内核不报错，单靠 `_tcp_ok` 会连到占用者而误判，必须同时查占用。

3. 返回值改为 `(manual_readback, port_readback)`；`port_readback` 为 `{P: name}`。`reload` 提交段（`actions.py:485-490`）在写 manual 名字后，把 `port_readback` 写入 `new_state["ports"][i]["name"]`。节点数不为 1 的无关端口不在 `port_readback` 中，其 `name` 保留旧值（内核未运行时 `port list` 会显示这个旧名字，实际可用性以 D16 为准）。

离线写入（内核未运行，`apply_offline`）：`mihomo -t` 查不出坏链接（K11），新端口 `name` 为 `None`；下一次重载时它不在 `ports_involved` 中，坏链接只 warn 不回滚，并由 D16 报失败。

`_verify` 只有 `reload` 一个调用方（`actions.py:478`），改返回值不波及其它路径。

现有 `pre_counts` / `involved` 逻辑不变。

#### 5.1.4 端口占用与“自己在听”

`port_conflicts(ctx, port)`（`actions.py:238-243`）现在只用 `kernel_listens_on`（`GET /configs` 的 `mixed-port`）判断自己。固定端口没有接口可查（K8），改为：

```python
def kernel_listens_on(ctx, port):
    # 新增：固定端口不在管理接口里，旁证 = 内核在运行 + 当前 state 配置了该端口 + config.yaml 与 state 一致
    ...
```

具体：`port in [p["port"] for p in state["ports"]]` 且 `kernel_running(ctx)` 且 `config.yaml` 内容等于 `render_text(state)` 时视为自己在听，再走 `listening_conflicts(port, own_listener=True)`（Linux 侧只排除 uid 等于当前用户的监听，`platform/linux.py:257-259`）。`port_conflicts(ctx, port, state=None)` 与 `kernel_listens_on(ctx, port, state=None)` 都加可选参数 `state` 并往下传；一致性比对用 `render_mod.render_text(state, ctx.paths)`（`render.py:186`）。不传 `state` 时行为与原来相同，原调用方（install、D06）不改。

端口“在听”的统一口径（`_verify`、`port list`、`status`、D16 共用一个 helper `port_listening(ctx, P, state)`）：`port_conflicts(ctx, P, state)` 为空 **且** `_tcp_ok(P)` 为真。helper 多一个参数 `running: bool`，由调用方传入本次已取得的内核运行状态（`status` 已有 `st`、`_verify` 处于重载后必然运行），旁证里不再逐端口调 `kernel_running`；这样不会因服务状态切换抛退出码 3（`actions.py:137-145`），也免去每端口一次 systemd 查询。

`port add` 添加前，对每个新端口 P 检查：

- P 不等于 `mixed_port`、不在已有 `ports` 中（否则 `EXIT_USAGE`，`err.port_taken`）；
- `ctx.platform.listening_conflicts(P)` 为空（新端口内核还没在听，不需要自己的旁证）；否则 `EXIT_ERROR`，`err.port_in_use`（已有文案）。

#### 5.1.5 内核版本把关

- `core_dl.PORTS_MIN_CORE_VERSION = (1, 19, 27)`。
- `port add`：读 `core_dl.core_version(ctx.paths.core)`，低于该版本或读不到 → `EXIT_ERROR`，`err.port_core_old`，提示 `mihomo-cli core upgrade`。
- `core_upgrade`（`actions.py:768-791`）：`state["ports"]` 非空且目标版本 < 1.19.27 → 下载前拒绝，`EXIT_USAGE`。
- `install --core-version`（`actions.py:703-710`）同理。
- D16 在版本过低时报失败（见 §5.1.7）。

#### 5.1.6 命令

| 命令 | 行为 |
| ---- | ---- |
| `port list [--ip]` | 表格：端口 / 节点名 / 链接摘要 / 是否在听；`--ip` 时经每个端口查出口 IP（复用 `exit_ip(port)`，`actions.py:625-640`）。内核运行时节点名取接口 `provider_node_names("pin-<P>")`，否则取 state 的 `name`（`None` 显示“待回读”） |
| `port add <端口> <链接>` | 加一个端口 |
| `port add --file <文件> --start <端口>` | 文件按 `split_links`（`nodes.py:59-71`）取链接，依次分到 start、start+1…；任一端口不合法 / 已占用即整体拒绝，不部分写入 |
| `port rm <端口>...` | 删一个或多个端口；不存在的端口报 `EXIT_ERROR` |

`port add` / `port rm` 都在 `locked(ctx)` 内走 `commit(ctx, state, new_state, "port-add" / "port-rm", files=...)`（`actions.py:606-622`）：files 为 `{port_file(P): encode_manual_file([link]).encode()}`，删除时值为 `None`。内核未运行时走 `apply_offline`（`actions.py:363-375`），名字为 `None`。所有端口一律监听 `127.0.0.1`。

#### 5.1.7 doctor D16

`CHECK_ORDER`（`doctor.py:363`）在 D06 之后插入 `D16`。`ports` 为空 → `skip`。否则：

- 内核版本 < 1.19.27 → `fail`，提示 `mihomo-cli core upgrade`；
- 逐端口 `actions.port_conflicts(ctx, P, state)`（含 §5.1.4 旁证）有结果 → `fail`；
- 内核未运行 → `skip`（`doc.need_running`）；
- 内核运行时：`port_listening` 为假 → `fail`（端口没在听）；`provider_node_names("pin-<P>")` 为空 → `fail`（该端口断网中）；
- 全部通过 → `ok`，detail 如 `9 ports: 7901-7909`。

不可自动修复。D04 失败（config.yaml 与 state 不一致）时，“自己在听”的旁证不成立，本项可能把内核自己的监听连带报成“被占”，提示先修 D04。

#### 5.1.8 status、菜单、备份恢复

- `status()`（`actions.py:643-678`）新增 `info["ports"] = [p["port"] for p in state["ports"]]`；内核运行时另给 `info["ports_listening"]` = `port_listening` 为真的个数。`_print_status`（`cli.py:598-618`）在混合端口行之后加一行“固定端口 9 个：7901-7909（9 个在听）”，没有固定端口不显示该行；内核未运行时只显示“固定端口 9 个：7901-7909（内核未运行）”。端口区间连续时显示 `首-尾`，否则逗号列出。
- 菜单：`run_menu`（`menu.py:60-78`）在“TUN 开关”之前加“固定节点端口”，子菜单：列表（含出口 IP）、添加（输入端口号 + 粘贴链接）、删除（从列表选，二次确认 `menu.confirm_rm`）。
- `backup_restore`（`actions.py:1491`）：恢复键加 `"ports"`；`nodes/` 下文件已整体恢复（`:1494-1504`）。恢复前检查：备份里的任一固定端口等于当前 `mixed_port`（恢复会保留当前 `mixed_port`，`actions.py:1492`）→ `EXIT_USAGE` 并列出端口，否则 `save_state` 的 `_validate` 会在内核已套用新配置之后才抛错；对“备份里有、当前没有”的端口做 §5.1.4 的占用检查，任一被占则拒绝恢复并列出端口；备份里的内核版本要求同 §5.1.5（有端口且内核 < 1.19.27 则拒绝）。0.1.3 的旧备份没有 `ports` 键，按 `_validate` 默认值为空列表恢复，等于删掉当前全部固定端口，恢复前用 `ctx.say("warn", ...)` 提示。
- `install --port N`（`actions.py:697-701`）：N 在 `ports` 中 → `EXIT_USAGE`。

#### 5.1.9 日志

`port add` / `port rm` 记 `[switch] port add ports=7901,7902 count=2` / `[switch] port rm ports=7901`，不记链接。重载事务沿用 `[reload] action=port-add ...`。

### 5.2 接口变更

| 类别 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| 命令 | 新增 `port list/add/rm` | 新增 |
| state.yaml | 新增 `ports` 列表，版本号仍为 1 | 旧文件按默认空列表读入；旧版 mihomo-cli 会忽略该键 |
| 主目录文件 | 新增 `nodes/_port-<端口>.txt` | 新增 |
| config.yaml | 有固定端口时新增 `pin-*` provider、`PORT-*` 组、`listeners` | 无固定端口时与 0.1.3 逐字一致 |
| `status --json` | 新增 `ports`、`ports_listening` | 新增字段，旧字段不变 |
| doctor | 新增 D16，共 16 项 | `--json` 新增一项 |
| 退出码 | 不新增 | — |

reference sibling 回补检查：N/A——本仓库无 `docs/reference/`，本方案不涉及 reference 文档。

## 6. 备选方案与决策

| 方案 | 结论 | 理由 |
| ---- | ---- | ---- |
| 一个端口一个 provider + select 组 + `empty-fallback: REJECT`（采用） | 采用 | 不按名字匹配；节点没了即断网；不在 Python 解析协议 |
| 把节点写成顶层 `proxies` | 否决 | 需要在 Python 里把链接解析成 YAML，违反 `nodes.py:1-5` 的约束 |
| 共用一个 provider + 每组 `filter: ^名字$` | 否决 | 链接名可能重复或含反引号（`nodes.py:36-40`），依赖名字回读才能生成配置，首次离线写入时名字未知 |
| 组写 `proxies: [REJECT]` + `use` | 否决 | K3：默认选中 REJECT，端口一开就断网 |
| 另起独立 mihomo | 否决 | 与 TUN 冲突检测相撞（§1） |

## 7. 影响分析

正向：

1. render 新增内容只在 `ports` 非空时出现 → 现有用户 `config.yaml` 不变，D04 / D05 / 重载核验不受影响。
2. `_verify` 返回值改动 → 唯一调用方 `reload`（`actions.py:478`）同步修改；`apply_offline` 不调用 `_verify`。
3. `kernel_listens_on` 增加旁证 → 调用方 `port_conflicts`（install、D06、新的 D16）；不带 `state` 时行为与原来相同。
4. `backup_restore` 恢复键增加 `ports` → 旧备份恢复会清空固定端口（有 warn 提示）。
5. 固定端口流量：经 K1 不受模式、PROXY 选择、CN 开关影响；TUN 开启时浏览器连 `127.0.0.1:P` 走回环，不进 TUN；节点出站与现有节点出站同一路径（`auto-detect-interface`）。

反向：

1. `nodes/` 目录还有谁读写：`make_backup` 复制全部 `*.txt`（`actions.py:294-296`）——需要；`backup_restore` 删除备份里没有的 `*.txt`（`:1501-1504`）——与恢复 `ports` 一致；`profile_rm` / `profile_rename` 只碰 `nodes/<代理服务名>.txt`（`:917`、`:970-975`）——不碰 `_port-*`。
2. `_service-reapply`（`actions.py:1594-1650`）只管 PROXY / CN 选择；PORT 组只有一个成员，内核 select 组未选时取第一个（`selector.go:103-112`），不需要下发选择；cache.db 里记的 PORT 组选择（`store-selected`）也只能是这个唯一成员。
3. `_service-preflight`（`actions.py:1520-1566`）开机时若固定端口被别的程序占用：K6，内核照常启动、该端口不在听，其它端口和主功能不受影响；D16 报失败。preflight 不新增检查（不阻止内核启动，与 v1 原则一致）。
4. 重载期间：K7，配置未变的固定端口监听器不重开；但每次重载（增删手工节点、切代理服务、增删固定端口等）都会断开固定端口上的已有连接、重载窗口内短暂拒新连接，与主端口相同。切节点（`PUT /proxies/PROXY`）不是重载，不影响固定端口。
5. 内核文件监视：pin provider 是 file 类型，内核监视其路径；节点文件只经 staging → `os.replace` 进入正式路径（`actions.py:342-353`），与手工节点相同。
6. 已有固定端口的节点变空（离线写入的坏链接、文件被删后重启）：§5.1.3 只 warn 不回滚，无关动作照常成功；该端口断网，D16 报失败。

运行时：

- 进程 / 线程：不新增进程；每个固定端口在内核里是一个监听器。
- 资源：9 个端口 = 9 个 TCP + 9 个 UDP 监听；`config.yaml` 每端口约 300 字节。
- 时序：`_verify` 每端口多一次接口调用、一次 TCP 连接与一次 `/proc/net/tcp*` 读取（本机，毫秒级）；`status` 每端口多一次 TCP 连接与一次 `/proc/net/tcp*` 读取；`port list --ip` 每端口一次出口查询（最长 `EXIT_IP_TIMEOUT_SEC`，串行）。
- 部署形态：内核 < 1.19.27 时不允许配置；已配置后被人手工换成旧内核，组空时会回落直连 → D16 报失败（这是唯一会直连的路径，只能靠 D16 提示）。

## 8. 回归测试

L0：本机 pytest（假内核）。L1：编译机一次性 Docker 容器 + 真 mihomo v1.19.32（沿用 v1 §8 的 L1 台架：被测容器 + 节点容器，节点链接写 IP）。L2：编译机宿主真实环境。

| 编号 | 层 | 验证内容 | 通过判据 |
| ---- | ---- | ---- | ---- |
| P-01 | L0 | `render`：`ports` 为空 / 1 个 / 3 个；无当前代理服务时有端口 | 为空时与 0.1.3 快照逐字一致；非空时 provider / 组 / listeners 与 §5.1.2 一致，PROXY 的 `use` 不含 `pin-*`，GLOBAL 只含 PROXY |
| P-02 | L0 | `_validate`：端口越界、重复、等于 `mixed_port`、链接无 `://`、旧 state 无 `ports` | 前四者 `StateError`；旧 state 读出 `ports == []` |
| P-03 | L0 | `port add 7901 <链接>` 在线 / 离线；`--file` 加 `--start` 批量；批量中有一个端口被占 | 在线时 name 已回读、`nodes/_port-7901.txt` 为 base64；离线 name 为 None；被占时整体拒绝，state 与文件未变 |
| P-04 | L0 | `port add` 时假内核让新端口 pin provider 节点数为 0；让新端口 `_tcp_ok` 为假；让新端口被其它进程占用（`listening_conflicts` 非空、`_tcp_ok` 为真） | 三者都回滚，退出码 4，state、`nodes/` 未变 |
| P-18 | L0 | 已有端口 7901 的 provider 节点数为 0 时执行 `node add` 合法链接、`profile use`、`doctor --fix`（D04） | 均成功、不回滚，输出 `warn.port_blocked`；D16 失败 |
| P-05 | L0 | `port rm` 存在 / 不存在的端口 | 前者文件删除、state 更新；后者退出码 1 |
| P-06 | L0 | 内核版本 1.19.21 时 `port add`；有端口时 `core upgrade --version 1.19.26` / `install --core-version 1.19.26`；`install --port 7901` | 均拒绝；`port add` 退出码 1 并提示升级 |
| P-07 | L0 | `backup restore`：备份有端口 → 当前无；0.1.3 格式备份（无 `ports`）；备份端口被占；备份端口等于当前 `mixed_port` | 第一项端口与文件恢复；第二项 warn 后端口清空；第三、四项拒绝且 state、`config.yaml`、`nodes/` 无改动 |
| P-08 | L0 | D16 各分支（无端口 skip、版本低、被占、未在听、节点为空、全部正常）；`status` 一行汇总（连续 / 不连续 / 无端口） | 状态与 §5.1.7、§5.1.8 一致；`--json` 可解析 |
| P-09 | L0 | `port -h` 及子命令 `-h`、`--lang zh/en`；菜单添加 / 删除与命令产生相同 state | 无缺失文案键（`tests/test_cli_help.py:62-71` 扫描通过）；state 相同 |
| P-10 | L1 | 真内核：加两个端口分别绑节点容器上两个不同入站；`mode global` / `rule` / `direct` 下、PROXY 切到 REJECT 时分别经两个端口请求 | 按节点容器两个入站各自的访问日志（或内核日志 `using` 后的出口链）判定：两个端口的请求分别只出现在各自入站，与模式、PROXY 选择无关；不用连接快照判定 |
| P-11 | L1 | 真内核：一个端口绑无法解析的链接 | `port add` 回滚，退出码 4 |
| P-12 | L1 | 真内核：端口生效后把 `nodes/_port-<P>.txt` 改成空文件；随后执行一次 `node add`（触发重载）；再 `systemctl restart` | 改文件后请求仍走旧节点（K10）；重载后 `node add` 成功并 warn，经该端口请求被拒（目标端无直连入站，节点容器收不到），不直连，D16 失败；重启后同样被拒 |
| P-13 | L1 | 真内核：端口生效后 `node use` 切节点、`node add` 手工节点、`profile use` 切代理服务，期间对固定端口每秒发一次短请求 | `node use` 期间无失败；重载类动作允许重载窗口内个别请求失败，重载结束后新请求全部成功且仍走原节点；配置中 PORT 组仍在 |
| P-14 | L1 | 先用 `nc -l 127.0.0.1 <P>` 占住端口再 `port add`；端口生效后 `systemctl restart`，重启前另一进程抢占该端口 | 前者拒绝；后者内核启动、其它端口正常，D16 报该端口被占 |
| P-15 | L1 | 真内核 v1.19.26 与 v1.19.27 各起一次，同一组 `use` 指向空 provider + `empty-fallback: REJECT` | 1.19.27 请求被拒；1.19.26 直连成功（证明版本门槛必要） |
| P-16 | L1 | 有端口时 `doctor`、`backup restore`、`status`、`port list --ip`；日志检查 | doctor 16 项可解析；`mihomo-cli.log` 与输出中不出现链接里的 uuid |
| P-17 | L2 | 编译机升级新版本后 `port add --file ads-all-nodes.txt --start 7901` | `ss -tln` 见 `127.0.0.1:7901`～`7909` 在听；经各端口 `curl` 出口 IP 与下表一致；doctor 全部 OK；`status` 显示 9 个在听 |

P-17 预期出口 IP（2026-11-03 经 macOS Clash Verge 同端口实测）：7901 `99.30.236.56`，7902 `192.220.62.136`，7903 `38.244.24.90`，7904 `99.112.133.223`，7905 `99.112.133.190`（扩展脚本注释值；macOS 上实测连续失败，验证时需先确认节点本身是否可用），7906 `99.112.133.158`，7907 `99.30.236.56`，7908 `99.30.236.56`，7909 `47.180.0.169`。

## 9. 日志 / 观测点

- `mihomo-cli.log`：`[switch] port add ports=... count=N`、`[switch] port rm ports=...`、`[reload] action=port-add|port-rm ... result=ok|invalid`、`[reload] rolled_back ... reason=port P nodes N; link: <链接摘要>` / `port P not listening`。
- 内核日志（`mihomo-cli logs` / `journalctl -u mihomo-cli`）：`Listener port-<P> listen err`（端口被占）；经固定端口的连接日志走 SpecialProxy 分支（`tunnel/tunnel.go:637-638`），`using` 后是该连接的出口链（含 PORT 组与节点名，确切格式以 L1 实测为准）。
- `doctor` D16；`status --json` 的 `ports` / `ports_listening`；`port list --ip`。
- 目标侧探针：`ss -tln | grep 127.0.0.1:79`；`curl -x http://127.0.0.1:<P> https://api.ipify.org`。

## 10. 开放问题

- 7905（`ads-bandwagon-attvps-190`）在 macOS 上经 Clash Verge 实测连续失败，原因未查；不影响功能实现，P-17 验证时单独确认。
