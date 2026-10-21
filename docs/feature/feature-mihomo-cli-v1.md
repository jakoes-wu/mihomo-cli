# mihomo-cli 首版方案：以 mihomo 为内核的命令行代理客户端

> **2026-10-20 注记**：方案已写，第 1 轮评审（2 高 7 中 5 低）、第 2 轮评审（1 高 5 中 3 低）、第 3 轮评审（1 高 8 中 3 低）、第 4 轮评审（1 高 9 中 6 低）、第 5 轮评审（0 高 7 中 3 低）、第 6 轮评审（0 高 2 中 4 低）、第 7 轮评审（0 高 3 中 5 低）、第 8 轮评审（0 高 4 中 4 低）已全部修订（第 8 轮为用户设定的评审上限，其修订未再经独立评审），DNS 接管已按编译机现有脚本改写；仓库为新建空仓库，代码尚未开始。

## 1. 背景

Ubuntu 服务器 / 编译机没有桌面版 Clash 客户端。编译机现状（2026-10-20 实查）：

- 内核 `/usr/local/bin/mihomo`，`Mihomo Meta v1.19.21 linux amd64`，构建标签 `with_gvisor`。
- 运行中的 `mihomo-vps.service`（描述 `mihomo VPS_US (residential, TUN global)`），配置 `/etc/mihomo-vps/config.yaml`（仅 root 可读）；另有未运行的 `mihomo.service`。
- 模式切换脚本 `/usr/local/bin/mihomo-vps-mode`：其 `global` / `rule` 不是 mihomo 原生模式，而是在规则模式下注释 / 启用 `GEOSITE,cn,DIRECT`、`GEOIP,CN,DIRECT` 两条规则后 `systemctl restart mihomo-vps`。脚本先判定当前模式、切换后再做后置校验，两条规则状态不一致时拒绝猜测。
- `/etc/mihomo-vps/` 下有 `nic-dns-takeover.sh`、`nic-dns-revert.sh`、`resolved-takeover.conf`，说明该机 TUN 模式需要额外接管 systemd-resolved 的 DNS。2026-10-20 经用户同意读取：`nic-dns-takeover.sh` 取默认路由网卡 `DEFIF`（`ip route | awk '/^default/{print $5; exit}'`）后执行 `resolvectl dns $DEFIF 127.0.0.1` 与 `resolvectl domain $DEFIF '~.'`；`nic-dns-revert.sh` 执行 `resolvectl revert $DEFIF`；`resolved-takeover.conf` 内容为 `[Resolve]` / `DNS=127.0.0.1` / `Domains=~.`。即 DNS 设在物理网卡而非 TUN 网卡上，指向 mihomo 在 `127.0.0.1:53` 的 DNS 监听（编译机 `ss -tln` 可见该端口在听），另有 resolved 全局配置兜底。

用户需求（原话归纳）：在终端里用命令行完成——添加 / 删除代理服务、切换代理服务、添加 / 删除节点、切换节点、切换代理模式（全局 / 规则 / 直连）、丰富的 help、doctor 自检、开源到 GitHub 与 PyPI，并补充建议功能。

用户已拍板（2026-10-20）：

1. “代理服务”= 一份完整的代理来源，即一个机场订阅或一台自建 VPS（例如 ownexit 搭建的）。
2. 包名 `mihomo-cli`（2026-10-20 查 PyPI 未被占用）。
3. 首版只支持 Linux（以 Ubuntu 20.04 / 22.04 / 24.04 为准）；与操作系统打交道的部分单独分层、给 macOS 留接口；不做 Windows。
4. 三种模式按 mihomo 原生语义（全局 / 规则 / 直连），另设“国内网站走不走代理”开关，承接 `mihomo-vps-mode` 的现有用法。
5. 与编译机现有 `mihomo-vps` 互不干扰：mihomo-cli 跑自己的独立内核实例、独立目录与端口，不接管、不修改它；两者的 TUN 不同时开启。
6. 安装时用一次 sudo，日常换节点 / 换模式 / 换代理服务不需要 sudo。

## 2. 目标 / 非目标

**目标**

1. 一个可 `pipx install mihomo-cli` 安装的命令行工具，命令与交互菜单两种用法覆盖第 1 节全部需求及第 5.1.9 节的补充功能。
2. 切换节点、模式、国内开关不重启内核、不断网；切换代理服务走“校验 → 备份 → 重载 → 后置核验 → 失败回滚”。
3. 任何情况下都不会在用户不知情时把应走代理的流量变成直连（见 5.1.4）。

**非目标**

- Windows 支持；macOS 的具体实现（只留接口）。
- 图形界面 / Web 面板；同一台机器多用户各跑一份实例。
- 通用规则编辑器（首版规则固定，见 5.1.4）。
- 接管、迁移或修改编译机现有 `mihomo-vps`。
- 桌面环境的系统代理设置（GNOME `gsettings` 等）。

## 3. 假设与约束

- 运行环境：systemd 管理的 Linux，x86_64 或 arm64；Python ≥ 3.8（Ubuntu 20.04 自带 3.8）。
- 依赖库按 Python 3.8 可装的最后版本约束上限由 pip 自动选取（2026-10-20 查 PyPI：click 3.8 末版 8.1.8、rich 14.3.4、questionary 2.1.0、prompt_toolkit 3.0.52、httpx 0.28.1、PyYAML 6.0.3）。`pyproject.toml` 只写下限，pip 依据各版本 `requires_python` 自动挑可用版本。（2026-10-21 注记：例外是 3.8 上 prompt_toolkit 已限制在 3.0.52 以下，实际解析为 3.0.51，见 `docs/bugfix/fix-menu-py38.md`。）
- 内核版本：最低支持 mihomo v1.19.0，首版开发与测试基线 v1.19.21。
- 许可证：本项目 MIT；mihomo 为 GPL-3.0，**不随包分发**，由 `install` 子命令从官方发布页（可换镜像）下载。
- 一台机器只有一个 mihomo-cli 实例，systemd 单元名固定 `mihomo-cli.service`。

## 4. 涉及模块

新仓库，全部为新增文件。下表即首版全部文件范围；超出此表的文件视为越权。

| 区域 | 文件 | 改动类型 | 内容 |
| ---- | ---- | ---- | ---- |
| 包元数据 | `pyproject.toml` | 新增 | 包名、依赖、入口 `mihomo-cli = mihomo_cli.cli:main` |
| 包标识 | `src/mihomo_cli/__init__.py` | 新增 | 包版本（读包元数据） |
| 模块入口 | `src/mihomo_cli/__main__.py` | 新增 | 调 `cli.main()`，供 `<python> -m mihomo_cli`（sudo 自调用与 systemd 单元使用，见 5.1.7） |
| 命令行入口 | `src/mihomo_cli/cli.py` | 新增 | click 命令树；无参数且在终端中时进入菜单 |
| 交互菜单 | `src/mihomo_cli/menu.py` | 新增 | questionary 菜单，只调用 `actions.py` |
| 业务动作 | `src/mihomo_cli/actions.py` | 新增 | 命令与菜单共用的全部动作，唯一业务入口 |
| 内核接口客户端 | `src/mihomo_cli/api.py` | 新增 | 经 unix 套接字调用 mihomo 管理接口；各调用超时见 5.1.5 |
| 状态存储 | `src/mihomo_cli/state.py` | 新增 | `state.yaml` 读写、原子替换、文件锁 |
| 配置生成 | `src/mihomo_cli/render.py` | 新增 | 由状态生成 mihomo `config.yaml` |
| 节点链接 | `src/mihomo_cli/nodes.py` | 新增 | 手工节点条目（链接 + 回读得到的节点名）维护、生成 base64 节点文件、隐藏名单转义 |
| 内核下载 | `src/mihomo_cli/core_dl.py` | 新增 | 选资产名、下载、校验、安装到数据目录 |
| 自检 | `src/mihomo_cli/doctor.py` | 新增 | 平台无关检查项 + 调用平台检查项 |
| 文案 | `src/mihomo_cli/i18n.py` | 新增 | 中英文消息表，优先级 `--lang` > `MIHOMO_CLI_LANG` > `LANG`（以 `zh` 开头选中文，否则英文） |
| 平台选择 | `src/mihomo_cli/platform/__init__.py` | 新增 | 按 `sys.platform` 返回 `Platform` 实现 |
| 平台接口 | `src/mihomo_cli/platform/base.py` | 新增 | `Platform` 抽象类（见 5.1.7） |
| Linux 实现 | `src/mihomo_cli/platform/linux.py` | 新增 | systemd、TUN 前置、DNS、端口与冲突探测 |
| macOS 占位 | `src/mihomo_cli/platform/darwin.py` | 新增 | 每个方法抛 `NotSupported`，只为固定接口 |
| 测试 | `tests/` | 新增 | 单元测试 + 假管理接口 + 编译机集成测试脚本 |
| 持续集成 | `.github/workflows/ci.yml`、`release.yml` | 新增 | 测试矩阵；打标签发布 PyPI |
| 文档 | `README.md`、`README.zh-CN.md`、`LICENSE`、`CHANGELOG.md` | 新增 | 安装、用法、命令参考 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 总体结构

```text
命令 (cli.py) ─┐
               ├─> actions.py ─┬─> state.py   读写 state.yaml
菜单 (menu.py) ┘               ├─> render.py  生成 config.yaml
                               ├─> api.py     调 mihomo 管理接口（unix 套接字）
                               └─> platform/  systemd / TUN / DNS / 端口
```

命令和菜单只做参数收集与展示，所有动作都在 `actions.py`，保证两种用法行为一致。

#### 5.1.2 目录布局

单一根目录 `MIHOMO_CLI_HOME`，默认 `~/.local/share/mihomo-cli/`，同时作为 mihomo 的 `-d` 主目录。放在一起的原因：mihomo 对 `file` 类 provider 路径和 `PUT /configs` 的 `path` 都要求落在主目录或 `SAFE_PATHS` 内（`constant/path.go:87` `IsSafePath`，`adapter/provider/parser.go:81`、`:89`，`hub/route/configs.go:407`）。

```text
~/.local/share/mihomo-cli/
  state.yaml            mihomo-cli 的唯一权威状态
  state.lock            文件锁（见下文写入规则）
  config.yaml           由 render.py 生成，禁止手改（文件头注释说明）
  mihomo.sock           管理接口 unix 套接字（内核固定设 0666，安全靠主目录 0700）
  bin/mihomo            下载的内核
  providers/<代理服务>.yaml   订阅缓存（mihomo 维护）
  nodes/<代理服务>.txt        手工节点：由 state 中的条目生成，分享链接列表整体 base64 编码后写成一行（见 5.1.3）
  staging/              重载事务的临时文件（*.new），不在 provider 监视路径上
  backups/<时间戳>/       一次事务的完整备份：config.yaml、state.yaml、nodes/、providers/ 中的本地文件类；最近 10 份
  geoip.metadb, geosite.dat, cache.db   mihomo 自管
```

**状态主从**：`state.yaml` 记录用户想要的状态，是权威；内核的 `cache.db`（`store-selected`，重载 / 重启后由 `hub/executor/executor.go:442-470` 恢复选择）只是内核侧缓存。两者不一致时以 `state.yaml` 为准：每次重载后、`service start` 后、doctor D04 修复时，由 `actions` 把 `PROXY`、`CN` 的选择重新下发到内核。用户绕过本工具直接调接口造成的不一致，下一次上述时机会被覆盖。

`state.yaml` 结构：

```yaml
version: 1
mixed_port: 7897
active_profile: home-vps
mode: rule            # global | rule | direct
cn_route: direct      # direct | proxy
tun: false
mirror: null          # 下载站根地址，替换 https://github.com；内核与地区数据库共用；由 install / core upgrade 的 --mirror 写入
profiles:
  home-vps:
    kind: nodes       # subscription | file | nodes
    url: null
    file: null
    interval: 86400
    user_agent: null  # 为空用 mihomo 默认 clash.meta/<版本>
    hidden: []        # 被“删除”的订阅节点名
    manual:           # 手工节点条目，nodes/<名>.txt 由此生成
      - { link: "vless://...", name: "[M] US-1" }   # name 由重载核验通过后从接口回读；未经重载时为 null（见 5.1.5 第 5 步）
    selected: "[M] US-1"
tun_suspended: null   # 开机时因冲突暂停 TUN 的原因；null 表示未暂停
```

写入一律“写临时文件 → fsync → `os.replace`”，并用 `fcntl.flock`（锁文件 `state.lock`）防两个命令同时改。CLI 的重载事务从读 state 到第 6 步结束全程持锁，其它 CLI 命令拿锁等 130 秒（覆盖单次重载；事务进入回滚时等待方可能超时退出 1，属安全失败）。任何命令在调用 `systemctl start/restart/stop/enable --now` 之前必须先释放 `state.lock`，否则 ExecStartPre 拿不到锁，超时报“另一个命令正在执行”，退出码 1。`_service-preflight` 拿锁最多等 5 秒，拿不到按“不改写”退出 0；此时若检测到冲突，记 ERROR `[tun] preflight lock timeout, conflict=...`，doctor D13 读 journal 中该条并报失败；`_service-reapply` 只在写回 `state.selected` 时短暂拿锁，最多等 5 秒，拿不到放弃写回并记 WARN。

#### 5.1.3 代理服务的三种来源

| 来源 | 添加方式 | 生成的 provider |
| ---- | ---- | ---- |
| 机场订阅 | `profile add <名> --url <订阅链接>` | `type: http`，`path: ./providers/<名>.yaml` |
| 本地配置文件 | `profile add <名> --file <路径>` | 复制到 `providers/<名>.yaml`，`type: file` |
| 自建 VPS | `profile add <名> --node <链接>...` 或先建空再 `node add` | 不生成 `main`，只生成 `manual`（见下） |

源码依据：provider 内容先按 YAML 解析 `proxies` 字段，失败再按分享链接列表解析（`adapter/provider/provider.go:369-379`，`ConvertsV2Ray` 先做 base64 解码再逐行解析，`common/convert/converter.go:16-24`），支持 ss / ssr / vmess / vless / trojan / hysteria / hysteria2 / tuic / socks / http / anytls（`common/convert/converter.go` 各 `case`）。因此 ownexit 输出的 Clash YAML、订阅链接、`vless://` 链接都能直接用，**本项目不在 Python 里实现任何协议解析**。

订阅更新失败（下载失败或解析失败）时 mihomo 保留旧内容、不覆盖缓存文件（`component/resource/fetcher.go:85-91`、`:116-120`），满足“更新失败不断网”。

每个代理服务都可以有一份手工节点 `nodes/<名>.txt`，对应 provider 名固定为 `manual`、节点名统一加 `[M] ` 前缀：自建 VPS 类的全部节点就是手工节点（只有 `manual`，没有 `main`）；订阅 / 本地文件类在 `main` 之外也可以 `node add` 追加。手工节点文件写成“链接列表整体 base64 编码”的单行：明文链接若含 `": "` 可能被当成 YAML 映射解析成功、随后报“缺 proxies 字段”而不再走链接解析（`adapter/provider/provider.go:369-380`）；base64 单行在 YAML 层是字符串，解析为结构体必然失败，从而稳定进入 `ConvertsV2Ray`（其先做 base64 解码，`common/convert/converter.go:17`）。此推断由 R-03 用真内核核实。

#### 5.1.4 生成的配置与“不悄悄直连”保护

```yaml
# 本文件由 mihomo-cli 生成，手改会被覆盖
mixed-port: 7897
bind-address: 127.0.0.1
allow-lan: false
mode: rule
log-level: info
external-controller: ""            # 关闭 TCP 管理端口
external-controller-unix: mihomo.sock
profile: { store-selected: true }
geox-url: {...}                    # 仅 mirror 非空时写
proxy-providers:
  main:   { type: http, url: ...,   # 仅订阅 / 本地文件类；本地文件类为 type: file path: ./providers/<名>.yaml, interval: 86400,
            health-check: { enable: true, url: https://www.gstatic.com/generate_204, interval: 600, lazy: true } }
  manual: { type: file, path: ./nodes/<名>.txt,    # 文件非空才写
            override: { additional-prefix: "[M] " } }
proxy-groups:
  - { name: PROXY, type: select, use: [<实际写出的 provider>], proxies: [REJECT],
      exclude-filter: <由 hidden 生成，hidden 为空时不写> }
  - { name: CN,    type: select, proxies: [DIRECT, PROXY] }
  - { name: GLOBAL, type: select, proxies: [PROXY] }   # 自定义全局组，见下
rules:
  - GEOSITE,private,DIRECT
  - GEOIP,private,DIRECT,no-resolve
  - GEOSITE,cn,CN
  - GEOIP,CN,CN
  - MATCH,PROXY
```

要点：

- **“不悄悄直连”保护**：mihomo 中策略组节点列表为空时会返回 `COMPATIBLE`（`adapter/outboundgroup/groupbase.go:206-207`），而 `COMPATIBLE` 是直连实现（`NewCompatible` 返回 `*Direct`，见 `adapter/outbound/direct.go:96`；空组兜底见 `adapter/outboundgroup/parser.go:100-101`）。会让组变空的情形：隐藏了全部节点、删光手工节点、首次启用就拿不到任何节点的订阅（provider 初始化失败只打日志，`hub/executor/executor.go:325-333`）。已有节点的订阅更新时返回空列表或非法内容则属于解析失败（`adapter/provider/provider.go:446-450`），内核保留旧节点、不覆盖缓存（`component/resource/fetcher.go:111-120`），`PUT /providers/proxies/main` 返回失败，不会把组清空。PROXY 组固定加入 `REJECT`，组永不为空，节点全无时表现为“断网”而非“直连”。展示时 `REJECT` 显示为“阻断（无可用节点）”，`node list` 不把它当节点列出；`node auto`、`node test` 与组延迟结果都排除 `REJECT`。
- **补选规则（REJECT 的代价）**：`proxies: [REJECT]` 生成的组内 provider 排在 `use` 之前（`adapter/outboundgroup/parser.go:171`），没有有效选择记录时默认选中 `REJECT`。因此 `actions.ensure_selection()` 在每次重载后、`profile use`、`node add`（代理服务此前无节点时）、`node rm` / 隐藏了当前节点后执行：`state.selected` 为空或已不在 PROXY 成员中时，选第一个非 `REJECT` 成员并写回 `state.selected`，提示“已自动选择节点 X”；PROXY 只剩 `REJECT` 时保持阻断并提示。
- **国内网站开关**：国内规则指向 `CN` 组，切换 `cn` 即 `PUT /proxies/CN {"name":"DIRECT"|"PROXY"}`，不改规则、不重启，替代 `mihomo-vps-mode` 的“改规则 + 重启”。
- **全局模式不能用内核自动生成的 GLOBAL 组**：全局模式流量走名为 `GLOBAL` 的组（`tunnel/tunnel.go:384-385`）。内核自动生成的 `GLOBAL` 成员顺序固定为 `DIRECT, REJECT, <各组与节点>`（`config/config.go:873`、`:946-958`），选择器初值 `COMPATIBLE` 不在成员中，回落到第 0 个即 `DIRECT`（`adapter/outboundgroup/selector.go:131`、`:100-108`）——首次以全局模式启动或缓存无记录时会整机直连。因此配置里显式定义 `GLOBAL`，只含 `PROXY` 一个成员（内核检测到同名组即不再自动生成，`config/config.go:896`、`:952`）。全局模式下的节点切换仍只操作 `PROXY`，没有切换空档。
- **手工节点加前缀**：手工节点由 provider 的 `override.additional-prefix` 统一加 `[M] `（`adapter/provider/override.go:23`、`:80-81`），避免与订阅节点同名；`Selector.Set` 遇同名只选第一个（`adapter/outboundgroup/selector.go:80-86`），加前缀后 `node use` 无歧义。订阅内部本身的同名节点由内核自动改名（`common/convert/converter.go` 内 `names` 计数），不另处理。
- 隐藏节点：在 **PROXY 组**上写 `exclude-filter`（`adapter/outboundgroup/parser.go:37`，过滤在 `adapter/outboundgroup/groupbase.go:175-187`），由 `hidden` 生成为 `^(?:名1|名2)$`，每个名字逐字符转义，只生成这一个表达式。不放在 provider 层的原因：provider 层过滤后节点为 0 会被当成解析失败（`adapter/provider/provider.go:446-450`），本地缓存解析失败还会退回去远端重新拉取（`component/resource/fetcher.go:68-71`）；放在组层则 provider 节点数不受隐藏影响，隐藏全部时组里仍有 `REJECT`（`REJECT` 不会被 `^(?:...)$` 匹配；名为 `REJECT` 的订阅节点不允许隐藏）。组层正则用 `regexp2.MustCompile` 编译（`adapter/outboundgroup/groupbase.go:61`），非法正则会让解析 panic，因此正则只由转义函数生成，`-t` 退出码非零（含 panic）同样按校验失败处理。含反引号的节点名不允许隐藏。转义规则在 R-07 用真内核核对。
- `use` 只列实际写出的 provider（自建 VPS 类无 `main`，手工节点为空时无 `manual`），可以为空列表；引用不存在的 provider 会报 `'<名>' not found`（`adapter/outboundgroup/parser.go:208-213`）。

#### 5.1.5 各动作的执行路径

| 动作 | 命令 | 实现 | 是否重载 |
| ---- | ---- | ---- | ---- |
| 切节点 | `node use <名>` | `PUT /proxies/PROXY {"name":...}`（`hub/route/proxies.go:32`），写 `state.selected` | 否 |
| 切模式 | `mode global/rule/direct` | `PATCH /configs {"mode":...}`（`hub/route/configs.go:31`、`:356-357`，只改运行时），随后写 `state.mode` 并原子重写 `config.yaml`（不重载），保证重启后仍是新模式 | 否 |
| 国内开关 | `cn direct/proxy` | `PUT /proxies/CN`，写 `state.cn_route` | 否 |
| 更新订阅 | `profile update [名]` | `PUT /providers/proxies/main`（`hub/route/provider.go:23`） | 否 |
| 测延迟 | `node test [名]` | `GET /providers/proxies/<main\|manual>/<名>/healthcheck`（`hub/route/provider.go:30-36`；节点在 provider 里，`/proxies/<名>` 查不到）、`GET /group/PROXY/delay`（`hub/route/groups.go:26`），必带查询参数 `url`（默认 `https://www.gstatic.com/generate_204`）与 `timeout`（毫秒，默认 5000；`hub/route/proxies.go:108-109`、`hub/route/groups.go:68-69`） | 否 |
| 增删节点、隐藏 / 恢复 | `node add/rm/restore` | 改 `nodes/*.txt` 或 `hidden` → 重新生成配置 → 重载 | 是 |
| 切代理服务 | `profile use <名>` | 重新生成配置 → 重载 | 是 |
| 加代理服务 | `profile add` | 只写 `state.yaml`（本地文件类同时复制文件到 `providers/`）；首个代理服务自动 `use`。自动 `use` 回滚时代理服务保留、`active_profile` 仍为空，提示“代理服务已添加但启用失败：<底层原因>，可修正后执行 profile use <名>”，退出码 4；`status` 显示“未启用代理服务” | 否（自动 use 时是） |
| 删 / 改名代理服务 | `profile rm` / `profile rename` | 非当前代理服务：只改 `state.yaml` 并删除 / 改名其文件；当前代理服务：`rm` 拒绝（先 `use` 别的），`rename` 走重载事务：第 1 步把 `providers/<旧>.yaml`（订阅缓存或本地文件）与 `nodes/<旧>.txt` 复制到 `staging/` 下的新名字，第 4 步随其它文件一起放到正式路径，避免新路径无缓存时内核去远端重拉（`component/resource/fetcher.go:52-81`）；第 5 步通过后删旧名文件，第 6 步回滚时删新名文件 | 当前代理服务改名时是 |
| 恢复备份 | `backup restore <名>` | 取备份中的 profiles、`active_profile`、`mode`、`cn_route` 与文件，**保留当前的** `tun`、`tun_suspended`、`mixed_port`、`mirror`（TUN 与端口只能经 `tun on/off`、`install --port` 改，它们各有冲突检查与 sudo 步骤），作为新 state 走重载事务第 1–6 步；第 5 步把 `main`、`manual` 都按“本次涉及”核验（`manual` 精确计数，`main` 大于 0，同下文第 5 步口径） | 是 |
| TUN 开关 | `tun on/off` | 执行顺序以 5.1.8 为准：on 为事务提交后接管 DNS；off 为先还原 DNS 再走事务；内核未运行时只改 state 与 config | 内核运行时是 |

**重载事务**（`actions.reload()`，所有“是否重载=是”的动作共用）：

1. 在内存中得到新 state，写到 `staging/state.yaml.new`（不替换 `state.yaml`）；按新 state 生成 `staging/config.yaml.new`，以及本次涉及的 `staging/nodes/<名>.txt.new`、`staging/providers/<名>.yaml.new`（本地文件类）。内核对 `file` 类 provider 挂了文件监视，文件一变立即热加载（`component/resource/fetcher.go:181-194`、`:201-203`），所以**任何 provider 文件在第 4 步之前都不得写入正式路径**；
2. `bin/mihomo -t -d <主目录> -f <主目录绝对路径>/staging/config.yaml.new` 校验（`main.go:57`、`:144`；`-f` 相对路径按当前工作目录解析而非 `-d`，`main.go:129-133`，故一律用绝对路径），失败（含 panic 导致的非零退出）即清空 `staging/` 并中止。注意 `-t` 只解析配置、不读取 provider 内容（`main.go:144-157`），坏节点 / 坏订阅要靠第 5 步拦截；
3. 把当前 `config.yaml`、`state.yaml`、`nodes/`、本地文件类 `providers/<名>.yaml` 备份到 `backups/<时间戳>/`，保留最近 10 份；记录重载前各 provider 节点数（`GET /providers/proxies`，不存在按 0）；
4. 依次 `os.replace` 本次涉及的 provider 文件与 `config.yaml`（`state.yaml` 仍未替换），调用 `PUT /configs?force=true`，body `{"path": "<绝对路径>"}`（`hub/route/configs.go:375-424`；`force` 影响监听器重建，`hub/executor/executor.go:108`）；
5. 后置核验（任一不满足即失败）：`GET /configs` 的 `mode` 与 `mixed-port` 符合预期；`GET /proxies/PROXY` 存在；`mixed_port` 实际可连（TCP connect）；provider 节点数（`GET /providers/proxies/<名>`，返回 404 按 0 计）：
   - **本次动作涉及的 provider 精确比较**：只要新 state 的当前代理服务含手工节点条目，不论什么动作触发重载（含 `tun on/off`、隐藏 / 恢复），`manual` 一律按“涉及”处理，必须等于 `len(新 state.manual)`——内核未运行时写入的条目从未核验过，可能含无效链接，计数不等时按位置回读会把名字安错，因此计数不等即判失败回滚，退出码 4，提示“手工节点中有无法解析的链接”并列出 `name` 为 null 的条目；`profile use` 切过去后，订阅 / 本地文件类的 `main` 必须大于 0。
   - **本次未涉及的 provider**：重载前大于 0 的，重载后仍须大于 0（订阅缓存过期时内核会在后台异步拉取，`component/resource/fetcher.go:60-61`、`:195-196`，条数可能变化，所以不做精确比较）。
   - 隐藏 / 恢复不改 provider 节点数，只检查 `GET /proxies/PROXY` 的成员列表：本次隐藏的、且重载前确实存在的名字已不在其中；恢复的、且当前 provider 中存在的名字已回到其中。
   - 预期 PROXY 无可用节点（删光手工节点、切到尚无节点的空自建代理服务、隐藏了全部订阅节点且无手工节点）时放行，并提示“当前没有可用节点，代理流量将被阻断”。判断函数 `expects_no_nodes(state)` 由 render 同一份数据得出，`_service-reapply` 的就绪判据共用它。
   - **手工节点名的回读**：CLI 不预测节点名（各协议取名字段不同；重名按出现顺序由 `uniqueName` 加 `-01`、`-02` 后缀，`common/convert/converter.go:631-640`，删掉前面的同名条目后后面条目会改名）。精确计数通过即说明每一行链接都被成功解析（`ConvertsV2Ray` 会静默跳过无效行），而解析按行序进行、provider 保持该顺序（`adapter/provider/provider.go:381-440`），所以 `manual` 的节点顺序与 `state.manual` 条目顺序一一对应。**凡新 state 含手工节点的重载**（`node add/rm`、`profile use`、当前代理服务 `rename`、`backup restore`、首个代理服务自动 `use`），都在计数通过后按位置整体回读、重写全部条目的 `name`；若 `state.selected` 原来指向某个手工条目，按该条目在新列表中的位置迁移到它的新名字，`node rm` 删除的正是选中条目时才清空交给 `ensure_selection()`。不经重载写入的条目（内核未运行时 `node add`、非首个代理服务的 `profile add --node`）`name` 置为 null，由该代理服务下一次重载补齐；`name` 为 null 的条目在 `node list` 中显示链接摘要，`node rm` 可按序号删除。`node rm <名>` 按 `state.manual[].name` 找到条目删除。
   - provider 读取失败内核只打日志、`PUT /configs` 照样返回 204（`hub/executor/executor.go:325-333`、`hub/route/configs.go:422-423`），所以上述计数是拦截坏节点 / 坏订阅的唯一关口。

   核验通过后 `os.replace` 替换 `state.yaml`，再执行 `ensure_selection()`，并把 `CN` 的选择设为 `state.cn_route`。选择恢复失败（如记录的节点已不存在，`PUT /proxies/PROXY` 报错，`hub/route/proxies.go:92-94`）**不算**回滚条件，按补选规则处理；
6. 第 4、5 步任何一步失败：用第 3 步的备份恢复 `config.yaml` 与本次涉及的 provider 文件，清空 `staging/`（`state.yaml` 从未被替换，保持旧值），再次重载；仍失败则 `POST /restart`（`hub/route/restart.go:20`）。报错时同时给出底层错误原文与“已回滚到 <备份名>”，退出码 4；回滚后的重载与 `POST /restart` 都失败，或 TUN 开启时只能提示手动重启，退出码 7（“回滚未完成，需人工处理”），并打印 `backup restore` 与 `sudo systemctl restart mihomo-cli` 的具体命令（以 root 运行 mihomo-cli 会被拒绝，见 5.1.7）。

**管理接口超时**（`api.py` 按调用分别设定，不用 httpx 默认的 5 秒）：`PUT /configs` 120 秒——该请求同步执行完整个应用配置（`hub/route/configs.go:422`），包括等待所有 provider 初始化完（`hub/executor/executor.go:350` `wg.Wait()`），订阅首次下载单次超时 20 秒（`component/resource/vehicle.go:20`），留足多 provider 与慢网余量；`PUT /providers/proxies/<名>` 60 秒；`GET .../delay` 为查询参数 `timeout` 加 5 秒；其余调用 10 秒。`PUT /configs` 超时按第 4 步失败处理进入回滚。

**测试注入点**：环境变量 `MIHOMO_CLI_TEST_FAIL_VERIFY=1` 时第 5 步强制判失败（R-11）；`MIHOMO_CLI_TEST_FAIL_DNS_REVERT=1` 时 `_tun-dns-revert` 不做任何改动直接退出 1（R-29，经 sudo 调用时以 `sudo --preserve-env=MIHOMO_CLI_TEST_FAIL_DNS_REVERT` 传入）；`MIHOMO_CLI_TEST_FAIL_ROLLBACK=1` 时第 6 步的回滚重载与 `POST /restart` 均强制判失败（R-11 后半，退出码 7）；`MIHOMO_CLI_TEST_BAD_CONFIG=1` 时 render 写入一个未知类型的策略组，`-t` 报 `unsupported type`（`adapter/outboundgroup/parser.go:189-190`，R-10）；README 不对外说明。

**内核是否运行的判据**（CLI 与 root 子命令统一）：`systemctl show -p ActiveState,MainPID mihomo-cli`：`ActiveState` 为 `activating` / `deactivating` / `reloading` 时，一切会改 state 的命令返回退出码 3“内核正在启动或停止，请稍后再试”，不改任何文件（ExecStartPre 期间 MainPID 尚为 0，不能据此判为未运行）；`active` 且 MainPID 非 0 才算运行，此时套接字连不上同样返回退出码 3；`inactive` / `failed` 算未运行。内核未运行时，只改文件不重载，并提示 `mihomo-cli service start`。

**下载地址拼法**（`core_dl.py` 与 render 的 `geox-url` 共用）：站点根 `base = state.mirror or "https://github.com"`；`state.mirror` 写入前去掉末尾的 `/`。内核为 `<base>/MetaCubeX/mihomo/releases/download/<版本>/<资产名>`（资产名见 5.1.7 `core_asset_name`）。
- **版本**：`--core-version` / `--version` 指定时用指定值（不带 `v` 前缀时自动补上，`1.19.21` 等同 `v1.19.21`；格式不是 `v<数字>.<数字>.<数字>` 时用法错误，退出码 2）；不指定时读 `<base>/MetaCubeX/mihomo/releases/latest/download/version.txt`（内容如 `v1.19.21`；2026-10-20 实查官方发布页附有该文件，`latest` 路径经 302 跳到最新版，当日为 v1.19.32）。D01 `--fix`：`bin/mihomo -v` 读得到且版本 ≥ 1.19.0 时重新下载该版本；读不到或低于 1.19.0 时取最新版。
- **校验**：官方发布页不附校验和文件（2026-10-20 用 `gh api repos/MetaCubeX/mihomo/releases/tags/v1.19.21` 实查资产列表），但 GitHub 接口为每个资产给出 `digest` 字段（`sha256:<hex>`）。`base` 为默认 GitHub 时，从 `https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/<版本>` 取对应资产的 `digest` 比对；使用镜像时读同目录 `<资产名>.sha256`（内容为 64 位十六进制，可带文件名），两者都取不到时降级为“gzip 完整解压 + 解压后 `-v` 输出含目标版本号”并打印 WARN“未校验摘要”。摘要不符一律拒绝安装，原内核不变。
- 地区数据库为 `<base>/MetaCubeX/meta-rules-dat/releases/download/latest/<文件名>`（默认地址见 `config/config.go:569-572`）。`--mirror` 改变 `state.mirror` 后与切模式一样原子重写 `config.yaml`（不重载），使 D04 一致。

#### 5.1.6 命令清单

无参数且标准输入是终端时进入菜单；否则打印帮助。

```text
mihomo-cli                      进入交互菜单
mihomo-cli status [--no-ip]     一屏概览：内核、代理服务、节点、模式、国内开关、TUN、端口、出口 IP（--no-ip 不查出口 IP）
mihomo-cli install [--core-version V] [--mirror URL] [--port N]   --mirror 写入 state.mirror
                                下载内核与地区数据库、写 systemd 单元（需 sudo）
mihomo-cli uninstall [--purge] [-y]  停止并删除服务；--purge 同时删除数据目录（-y 跳过确认）
mihomo-cli core upgrade [--version V] [--mirror URL]                    不带 --mirror 时用 state.mirror
mihomo-cli service start|stop|restart|status|logs [-f] [-n 行数]
mihomo-cli profile list|show [名] [--reveal]|add <名> (--url U|--file F|--node 链接...) [--interval 秒] [--user-agent UA]
                |rm <名>|use <名>|update [名]|rename <旧> <新>
mihomo-cli node list [--delay]|use <名>|add <链接>...|add --file F|rm <名>|restore <名>|test [名] [--url U] [--timeout 毫秒]|auto
mihomo-cli mode [global|rule|direct]        不带参数显示当前模式
mihomo-cli cn [direct|proxy]                国内网站直连 / 走代理
mihomo-cli tun on|off|status                on / off 需 sudo；status 不需要
mihomo-cli proxy-env [on|off]               输出 export / unset 语句，配合 eval 使用
mihomo-cli conns [--watch]                  当前连接：域名、命中规则、所走节点
mihomo-cli logs [-f] [--level L]            不带 -f：服务日志（journal 最近 100 行；内核 /logs 只推实时日志、没有历史）；
                                            -f：内核实时日志（管理接口 /logs），--level 只在 -f 时生效
mihomo-cli backup list|restore <名>
mihomo-cli doctor [--fix] [--json]
mihomo-cli completion bash|zsh [--install]
mihomo-cli help [命令]                      等价于 <命令> -h
全局选项：--json（机器可读输出）、--lang zh|en、--home <目录>（覆盖 MIHOMO_CLI_HOME）、-h/--help、--version
```

- `node rm` 对手工节点直接删除；对订阅节点改为隐藏，并提示“订阅节点每次更新都会回来，已改为隐藏，可用 node restore 恢复”。
- `node auto`：对 PROXY 组测延迟，选最快的可用节点并切换。
- `proxy-env`：子进程改不了父 shell 的环境变量，所以输出语句，由用户 `eval "$(mihomo-cli proxy-env on)"`；`completion --install` 同时提供 shell 函数 `mproxy on|off` 包装这一步。设置 `http_proxy` / `https_proxy` / `all_proxy` 及大写形式，`no_proxy=localhost,127.0.0.1,::1`。
- 每条命令的帮助都含说明与至少一个示例；`mihomo-cli help <命令>` 等价于 `<命令> -h`。

**退出码**：0 成功；1 一般错误；2 用法错误；3 内核不可达；4 配置校验失败或已回滚；5 需要 sudo 或被以 root 身份运行；6 平台不支持；7 回滚未完成，需人工处理。

#### 5.1.7 平台接口（给 macOS 留口）

`platform/base.py` 定义抽象类 `Platform`，`platform/__init__.py` 按 `sys.platform` 选择实现：

| 方法 | 作用 | Linux 实现 |
| ---- | ---- | ---- |
| `core_asset_name(version)` | 内核下载文件名 | amd64 为 `mihomo-linux-amd64-compatible-<版本>.gz`，arm64 为 `mihomo-linux-arm64-<版本>.gz`（`<版本>` 带 `v` 前缀；2026-10-20 对照 v1.19.0 / v1.19.11 / v1.19.21 资产列表核对：`amd64-v1-` 前缀从 v1.19.12 才有，不能覆盖最低支持版本 1.19.0，`amd64-compatible-` 各版本都有） |
| `install_service(home, user)` / `uninstall_service()` | 后台服务 | 写 `/etc/systemd/system/mihomo-cli.service` 并 `enable --now` |
| `service_action(start/stop/restart)` / `service_status()` / `service_logs(follow)` | 服务控制 | `systemctl` / `journalctl -u` |
| `tun_conflicts()` / `dns_takeover()` / `dns_revert()` | TUN 冲突检查与 DNS 接管 | 见 5.1.8 |
| `listening_conflicts(port, own_listener=False)` | 端口占用 | 尝试 bind + 读 `/proc/net/tcp*` 找占用进程；`own_listener` 为真时属主不可见的监听不算占用 |
| `other_tun_instances()` | 冲突来源（`tun on` 与 preflight 共用同一口径） | ① `ip -j link show type tun` 中名字不是 `mihomocli0` 的网卡；② 可执行文件名（取 `/proc/<pid>/exe` 链接目标的文件名，不用 comm，以免把以 `mihomo-cli` 为名的 Python 入口算进去）含 `mihomo`、`clash`、`sing-box` 的进程，另排除当前 CLI 进程及其父进程链，只排除 PID 等于本服务 MainPID（`systemctl show -p MainPID --value mihomo-cli`）的那一个进程，不按可执行文件路径排除（同一路径启动的其它进程照样算冲突）。任一命中即冲突；只有端口模式的其它 mihomo 也算冲突（保守，开机时无法区分对方是否稍后开 TUN） |
| `installed_unit_text()` / `service_journal(lines)` | 供 doctor 使用：已安装单元原文（D02 比对）、本服务 journal（D13 查 preflight 超时标记） | 读 `/etc/systemd/system/mihomo-cli.service`；`journalctl -u mihomo-cli -b -n N`。自检项本身集中在 doctor.py，平台只提供这些基础调用（不设 `doctor_checks()`） |

systemd 单元：

```ini
[Unit]
Description=mihomo-cli managed mihomo kernel
After=network-online.target mihomo-vps.service mihomo.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=<安装用户>
Environment=MIHOMO_CLI_HOME=<主目录>
ExecStartPre=<mihomo-cli 绝对路径> --home <主目录> _service-preflight
ExecStart=<主目录>/bin/mihomo -d <主目录>
ExecStartPost=-<mihomo-cli 绝对路径> --home <主目录> _service-reapply
ExecStartPost=-+<mihomo-cli 绝对路径> --home <主目录> _tun-dns-apply
ExecStopPost=-+<mihomo-cli 绝对路径> --home <主目录> _tun-dns-revert
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
Restart=on-failure
RestartSec=5
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
```

**开机 / 重启时的 TUN 保护**（systemd 每次启动、包括 `Restart=on-failure` 拉起都会重跑 Pre / Post）：

- **render 的 TUN 取值**：`tun = state.tun and not state.tun_suspended`。所有重载与重写都经同一 render，所以暂停期间的任何重载都不会重新打开 TUN。`tun_suspended` 只在两处清除：`tun on` 经冲突检查通过时，或 preflight 判定冲突已消失时。
- `_service-preflight`（以安装用户运行；写 `state.yaml` / `config.yaml` 与 CLI 共用 `state.py` 的文件锁和原子替换）：`state.tun` 为真且 `tun_conflicts()` 返回冲突时（口径见 5.1.8，与 `tun on` 相同），按 `tun: false` 重写 `config.yaml`，`state.tun_suspended` 置为冲突来源描述，日志 `[tun] suspended reason=...`；无冲突且此前处于暂停时恢复 TUN 配置并清除该字段。拿锁规则见 5.1.2。本命令除 `state.yaml` 不可读外一律退出 0，不阻止内核以端口代理方式启动。`status` 与 doctor D13 显示“TUN 已暂停：与 X 冲突”。
- 前缀语义依据 systemd.service(5)（freedesktop.org 在线手册，2026-10-20 读）：`+` 表示“以完整权限执行”，`User=`、`CapabilityBoundingSet=` 等限制不作用于该行；`-` 表示失败只记录、视同成功；`-` 与 `+` 可同行组合。ExecStartPost 不带 `-` 时一旦失败，后续命令不再执行且单元判为失败，所以两条 Post 都带 `-`。`+` 行的 `$HOME` 等环境手册未写明，因此所有 Exec 行一律显式传 `--home`，并设 `Environment=MIHOMO_CLI_HOME`，不依赖 `~` 解析。
- `_service-reapply`（以安装用户运行）：管理接口在应用配置之前就开始监听（`hub/hub.go:43-45`），provider 在应用配置末尾才加载完（`hub/executor/executor.go:115`），所以就绪判据不是“套接字可连”，而是“`GET /proxies/PROXY` 的成员中出现非 `REJECT` 项”，或 `expects_no_nodes(state)` 为真（此时立即视为就绪，不等满 60 秒）；等待上限 60 秒（订阅首次下载超时 20 秒，`component/resource/vehicle.go:20`，留足余量）。注意内核在 provider 加载完之后才执行 `updateProfile`，用 `cache.db` 记录恢复选择（`hub/executor/executor.go:116`、`:442-470`），而切节点接口不经过应用配置的那把锁，reapply 若落在这两步之间下发的选择会被覆盖。因此就绪后：下发 `state` 中的 `PROXY`、`CN` 选择并执行 `ensure_selection()` → 等 2 秒 → 回读两组的 `now`，与 `state` 不一致则重新下发，最多 3 次（同时受“启动耗时预算”中 reapply 总预算 90 秒约束），仍不一致记 WARN。超时未就绪时记 WARN 后退出 0，**不改写** `state.selected`。等待期间每秒检查内核主进程是否已退出（优先用 systemd 传给 ExecStartPost 的 `$MAINPID`，没有时查服务 MainPID 是否为 0），已退出就记 `[reapply] kernel exited, skip` 立即退出 0；否则配置坏导致内核秒退时每次启动都白等 60 秒，`StartLimitBurst` 要好几分钟才触发（2026-10-20 编译机 L1 R-13 发现）。这是“`service start` 后由 actions 重新下发选择”在 systemd 拉起场景下的落点。
- `_tun-dns-apply` / `_tun-dns-revert`（root 身份）：行为见 5.1.8。这两条以 root 运行的子命令读 `state.yaml` 时只读、**不加文件锁**（加锁可能创建 root 属主的锁文件），**不写主目录下任何文件**（包括 `mihomo-cli.log`），只输出到标准输出 / 错误（进入 journal），避免在用户目录里留下 root 属主的文件。
- 开机先后：`After=mihomo-vps.service mihomo.service` 让编译机上已知的两个实例先于本服务启动（`After=` 只排先后，不会拉起对方，systemd.unit(5)）。其它名字的 TUN 实例开机先后无法保证，由运行时 doctor D12 / D13 发现，见 §10。
- 单元里的 `<mihomo-cli 绝对路径>` 在 `install` 时展开为 `<sys.executable> -m mihomo_cli`（见下“sudo 的使用方式”）；pipx 重装到不同解释器后需重跑 `install`，doctor D02 检查该解释器存在且能 `import mihomo_cli`；不满足时同时提示“停止服务时的 DNS 还原可能没执行”，给出手工清理命令（`sudo resolvectl revert <网卡>`、`sudo rm /run/systemd/resolved.conf.d/mihomo-cli.conf`、`sudo systemctl restart systemd-resolved`）。
- **启动耗时预算**：`TimeoutStartSec` 覆盖 ExecStartPre 到 ExecStartPost 全程（依据 systemd.service(5)，编码时对照手册核实）。最坏耗时 = preflight ≤ 10 秒（含拿锁 5 秒）+ reapply 总预算封顶 90 秒（等待就绪 ≤ 60 秒，其后下发 / 回读重试受剩余预算约束，预算用完即记 WARN 退出 0）+ `_tun-dns-apply` ≤ 25 秒（等网卡与 53 端口 10 秒 + 重启 resolved 与 `resolvectl` 各调用设 15 秒总超时）= 125 秒 < 180 秒。
- 内核自身的 `POST /restart` 是进程内重启，不会重跑 Pre / Post；TUN 开启时重启期间 `127.0.0.1:53` 无人监听、整机域名解析中断，重启失败则一直中断。因此重载事务第 6 步的 `POST /restart` 兜底仅在 TUN 关闭时使用；TUN 开启时改为提示执行 `sudo systemctl restart mihomo-cli`。

以安装用户身份运行，`state.yaml`、`config.yaml`、套接字都归该用户，日常操作无需 sudo；`StartLimitBurst` 保证配置坏掉时不会无限重启。

**sudo 的使用方式**：

- 内部 sudo 只执行以下命令，提示里说明原因：`install` 时 `sudo install -m 0644 <临时单元文件> /etc/systemd/system/mihomo-cli.service`、`sudo systemctl daemon-reload`、`sudo systemctl enable --now mihomo-cli`；`uninstall` 时 `sudo systemctl disable --now mihomo-cli`、`sudo rm /etc/systemd/system/mihomo-cli.service`、`sudo systemctl daemon-reload`；`service start|stop|restart` 时 `sudo systemctl <动作> mihomo-cli`；`tun on|off` 时先 `sudo -v`，事务提交后 `sudo <python 绝对路径> -m mihomo_cli --home <主目录> _tun-dns-apply|_tun-dns-revert`（它们内部写 / 删 `/run/systemd/resolved.conf.d/mihomo-cli.conf`、重启 systemd-resolved、执行 `resolvectl`，见 5.1.8）；doctor D13 `--fix` 同样调用 `_tun-dns-apply`。
- 调用自身时用当前解释器的绝对路径（`sys.executable`）加 `-m mihomo_cli`，不依赖 `~/.local/bin` 是否在 sudo 的 `secure_path` 中；单元里写的“mihomo-cli 绝对路径”同样展开为 `<python 绝对路径> -m mihomo_cli`。
- 有效用户为 root 时，除 `_tun-dns-apply` / `_tun-dns-revert`、`help` 子命令以及带 `-h` / `--help` / `--version` 的调用（只打印、不碰主目录）外一律拒绝运行，退出码 5，提示“不要用 sudo 运行 mihomo-cli，需要时它会自己调用 sudo”，从源头避免在用户目录生成 root 属主文件。

#### 5.1.8 TUN 模式

- **TUN 冲突口径** `tun_conflicts()` = `other_tun_instances()`（5.1.7）+ `127.0.0.1:53` 已被其它进程监听（`listening_conflicts`，如编译机 `mihomo-vps` 的 DNS 监听；占用进程同样只排除本服务 MainPID）。`tun on` 与开机 `_service-preflight` 共用此口径，避免“现在能开、重启后被暂停”。
- 生成的 `tun` 段（仅 `state.tun and not tun_suspended` 时写）：`enable: true`、`device: mihomocli0`、`stack: mixed`、`auto-route: true`、`auto-detect-interface: true`、`dns-hijack: [any:53]`；同时写 `dns` 段：`enable: true`、`listen: 127.0.0.1:53`、`enhanced-mode: fake-ip`、`fake-ip-range: 198.18.0.1/16`、上游写死为：`default-nameserver: [223.5.5.5, 119.29.29.29]`（必须是纯 IP，`config/config.go:1421-1437`）；`proxy-server-nameserver: [https://223.5.5.5/dns-query]`（解析节点域名）；`nameserver: [https://1.1.1.1/dns-query, https://8.8.8.8/dns-query]`（不得为空，`config/config.go:1372-1373`）；`nameserver-policy: {"geosite:cn,private": [https://223.5.5.5/dns-query, https://doh.pub/dns-query]}`。**禁止**使用 `system`（`config/config.go:1216-1217`）、`dhcp://` 等经系统解析的上游：系统解析此时已指向 mihomo 自己，会形成循环。字段写法由 R-01 快照与 R-15 真内核校验。TUN 未生效时不写 `dns` 段，不占用 53 端口。监听 53 依赖单元授予的 `CAP_NET_BIND_SERVICE`。
- **DNS 接管做法**（照编译机现有脚本，见第 1 节）：
  - `dns_takeover()`：取默认路由网卡 `DEFIF`（`ip -j route show default` 的第一条 `dev`）；写 `/run/systemd/resolved.conf.d/mihomo-cli.conf`（内容 `[Resolve]` / `DNS=`（空赋值，先清空主配置与其它 drop-in 已有的全局 DNS 列表，否则列表是追加的，resolved 可能优先用原有服务器而绕过 mihomo；该语义编码时对照 resolved.conf(5) 核实）/ `DNS=127.0.0.1` / `Domains=~.`，新建文件，目录不存在则创建；放在每次开机都会清空的 `/run` 下，断电、卸载残留都不会延续到下次开机。resolved.conf(5) 的配置目录是否含 `/run/systemd/resolved.conf.d/`，编码时对照手册核实）→ `systemctl restart systemd-resolved` 使全局配置生效 → `resolvectl dns $DEFIF 127.0.0.1` 与 `resolvectl domain $DEFIF '~.'`（放在重启之后，以免重启 resolved 后网卡级设置状态不确定）；最后写标记文件 `/run/mihomo-cli/dns.state`（root 属主，记录 `DEFIF`，重启机器后自然消失）。
  - `dns_revert()`：标记存在时对其中记录的网卡执行 `resolvectl revert`（即使此后默认路由换了网卡也还原正确的那块）；删除 drop-in 文件（存在才删）并 `systemctl restart systemd-resolved`；删除标记。标记与 drop-in 都不存在时什么也不做，退出 0——不对没接管过的网卡执行 `revert`，以免清掉网络管理器下发的设置。
  - 设在物理网卡而非 TUN 网卡、并加全局兜底的原因属推断（物理网卡上运营商 DNS 仍可能被选用；网卡重连后网卡级设置可能被网络管理器覆盖），由 R-15 ③ 与 R-15b 验证。
- **`_tun-dns-apply` / `_tun-dns-revert`**（root 身份，5.1.7）：“TUN 生效”指 `state.tun and not tun_suspended` 且本服务 MainPID 非 0（内核在运行）。apply 在 TUN 生效时等待 `mihomocli0` 出现且 `127.0.0.1:53` 可连（最多 10 秒）后执行 `dns_takeover()`；TUN 未生效（关闭或暂停）时改为执行 `dns_revert()`，保证不会留下指向一个没人监听的 `127.0.0.1:53` 的全局配置。revert 一律执行 `dns_revert()`，不看 `state.tun`。任何一步失败退出 1（单元里带 `-` 前缀只记日志；CLI 调用时据此判失败）。
- **`tun on` 执行顺序**：
  1. `state.tun` 已为真且未暂停 → 提示“TUN 已开启”，并顺带做 D13 检查，不通过则提示执行 `mihomo-cli doctor --fix`，退出 0（不需要 sudo，所以先于 sudo 判断）；
  2. `sudo -v` 取得凭据；失败（密码错、无 sudo 权限）→ 退出码 5，不改任何东西；内核构建不支持 TUN 或 `tun_conflicts()` 命中 → 拒绝并说明是哪个进程 / 网卡 / 端口，退出码 1；
  3. 新 state：`tun: true`、`tun_suspended: null`，走重载事务；失败按事务规则回滚，退出码 4，DNS 未动；
  4. 事务提交后 `sudo <python> -m mihomo_cli --home <主目录> _tun-dns-apply`；失败时自动撤回：只执行 `tun off` 第 2 步的 DNS 还原与第 3 步的重载事务，不执行第 3 步失败分支里的“重新接管”；提示“DNS 接管失败：<原因>，已关闭 TUN”，退出码 4；撤回也失败则退出码 7，打印 `sudo mihomo-cli` 不可用时的手工命令（`sudo resolvectl revert <网卡>`、删除 drop-in、`systemctl restart systemd-resolved`）。
- **`tun off` 执行顺序**（先还原 DNS 再关 TUN：重载去掉 `dns` 段后内核立即停掉 53 端口监听，`hub/executor/executor.go:107`、`:239-248`；先还原时 TUN 仍开着，系统转去问原上游的查询仍被 `dns-hijack any:53` 接住，解析不中断）：
  1. `sudo -v`，失败退出码 5；
  2. sudo 执行 `_tun-dns-revert`；失败则不关 TUN（此时 DNS 仍指向在听的 mihomo，可用），退出码 1，打印手工还原命令；
  3. 新 state `tun: false`、`tun_suspended: null` 走重载事务；失败按事务规则回滚，TUN 仍开着，再 sudo 执行 `_tun-dns-apply` 恢复接管（恢复失败只记 WARN：TUN 的 `dns-hijack` 仍兜着解析），退出码 4；回滚也未完成（退出码 7）时内核状态未知、53 端口可能已不在监听，不再重新接管，保持 DNS 已还原。
- **内核未运行时**：`tun on`（仍做第 2 步检查）/ `tun off` 只改 `state.yaml` 与 `config.yaml`，跳过 sudo 与 DNS 步骤，提示 `mihomo-cli service start`；DNS 由启动时的 `ExecStartPost` 接管。

#### 5.1.9 补充功能（相对用户原始需求）

一键安装 / 升级内核与地区数据库；测速与 `node auto`；订阅定时更新（mihomo 自带 `interval`）；`proxy-env`；TUN 开关；`conns` / `logs`；改配置前自动备份与 `backup restore`；`--json` 输出；中英文帮助与 shell 补全。

#### 5.1.10 doctor 检查项

每项输出“状态（通过 / 警告 / 失败）+ 现象 + 修复建议”；`--fix` 只做标注“可自动修复”的项。

| 编号 | 检查 | 可自动修复 |
| ---- | ---- | ---- |
| D01 | 内核文件存在、可执行、版本 ≥ 1.19.0 | 是（重新下载） |
| D02 | systemd 单元存在、已启用、正在运行；最近是否连续重启；单元内容与当前版本应生成的一致 | 是（启用 / 启动，需 sudo） |
| D03 | 管理接口套接字可连、`GET /version` 正常 | 否 |
| D04 | `config.yaml` 与 `state.yaml` 一致（按同一 render 规则重新生成后比对，已计入 `tun_suspended`） | 是（重载） |
| D05 | `mihomo -t` 校验通过 | 否 |
| D06 | 混合端口被本实例监听；未被其它进程占用 | 否 |

> 2026-10-20 注记（编译机 L1 发现）：内核带 AmbientCapabilities 运行，同一用户读 `/proc/<pid>/fd` 链接被拒，属主不可见，服务运行中重跑 `install` 或跑 D06 会把自己误报成占用。install 与 D06 改用 `actions.port_conflicts`：先查占用，有占用时问本实例管理接口 `GET /configs` 的 `mixed-port`，等于该端口才以 `own_listener=True` 复查；接口不通按占用处理。`own_listener` 只排除 `/proc/net/tcp*` 第 8 列 uid 等于当前用户的不可见监听（平台层新增 `listening_sockets` 连同 uid 返回）；root 或其它用户的不可见监听、可见属主的其它进程始终算占用。同一用户的普通进程属主可见，不会被误放。
| D07 | 地区数据库存在且 30 天内更新（文件名不区分大小写，与内核 `constant/path.go` 的 EqualFold 查找一致：文件缺失时内核经 `/configs/geo` 下载会存成 `GeoSite.dat`；install 补下载也覆盖已有的大小写变体，不另建一份） | 是（`POST /configs/geo`，`hub/route/configs.go:30`） |
| D08 | 当前代理服务有可用节点；PROXY 当前选择不是 `REJECT` | 否 |
| D09 | 当前节点延迟测试成功 | 否 |
| D10 | 经代理访问外网，显示出口 IP 与国家 | 否 |
| D11 | 订阅最近一次更新成功的时间 | 是（触发更新） |
| D12 | 存在其它 mihomo / clash 实例或 TUN 网卡（只报告，不处理） | 否 |
| D13 | TUN 状态（含“已暂停：与 X 冲突”）；TUN 生效时：网卡存在、`127.0.0.1:53` 可连、drop-in 存在、`resolvectl status` 中默认路由网卡 DNS 为 `127.0.0.1`、域名解析正常；TUN 未生效时 drop-in 不应存在 | 是（sudo 执行 `_tun-dns-apply`） |
| D14 | 当前 shell 的代理环境变量是否指向本实例端口 | 否 |
| D15 | 主目录权限为 0700（套接字本身是 0666，靠目录隔离） | 是 |

#### 5.1.11 发布流程（GitHub + PyPI）

- **构建与版本**：构建后端 hatchling + hatch-vcs，版本号取自 git 标签 `vX.Y.Z`（checkout 需完整历史，`fetch-depth: 0`；编码时对照 hatch-vcs 文档核对），`pyproject.toml` 不写死版本；`mihomo-cli --version` 读包元数据。
- **ci.yml**（push / PR 触发）：ruff 检查 + pytest（L0 层）；Python 3.8–3.13 矩阵。全部跑在 `ubuntu-24.04`：`actions/python-versions` 的 versions-manifest.json 中 3.8.18 有 24.04 构建（2026-10-20 核）；首次 CI 实跑失败则 3.8 改用 `ubuntu-22.04`。
- **release.yml**（推送 `v*` 标签触发），作业依次：
  1. `test`：复用 ci 的检查，失败即停；
  2. `build`：`python -m build` + `twine check`，产物上传为 artifact；
  3. `publish-testpypi`：`environment: testpypi`，作业级 `permissions: id-token: write`，`pypa/gh-action-pypi-publish@release/v1`，`repository-url: https://test.pypi.org/legacy/`；
  4. `smoke`：在 Ubuntu 20.04 / 22.04 / 24.04 容器中建 venv，先从正式 PyPI 装依赖，再 `pip install --no-deps --index-url https://test.pypi.org/simple/ mihomo-cli==<版本>`（依赖不从 TestPyPI 取，避免同名抢注包；索引有延迟时重试），执行 `--version`、`--help` 与 `help node add`；
  5. `publish-pypi`：`environment: pypi`（在 GitHub 仓库设置中为该 environment 配置“需人工批准”），同样作业级 `id-token: write` + 同一 action；
  6. `github-release`：创建 GitHub Release，附 wheel 与 sdist，正文取 `CHANGELOG.md` 对应版本段。
  以上字段依据 PyPI 官方文档《Publishing with a Trusted Publisher》（docs.pypi.org/trusted-publishers/using-a-publisher/，2026-10-20 读）：作业级 `id-token: write` 为必需，environment 可选但强烈建议，TestPyPI 用 `repository-url` 指定。
- **首次发布前的一次性操作**（由用户在 PyPI / TestPyPI 网站完成）：各登记一个 pending publisher，字段为项目名 `mihomo-cli`、所有者 `jakoes-wu`、仓库 `mihomo-cli`、工作流文件 `release.yml`、环境 `pypi` / `testpypi`。依据《Creating a PyPI Project with a Trusted Publisher》（docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/，2026-10-20 读）：pending publisher **不占用包名**，首次发布前若被他人注册即失效，因此登记后应尽快发布首个版本。
- 创建 GitHub 公开仓库、推送、打标签发布都是对外动作，执行前逐次征得用户确认。

### 5.2 接口变更

新项目，无既有接口。对外暴露面：

- 命令行：5.1.6 命令树、全局选项与退出码；`--json` 输出每个命令一个顶层对象，字段在 README 命令参考中列出，首版之后只增不改。
- 文件：`state.yaml`（带 `version: 1`，结构升级时自动迁移）；`config.yaml` 为生成物，不作为接口。
- systemd 单元名 `mihomo-cli.service`；环境变量 `MIHOMO_CLI_HOME`、`MIHOMO_CLI_LANG`。
- 写进已安装单元的内部接口：全局选项 `--home`，隐藏子命令 `_service-preflight`、`_service-reapply`、`_tun-dns-apply`、`_tun-dns-revert`。承诺跨版本保持名字与参数不变；另外 doctor D02 比对已安装单元与当前版本应生成的单元内容，不一致时提示重跑 `install`。
- 系统文件：TUN 生效期间的 `/run/systemd/resolved.conf.d/mihomo-cli.conf` 与标记 `/run/mihomo-cli/dns.state`（5.1.8），文件名跨版本不变，以便新版本能还原旧版本留下的接管。
- 测试注入环境变量 `MIHOMO_CLI_TEST_FAIL_VERIFY`、`MIHOMO_CLI_TEST_FAIL_ROLLBACK`、`MIHOMO_CLI_TEST_FAIL_DNS_REVERT`、`MIHOMO_CLI_TEST_BAD_CONFIG`：仅测试用，README 不说明，不承诺兼容。

本方案不涉及 `docs/reference/*`，无 reference 回补项。

## 6. 备选方案与决策

| 备选 | 否决理由 |
| ---- | ---- |
| 沿用 `mihomo-vps-mode` 的“改规则 + 重启”切换 | 每次切换断网数秒；可用 select 组 + 管理接口无重启完成 |
| 在 Python 中解析各协议分享链接再写 `proxies` | 重复实现 mihomo 已有的 `ConvertsV2Ray`，协议更新需要跟进 |
| 用 TCP `external-controller` + 密钥 | 需管理密钥；unix 套接字靠文件权限即可隔离，且不占端口 |
| systemd 用户服务（`systemctl --user`） | 需 `loginctl enable-linger` 才能开机运行，TUN 仍需额外授权；系统服务 + `User=` + `AmbientCapabilities` 一次解决 |
| 包装 `mihomo-vps` 作为后端 | 用户已拍板独立实例 |

## 7. 影响分析

- **对编译机现有 mihomo-vps**：mihomo-cli 不读写 `/etc/mihomo-vps`、不操作 `mihomo-vps.service`。可能的交叉点：① 端口——编译机 127.0.0.1 上 7890、7901–7906、7909 已被占用（2026-10-20 `ss -tln` 实查），故默认端口取 7897；`install` 仍实测，被占用时要求 `--port` 另选；② TUN——mihomo-vps 已开 TUN，`tun on` 会被拒绝（5.1.8），mihomo-cli 只以端口代理方式运行；③ mihomo-vps 的 TUN 会接住 mihomo-cli 内核自身的出站流量，即 mihomo-cli 的节点连接再经过 mihomo-vps 转一次。这不影响功能，但延迟测试与出口 IP 会体现两层代理，doctor D12 明确提示。
- **对系统 DNS**：仅在 TUN 生效期间新增 `/run/systemd/resolved.conf.d/mihomo-cli.conf` 并改默认路由网卡的 resolved 设置；drop-in 在 `/run` 下，断电或卸载残留不会延续到下次开机；`tun on/off` 与开机 / 停止时各重启一次 systemd-resolved，期间整机域名解析中断约 1 秒（以 R-15 实测为准）。服务停止（含 `uninstall`、崩溃后 systemd 停止）经 `ExecStopPost` 执行 `dns_revert()`；机器断电后 drop-in 与标记都随 `/run` 消失，开机时 TUN 生效则由 `_tun-dns-apply` 重新接管。服务被 `Restart=on-failure` 拉起的间隙（`RestartSec=5`）以及 StartLimit 停手后，`ExecStopPost` 已还原 DNS，不会留下指向无人监听端口的配置。
- **进程与资源**：常驻一个 mihomo 进程（参考值几十 MB 内存，以实测为准）；CLI 本身为短进程。磁盘约 40 MB（内核约 36 MB，`ls -la` 实查 36319358 字节，另加地区数据库）。
- **频率与时序**：订阅默认每 24 小时更新一次；健康检查 600 秒、`lazy: true`（无流量时不测）。
- **持续状态的累积效应**：① 配置坏导致内核起不来时，systemd 最多 300 秒内重启 5 次后停手进入 failed，doctor D02 报告，不会无限重启；② 订阅长期拉取失败时，mihomo 退避重试并保留旧节点，D11 显示最后成功时间；③ 节点全部失效时，PROXY 组为 `REJECT`，表现为代理流量全部失败（不是直连），D08 报告。
- **对外语义**：`proxy-env` 只影响执行 `eval` 的那个 shell。
- **安全**：`allow-lan: false`、`bind-address: 127.0.0.1`，端口只对本机开放；mihomo 创建套接字后固定设为 0666（`hub/route/server.go:277`），因此靠目录隔离：主目录创建时设为 0700，每条命令在命令入口（`cli.py` 的 group 回调，菜单入口同样经过它）统一校验，权限过宽时拒绝运行并提示 `chmod 700`；豁免 `install`、`uninstall`、`doctor`（D15 要能报告并修复）、`help`、`completion`、systemd 调用的隐藏子命令，以及带 `-h` / `--help` / `--version` 的调用；主目录不存在时不拒绝；doctor 同项检查；订阅链接含令牌，`status` / `profile show` / 日志中只显示域名，完整链接需 `profile show --reveal`。内核文件位于安装用户可写目录，却由单元授予 `CAP_NET_ADMIN` 等能力：能改写该文件的人（安装用户本人）本就有 sudo，风险可接受；`core upgrade` 下载后校验发布页提供的校验和（若发布页无校验和文件，则至少校验 gzip 完整性与 `-v` 输出版本号），README 写明这一点。

## 8. 回归测试

测试分三层：L0 本机单元测试（假管理接口）；L1 编译机一次性 Docker 容器 + 真 mihomo 内核，同一个 docker 网络里放两个容器：被测容器（systemd、被测 mihomo-cli）与节点容器（另一个 mihomo 以 shadowsocks 入站充当节点，并以 HTTP 服务提供订阅与“镜像发布页”）。节点进程不在被测容器内，因此不会被冲突检测命中。docker 网络用固定子网，节点容器用固定 IP；节点链接、订阅地址、镜像地址一律写 IP，不写容器名——TUN 开启后内核用写死的公网 DoH 解析域名（节点域名走 `proxy-server-nameserver`，`adapter/outbound/util.go:46-52`、`hub/executor/executor.go:290-292`），解析不了 docker 容器名。`install` / `core upgrade` 一律带 `--mirror http://<节点容器 IP>`，该服务按 5.1.5“下载地址拼法”的路径放好 `releases/latest/download/version.txt`、内核 `.gz` 资产、`<资产名>.sha256` 与地区数据库；地区数据库也预先放入主目录（缺失时内核会在解析阶段联网下载，`component/geodata/init.go:94-99`），从而全程不依赖外网；L2 编译机宿主（只读观察，不改 mihomo-vps）。

| 编号 | 层 | 验证内容 | 通过判据 |
| ---- | ---- | ---- | ---- |
| R-01 | L0 | `render` 对三种来源（自建 VPS 类只有 `manual`，`use` 只列实际写出的 provider，`exclude-filter` 写在 PROXY 组上）、`tun_suspended` 为真时 TUN 关闭、隐藏名单、手工节点为空 / 非空、mode、cn、tun 的组合生成配置 | 快照比对一致；手工节点为空时不写 `manual` |
| R-02 | L0 | `state.yaml` 原子写与并发锁 | 两进程并发写不损坏文件 |
| R-03 | L1 | 订阅（YAML / base64 链接列表 / 明文链接列表）三种格式添加并启用；手工节点含一条带 `": "` 字样的链接（如 vless 备注） | `node list` 节点数与源一致；手工节点带 `[M] ` 前缀且全部可见 |
| R-04 | L1 | 切节点、切模式、切国内开关 | 管理接口回读一致；切换期间对持续请求无连接中断 |
| R-05 | L1 | 删除 `cache.db` 后以 `mode: global` 冷启动，持续发请求；随后切节点 | 以 `_service-reapply` 完成日志减去补选后的核对等待（`REAPPLY_SETTLE_SEC`，2 秒）为界：之前的请求被拒（目标端无直连入站），之后首个请求出现在 ss 服务端；切节点后入站换到新节点 |
| R-06 | L1 | 增删手工节点、`profile use` 切换到从未选过节点的代理服务、删除当前节点 | 重载后节点列表正确，`backups/` 新增备份；自动选中第一个非 REJECT 节点并提示，`state.selected` 同步 |
| R-07 | L1 | 隐藏含空格、括号、点号、中文、emoji 的订阅节点名 | 只隐藏目标节点，其余保留 |
| R-08 | L1 | ① 已有节点的订阅改为返回 `proxies: []` 后 `profile update`；② 隐藏全部订阅节点；③ 删光自建代理服务的手工节点；④ `profile use` 一个首次就返回空列表的订阅 | ① 报失败、旧节点保留；②③ 不回滚并提示将被阻断，PROXY 只剩 `REJECT`，请求被拒而非直连（目标端无直连入站），D08 失败；④ `main` 为 0 触发回滚，退出码 4 |
| R-09 | L1 | 订阅服务停掉后执行 `profile update` | 旧节点保留；D11 显示上次成功时间 |
| R-10 | L1 | 设 `MIHOMO_CLI_TEST_BAD_CONFIG=1` 后执行 `node add` 合法链接 | `-t` 失败，退出码 4；`config.yaml`、`state.yaml`、`nodes/` 均未变，`staging/` 已清空 |
| R-22 | L1 | `node add` 一条有效 + 一条无效链接；`profile use` 一个内容无法解析的订阅 | 计数核验失败并回滚，退出码 4；`config.yaml`、`state.yaml`、`nodes/*.txt` 为旧值，内核仍以旧节点工作 |
| R-11 | L1 | 设 `MIHOMO_CLI_TEST_FAIL_VERIFY=1` 后执行 `node add` 合法链接；再同时设 `MIHOMO_CLI_TEST_FAIL_ROLLBACK=1` 执行一次 | 前者自动恢复备份，`state.yaml`、`nodes/*.txt` 未变，内核仍以旧节点工作，退出码 4；后者退出码 7 并打印 `backup restore` 与 `service restart` 命令，`state.yaml` 未变 |
| R-12 | L1 | `install` / `uninstall` 在容器内（systemd 容器）执行 | 单元按 5.1.7 生成、启用、运行；卸载后无残留 |
| R-13 | L1 | 写坏配置后启动服务 | 5 次失败后进入 failed，不再重启；D02 失败 |
| R-14 | L1 | doctor 全部检查项正常 / 异常各一次；`--fix` 只修可修项 | 输出与 5.1.10 一致；`--json` 可被解析 |
| R-15 | L1 | systemd 容器加 `NET_ADMIN` 与 `/dev/net/tun`，运行 systemd-resolved。前提准备（docker 默认让 `/etc/resolv.conf` 指向内置 DNS `127.0.0.11`，不经过 resolved，必须改）：`/etc/resolv.conf` 改为只含 `nameserver 127.0.0.53`；resolved 全局配置设 `DNS=127.0.0.11` 作为“开启前的上游”；用 `getent hosts <测试域名>` 先确认经 resolved 解析成功。用 `install` 生成的单元、以非 root 用户运行内核；每个子项之间执行 `systemctl reset-failed mihomo-cli`，避免累计启动次数触发 `StartLimitBurst`。① `tun on/off`；② TUN 开启状态下 `systemctl restart mihomo-cli`；③ TUN 开启状态下默认路由网卡 `ip link set <网卡> down` 再 `up`，随后由测试脚本补回默认路由（容器内没有网络管理器，不会自动恢复）；④ TUN 开启时 `kill -9` 内核后等 systemd 拉起；⑤ 用 `systemctl edit` 临时去掉 `ExecStopPost` 后 `kill -9` 内核并 `systemctl stop`，确认 drop-in 残留，再恢复单元、在内核停止状态下执行 `mihomo-cli tun off`（按 5.1.8 只改 state 与 config）后 `start`（模拟服务异常退出留下的接管）；⑥ 配置快照中 DNS 上游不含 `system`、`dhcp` | ① 开启后 `getent hosts` 与 `curl` 经 TUN 成功、`resolvectl status` 默认路由网卡 DNS 为 `127.0.0.1`、drop-in 存在、`resolvectl status` 的 Global 段 DNS 只有 `127.0.0.1`；关闭后 drop-in 不存在、网卡 DNS 恢复开启前的值；②④ 重启后解析仍正常；③ 网卡恢复后 30 秒内解析经 mihomo（mihomo 日志可见查询）；⑤ 启动后 drop-in 被删除、解析恢复走原上游；⑥ 断言通过。判据中“经 mihomo”以 mihomo 日志出现该查询为准，不只看 `getent` 成功 |
| R-15b | 实机 | 网络管理器重连场景，L1 无法构造。在一台没有运行其它 TUN 的 Ubuntu（由 NetworkManager 管网卡）上：`tun on` → `nmcli device disconnect <网卡>` 再 `nmcli device connect <网卡>` | 30 秒内 `resolvectl status` 默认路由网卡 DNS 仍为 `127.0.0.1` 或全局 drop-in 生效，mihomo 日志可见新查询；不满足则记录现象，回到 5.1.8 调整接管方式 |
| R-16 | L1 | 子项之间执行 `systemctl reset-failed mihomo-cli`。① 本实例 TUN 关闭时，用另一份内核副本（路径不同于 `<主目录>/bin/mihomo`，网卡名不同）另起一个开启 TUN 的 mihomo（其配置不写 `dns.listen`，避免与本实例抢 53 端口，使 ② 只因 TUN 冲突而暂停），执行 `tun on`；② 停掉另一实例 → `tun on`（成功）→ 再起另一实例 → `systemctl restart mihomo-cli`；③ 暂停期间执行一次 `node add`；④ 停掉另一实例后再重启服务；⑤ 另起一份只开端口模式的 mihomo 后执行 `tun on`；⑥ 用 `nc -l 127.0.0.1 53` 占住 53 端口后执行 `tun on`；⑦ TUN 已开时再执行 `tun on` | ① 被拒绝并提示冲突来源；② 内核以端口代理方式启动，`status` 显示 TUN 已暂停，drop-in 不存在；③ 重载后 TUN 仍关闭；④ TUN 恢复；⑤ 被拒；⑥ 被拒并指出端口；⑦ 提示已开启、退出 0，不把本实例自身判为冲突 |
| R-28 | L1 | `--mirror` 服务上把内核 `.gz` 改坏一个字节、或校验和文件写错后执行 `core upgrade`；正常资产再执行一次 | 前者拒绝安装、退出码 1，`bin/mihomo` 的哈希不变、服务仍运行；后者升级成功，`-v` 为新版本；D01 删除内核后 `--fix` 从 `state.mirror` 重新下载成功 |
| R-29 | L1 | `tun on` 时 sudo 失败（让测试用户无 sudo 权限）；`tun on` 时 DNS 设置失败（预先把 `/run/systemd/resolved.conf.d` 换成同名普通文件）；内核停止时 `tun on`；TUN 开启时设 `MIHOMO_CLI_TEST_FAIL_DNS_REVERT=1` 让 `_tun-dns-revert` 失败后 `tun off` | 前者退出码 5，`state.yaml` 与 `config.yaml` 未变；第二项自动撤回，`state.tun` 为假、TUN 网卡不存在，退出码 4；第三项只改 state、提示启动服务，启动后接管生效；第四项 TUN 保持开启、解析正常，退出码 1 |
| R-30 | L1 | 当前代理服务（订阅类）在断开订阅服务后 `profile rename`；手工节点有两条同名（如同一链接加两次）时删除第一条，且当前选中第二条 | 改名成功，节点不变，旧名文件已删除；删除后 `state.manual[].name` 与接口一致，`state.selected` 跟随原第二条的新名字，未被换成别的节点 |
| R-31 | L1 | 以 systemd 拉起服务后立刻（`ExecStartPost` 期间）观察，`state` 选择与 `cache.db` 不同；订阅服务人为延迟 15 秒响应后执行 `profile use` 该订阅 | 前者 reapply 结束时 `PROXY` 的 `now` 与 `state.selected` 一致；后者不报超时，`profile use` 成功 |
| R-17 | L0/L1 | 菜单与命令执行同一动作 | 两者产生相同 `state.yaml` 与管理接口调用序列 |
| R-18 | L0 | 所有命令 `-h` 有说明与示例；`--lang en/zh` | 无缺失文案键 |
| R-32 | L0 | 模拟 `systemctl show` 返回 `ActiveState=activating`、`MainPID=0` 时执行 `node add` 与 `tun on`；`--core-version 1.19.21`、`--core-version abc`；`other_tun_instances` 对 comm 为 `mihomo-cli`、exe 为 python 的进程 | 前者退出码 3 且文件未变；`1.19.21` 规范为 `v1.19.21`，`abc` 退出码 2；python 进程不算冲突 |
| R-19 | L1 | Python 3.8 / 3.10 / 3.12 下 `pipx install` 本地 wheel | 安装成功，`--version` 正常 |
| R-20 | L2 | 编译机宿主以端口模式并存运行，`mihomo-vps` 不受影响 | 前后 `systemctl status mihomo-vps` 与其端口不变；D12 提示并存 |
| R-21 | L1 | 主目录权限改为 0755 后执行任意命令；另一用户连接套接字 | 命令拒绝运行并提示；恢复 0700 后另一用户连接被拒；D15 先失败后通过 |
| R-26 | L1 | 以 systemd 拉起服务（不经 CLI），`state` 中节点选择与 `cache.db` 不同；另检查 TUN 开启时主目录下文件属主 | 启动后内核选择与 `state` 一致；主目录下无 root 属主文件 |
| R-27 | L1 | 以 `sudo mihomo-cli status` 运行；`backup restore` 一份含手工节点的备份；`profile rename` 当前代理服务 | 前者退出码 5 且主目录无 root 属主文件；恢复后 `nodes/` 与备份一致、`state.manual[].name` 已回读，`tun`、`mixed_port` 保持恢复前的当前值；改名后节点与选择保持 |
| R-23 | L1 | `status`、`profile show`、`mihomo-cli.log` 中检查订阅链接 | 只出现域名，不出现令牌；`--reveal` 才显示完整链接 |
| R-24 | 发布 | 推送预发布标签（如 `v0.1.0rc1`）触发 release.yml | TestPyPI 发布成功；三版 Ubuntu 容器 venv + pip 冒烟通过；`publish-pypi` 停在人工批准处 |
| R-25 | L0 | `node auto`、`node test`、组延迟结果 | 不含 `REJECT`；同名订阅节点与手工节点可分别 `node use` |

## 9. 日志 / 观测点

- CLI 每次重载事务在 `~/.local/share/mihomo-cli/mihomo-cli.log` 记一行，前缀 `[reload]`，含动作、代理服务、备份名、耗时、结果与失败原因；切换类动作记 `[switch]`（对象、旧值、新值）；TUN 记 `[tun]`；doctor 记 `[doctor]` 汇总。日志按 1 MB 轮转保留 3 份。
- 内核日志：`journalctl -u mihomo-cli` 与 `mihomo-cli logs`。
- 机器可读：所有命令 `--json`；`status --json` 汇总当前状态，供脚本或监控读取。

## 10. 开放问题

1. 开机先后只对编译机上已知的 `mihomo-vps.service`、`mihomo.service` 排了序；其它名字的 TUN 实例（进程名不在检测名单内的程序，如 OpenVPN）若晚于本服务启动，开机时可能短暂与本服务的 TUN 同时开启，靠 doctor D12 / D13 在运行时发现并提示。属已知残余风险。WireGuard 网卡类型不是 tun，`ip link show type tun` 本就检测不到，它与本服务 TUN 的路由能否共存不在首版检测范围。
