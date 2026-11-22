# Codex Workspace Sync

[English](README.md) · [简体中文](README.zh-CN.md)

## 项目类型与用途

这是一套面向一台 Mac 和一台 Windows 电脑的开源自动化部署工具包，附带可选的 Codex Skill。可以直接运行 Python 脚本，也可以让 Codex 加载 Skill，按同样的流程部署和验收。

- **跨网络同步项目文件。** 在两台受信设备之间双向同步一个专用工作目录，包括尚未提交或尚未被 Git 跟踪的文件。Syncthing 通过 Tailscale 私网传输文件。
- **远程继续原生 Codex 聊天。** 使用 Codex 官方远程连接，从任意一端打开并继续对方设备上的聊天。原生历史保留在创建它的设备上。工具包不复制 Codex 聊天数据库、侧栏分区或置顶关系。

**原生历史迁移设计。** 涵盖 Windows 导出、Mac 项目路径映射、隔离回放验证和导入失败后的恢复。迁移命令目前是设计中的接口。[Windows 到 Mac 迁移设计](docs/native-history-migration.zh-CN.md)

## 怎么用

1. 安装依赖，让两台设备加入同一个 Tailscale 私有网络。
2. 两端运行 `bootstrap.py` 和 `kit.py prepare`，交换并应用对方的配对文件，然后启动同步。
3. 在 Codex 内完成设备配对，再分别验收两个方向的文件传输和远程聊天回复。

## 安装依赖

| 工具 | 用途 | 官方来源 |
| --- | --- | --- |
| Python 3.9 或更新版本 | 运行部署脚本、诊断和测试 | [下载 Python](https://www.python.org/downloads/) |
| Git | 克隆工具包，并独立管理你的项目版本 | [下载 Git](https://git-scm.com/downloads) |
| Tailscale | 为两端提供可跨局域网访问的私网地址 | [下载 Tailscale](https://tailscale.com/download)、[命令行文档](https://tailscale.com/docs/reference/tailscale-cli) |
| Syncthing | 同步项目工作目录 | [下载 Syncthing](https://syncthing.net/downloads/)、[工具包固定使用的版本](https://github.com/syncthing/syncthing/releases/tag/v2.1.7) |
| 支持 Codex 的 ChatGPT 桌面应用 | 为双向远程访问完成身份认证和设备配对 | [下载 ChatGPT 桌面应用](https://chatgpt.com/download)、[远程连接文档](https://learn.chatgpt.com/docs/remote-connections) |

引导脚本支持 macOS 和 Windows 的 AMD64、ARM64 架构。它下载固定版本的 Syncthing，校验压缩包的 SHA-256，并只解压可执行文件。也可以指定本机已有的兼容版本。通过官方安装程序安装 Tailscale 和 ChatGPT，两端登录并按提示完成系统授权及账号授权。确认两端属于同一个 tailnet，并且能看到彼此。再在 Codex 检查账号、工作区和远程控制功能是否可用。

## 部署工作目录同步

先选定一端已有文件的目录和另一端的空目录，或者确认两端参与同步的文件逐字节一致。配对完成前暂停编辑。两端分别选择已有项目目录或创建空副本，并把工具包的状态目录放在同步目录之外。默认使用 Tailscale 私网 TCP 端口 `22020`，本机管理界面使用回环端口 `18384`。

### macOS

```sh
git clone https://github.com/JulesYu777/codex-workspace-sync.git
cd codex-workspace-sync
python3 bootstrap.py
tailscale ip -4
mkdir -p "$HOME/Projects/my-project"
python3 kit.py prepare --role mac --address YOUR_MAC_TAILSCALE_IPV4 --folder "$HOME/Projects/my-project" --syncthing ./bin/syncthing
```

将 IPv4 占位符替换成 Tailscale 输出的地址。如果示例路径与实际项目不同，请使用实际路径。如果独立版 Tailscale 没有可在终端直接调用的命令，请运行 `/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`。

### Windows PowerShell

```powershell
git clone https://github.com/JulesYu777/codex-workspace-sync.git
Set-Location codex-workspace-sync
py -3 bootstrap.py
tailscale ip -4
New-Item -ItemType Directory -Force -Path "$HOME\Projects\my-project"
py -3 kit.py prepare --role windows --address YOUR_WINDOWS_TAILSCALE_IPV4 --folder "$HOME\Projects\my-project" --syncthing .\bin\syncthing.exe
```

替换 IPv4 占位符，并在需要时将示例路径换成实际项目路径。如果 PATH 中找不到 `tailscale`，可尝试 `& "$env:ProgramFiles\Tailscale\tailscale.exe" ip -4`。选定用于同步的实际目录。即使源端同时有已跟踪和未跟踪文件，接收端也可以使用空目录。

### 交换配对文件并启动

1. 通过私密渠道在两台受信设备之间交换 `offer.json`。文件包含 Syncthing 设备 ID、私网地址和待同步文件的路径及哈希清单。应用之前，在对端核对设备 ID 和地址。
2. 在工具包目录中，macOS 运行 `python3 kit.py apply "PEER_OFFER_PATH"`，Windows 运行 `py -3 kit.py apply "PEER_OFFER_PATH"`。将 `PEER_OFFER_PATH` 替换成收到的对方配对文件路径。两端都执行，分别校验本地文件。
3. 继续使用当前系统的 Python 启动器执行 `kit.py start`，让同步在前台运行。若需要登录时自动启动，执行 `kit.py service`。macOS 会注册并立即启动 LaunchAgent。Windows 会写入当前用户的 Run 项，下次登录时启动。若要在本次 Windows 会话立即运行，可另外执行 `kit.py start`。
4. 用相同的 Python 启动器执行 `kit.py doctor --require-ready`。验收时应看到对端以预期 IP 接入，同步完成度为 100%，待同步项目、待删除项目和拉取错误均为 0。

默认状态目录在 macOS 为 `~/.local/share/codex-cross-device-kit`，在 Windows 为 `%LOCALAPPDATA%\codex-cross-device-kit`。要运行另一个独立实例，在每个子命令之前加 `--home PATH`，并选择空闲的管理端口和监听端口。应用配对文件前，如果项目内容发生变化，带相同参数重新运行 `prepare`，再交换更新后的文件。如果两端已有文件且内容不同，先人工处理差异，或为接收端选择空目录。目录已有自定义 `.stignore` 时，请为本工具包使用单独的副本。

同步规则排除 `.git`、`.codex`、常见密钥与环境文件名，以及生成的缓存。Git 对象和索引留在各端本机。共享前请检查项目内其他敏感文件是否也需要排除。空副本会收到工作文件，Git 历史需由 Git 单独管理。专用 Syncthing 实例只使用配对设备和 Tailscale 监听地址。对端覆盖或删除文件时，它会保留最近五个版本。管理界面只监听回环地址，连接仍受 tailnet 策略和系统防火墙约束。

如果 Windows 防火墙拦截监听端口，管理员可在 PowerShell 中用下面的命令将权限限定到这对设备。请替换两个已核验的 Tailscale IPv4 地址及 Windows 实际监听端口。[Windows 防火墙命令文档](https://learn.microsoft.com/powershell/module/netsecurity/new-netfirewallrule)

```powershell
New-NetFirewallRule -DisplayName 'Codex Kit Syncthing' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalAddress 'WINDOWS_TAILSCALE_IPV4' -LocalPort 22020 `
  -RemoteAddress 'MAC_TAILSCALE_IPV4'
```

## Codex 配对与验收

在两端的 **设置 → 连接（Settings → Connections）** 中启用允许控制本机，再通过 **控制其他设备（Control other devices）** 添加对端，并在两端完成官方账号授权。分别从 Mac 和 Windows 打开对端的远程聊天，让它返回各自不同的简短标记。核验远程主机身份和实际回复。远程聊天列表可以打开后，仍须实测消息能发送并收到回复。[官方远程连接设置](https://learn.chatgpt.com/docs/remote-connections)

验收文件同步时，两台设备使用同一个从未用过的测试名，例如 `trial-001`。分别用各自的 Python 启动器运行 `kit.py probe write trial-001`，再运行 `kit.py probe read trial-001`。每端写入的哈希应与对端读出的哈希一致。两端都读到之后，各自运行 `kit.py probe clean trial-001` 删除自己写入的标记。再运行 `kit.py doctor --require-ready`，确认两个标记均已消失。要验收跨局域网场景，让一台设备留在原网络，另一台改用独立网络。

原始部署曾在不同物理网络上实测双向原生 Codex 回复、双向同步新建和未提交文件、离线回补、重启恢复及删除传播。仓库自动化测试使用临时测试目录和两个隔离的 Syncthing 实例。

### 诊断与停止

- 如果 `doctor` 无法访问本机接口，启动专用 Syncthing 实例并检查管理端口。如果本机接口可访问但对端未连接，核对两份配对文件、Tailscale 连通性、tailnet 策略和限定范围的防火墙规则。[Syncthing 连接 API](https://docs.syncthing.net/rest/system-connections-get.html)
- 如果 Codex 报 WebSocket 超时，检查被控设备当前的远程控制注册状态，并在发起连接的设备上检查官方目标的 DNS 和 TLS。若 TLS 证书的主机名不匹配，检查路由和 DNS 解析，恢复有效的 DNS 和 HTTPS 路径，并保留证书验证。必要时对受影响域名使用获准的解析器。[加密 DNS 文档](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/)
- 前台运行时，在两端对应终端按 Ctrl+C 停止。macOS 登录自启可用 `launchctl bootout gui/$(id -u) "PATH_PRINTED_BY_SERVICE"` 停止，再移除该专用 LaunchAgent 文件以禁用自启。Windows 在 `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run` 中移除 `service` 输出的指定值，再通过本机 Syncthing 管理界面关闭该专用实例。可保留项目文件、私有设备身份和配置，供恢复使用。[Windows Run 注册表项](https://learn.microsoft.com/windows/win32/setupapi/run-and-runonce-registry-keys)

## 部署工具包与可选 Skill

部署脚本负责比较文件清单、配置独立的 Syncthing 实例、注册登录自启和验收双向连接。[可选的 Codex Skill](skills/codex-cross-device-kit/SKILL.md) 引导智能体运行这些脚本，并在需要时交由用户完成应用的原生授权。

安装 Skill 时，将 `skills/codex-cross-device-kit` 目录复制到 macOS 的 `~/.agents/skills/` 或 Windows 的 `%USERPROFILE%\.agents\skills\` 下。如果目标目录不存在，先创建它。保留此仓库的本地副本，在 Codex 中选择 `codex-cross-device-kit`，再请它部署或排查问题。[Codex Skill 官方指南](https://learn.chatgpt.com/docs/build-skills)

## 工程验证

```sh
python3 bootstrap.py
SYNCTHING_BIN="$PWD/bin/syncthing" python3 -m unittest discover -s tests -v
```

```powershell
py -3 bootstrap.py
$env:SYNCTHING_BIN = (Resolve-Path .\bin\syncthing.exe).Path
py -3 -m unittest discover -s tests -v
```

GitHub CI 在托管的 macOS 和 Windows 环境运行同一套测试，覆盖初始文件冲突拒绝与重新准备、双向传输哈希一致、离线写入后重启回补、删除传播，以及 Git 和环境文件排除规则。

## 许可证

本工具包由社区维护，采用 [MIT 许可证](LICENSE)。Tailscale、Syncthing 和 Codex 各自由其维护方维护。
