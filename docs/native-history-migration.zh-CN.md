# 原生历史交换、隔离恢复与 Markdown 归档

[English](native-history-migration.md) · [简体中文](native-history-migration.zh-CN.md)

## 选择正确的流程

| 流程 | 数据与目标 | 自动化边界 |
| --- | --- | --- |
| 项目工作树同步 | 通过 Syncthing/Tailscale 传输项目文件 | 沿用 `prepare`、`apply`、`start` 和 `service`，见 [README](../README.zh-CN.md) |
| 官方远程连接 | 聊天在原设备执行 | 使用官方账户授权和设备配对，历史保留在原设备 |
| 按需启用的原生历史工具 | 指定历史包、私有交换目录、隔离恢复配置或 Markdown | 服务仅自动处理兼容单分段；归档/多分段恢复及 Markdown 导出均需显式操作 |

历史服务可以把兼容历史写入明确配置的本机原生配置目录，并非只预览文件、镜像数据库或官方同步功能。`history service` 与工作树的 `kit.py service` 不同；`--watch` 在前台常驻，不安装登录自启。不承诺迁移侧栏分区、置顶、附件或项目素材。

## 环境与数据边界

- 使用 **Python 3.12 或更新版本**。读取 Zstandard 压缩历史需要 Python 3.14 自带的 `compression.zstd`，或显式提供已有的 zstd 可执行文件。不要静默安装或搜索不可信解码器。
- 原生 App Server 操作遵循代码中的 Windows/macOS 构建 allowlist：**`codex-cli 0.171.0-alpha.13` 与 `codex-cli 0.171.0-alpha.13.2`**。`--app-cli` 指向实际安装的原生可执行文件，不能凭相近版本号推定兼容；列入名单仍须在实际目标环境验收。
- 按 README 的官方流程安装、授权 Codex、Tailscale 和 Syncthing。两端原生配置必须属于**同一真人的同一 ChatGPT 账户**。指纹相等只是配对检查，不是分享他人历史的授权。
- 禁止把 `CODEX_HOME`、SQLite 数据库、认证资料或实际对话放入项目工作树或 Git。不要依靠排除规则同步整个原生配置。备份、服务状态、导出包和 Markdown 也属于私有数据。
- 使用仅向两台受信设备开放的**私有专用交换目录**，与项目工作树分开；单独配置其传输，工作树配对不会建立历史通道。原生配置、交换目录和本机服务状态互不嵌套；暂存、导出包及恢复空间也放在它们和 Git 之外。暂存/发布使用支持硬链接的本地文件系统。
- 交换包包含对话正文和元数据。账户指纹、配对标记、收据和路径都须保密。不要把正式配置、日志、笔记、持久化 ID 或哈希贴入公开问题或示例。

下文所有名称和路径均为占位符，不是账户或主机样本；运行前替换成各端真实绝对路径。命令统一写作 `python`，请按系统选择满足版本要求的启动器。在工具包目录执行命令，但把所有私有输入和输出放在其它目录。

## 配置身份与服务

两端分别通过实际安装的原生 App Server 读取本机账户指纹：

```text
python kit.py history identity --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE
```

把返回指纹填入 `account_fingerprint`，私下核对两端一致，不自行构造或公开账户样本。未登录、非 ChatGPT 账户或指纹变化会阻止服务；应重新核对授权，而不是绕过检查。

在同步项目之外创建私有 JSON 配置。以下 Windows 端模板列出了配置字段，运行前必须替换所有 `REPLACE_*` 字符串：

```json
{
  "home": "C:/REPLACE_PRIVATE/native-home",
  "exchange": "C:/REPLACE_PRIVATE/history-exchange",
  "state_dir": "C:/REPLACE_PRIVATE/history-state",
  "app_cli": "C:/REPLACE_INSTALLED/codex.exe",
  "device": "windows",
  "peer": "mac",
  "pair_id": "REPLACE_WITH_PRIVATE_PAIR_TOKEN",
  "account_fingerprint": "REPLACE_WITH_IDENTITY_OUTPUT",
  "path_maps": [
    {"from": "/REPLACE_MAC_ROOT/project", "to": "C:/REPLACE_WINDOWS_ROOT/project"}
  ],
  "interval_seconds": 60,
  "batch_size": 8,
  "sync_compressed": false
}
```

macOS 端的 `home`、`exchange`、`state_dir` 和 `app_cli` 使用本机绝对路径，对调 `device`/`peer`，将 Windows 项目根映射到真实 Mac 项目根。两端使用相同的私有 `pair_id`、指纹及 `sync_compressed` 设置。`device` 与 `peer` 必须不同；`windows`、`mac` 是通用角色标记。交换目录须事先存在。服务等待双方就绪元数据相互匹配后才交换历史。

`interval_seconds` 范围为 1–86400，`batch_size` 为 1–128。启用压缩包时，两端均设置 `sync_compressed: true`。标准库解码器不可用时，在 `decompressor` 填已有的 zstd 可执行文件。把[配置模板](../examples/history-service.json)复制到仓库外，再填写两端各自的值。

`path_maps` 是含 `from`/`to` 的 JSON 数组，两者均用绝对根路径，按最长路径组件前缀匹配。核对实际仓库、项目和额外工作区根，而不只是同名目录。目标必须存在且解析后仍位于映射的本机根下。缺失、歧义或越界映射应拒绝，不能把未知项目塞进任意公共目录。对自动生成的非项目工作区，可在映射中显式设置 `"create_projectless": true`：仅全部工作路径校验完成后，允许创建符合 `ROOT/YYYY-MM-DD/SESSION-DIRECTORY` 的目标。已有聊天保留现有工作目录；Git 项目和其它根目录须事先存在。

```text
python kit.py history service --config ABSOLUTE-CONFIG.json
python kit.py history service --config ABSOLUTE-CONFIG.json --watch
```

先在两端各执行一次并检查私有结果，再启用 `--watch`；前台常驻用 Ctrl+C 停止。服务仅在映射、基线、写入协调及隔离原生读回检查通过后，应用兼容、独立且轮次完整的单分段历史。忙碌、基线变化和待索引状态需要重试/协调恢复，不能强制覆盖。归档或有依赖/多分段历史使用显式隔离恢复，不自动写入正式配置。

常驻进程每轮重读配置并校验本机账户。查看本机状态目录中的 `last-run.json` 和 `runs.jsonl`。需要独立恢复的入站归档记录为 `in:archived-recovery`。评估历史清理前先测量扫描、传输和原生读回的耗时。Rollout 可能保存原始会话，也可能是后续分段的依赖。

## 单分段导出与暂存

```text
python kit.py history export --codex-home LOCAL-CODEX-HOME --thread-id SELECTED-THREAD-ID --out NEW-BUNDLE
python kit.py history stage-fast-forward --source SOURCE.jsonl --target TARGET.jsonl --out NEW-STAGED.jsonl --cwd EXISTING-LOCAL-PROJECT
python kit.py history exchange-once --codex-home LOCAL-CODEX-HOME --thread-id SELECTED-THREAD-ID --exchange EXISTING-PRIVATE-EXCHANGE --device windows --peer mac --stage-dir LOCAL-STAGING --cwd EXISTING-LOCAL-PROJECT
```

`export` 通过本机只读索引定位显式选中的聊天，生成历史与包含字节数、SHA-256 的清单包。压缩输入需要时追加 `--decompressor ABSOLUTE-ZSTD`。可选 `--packed` 选择打包导出，不扩大到有依赖的分段链。暂存接收解码后的 JSONL 快照，不接收打包文件。

暂存比较同一已有聊天的两份快照，保留目标字节前缀，仅追加兼容且完成的轮次，映射结构化工作目录元数据。记录包含父工作区根时，同时传入 `--source-root EXACT-SOURCE-ROOT` 和 `--target-root EXISTING-TARGET-ROOT`。旧或相同源快照返回 `no-op`，不生成新暂存文件。分叉、未完成尾部、依赖分段和无效映射被拒绝。暂存本身不应用到原生配置。

`exchange-once` 发布一个指定本机单分段快照，核验对端包清单后暂存兼容新增内容。另一端对调 `--device` 和 `--peer`；角色标记使用不同的小写值，不能使用 Windows 保留名。相同输出以 `duplicate` 复用；收据绑定源/基线哈希及缺失轮次。冲突或无效/未完整传输包返回 `conflict`/`rejected`、退出码 1，并保留证据。此命令不调度常驻进程，也不应用暂存数据；不要手动把结果复制进正式配置。

## 显式分段链导出、隔离恢复与回退

归档或多分段历史走此流程。暂停源端写入，显式选择一条聊天：

```text
python kit.py history export-lineage --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE --thread-id SELECTED-THREAD-ID --out NEW-LINEAGE-BUNDLE
python kit.py history recover-lineage --bundle LINEAGE-BUNDLE --path-maps ABSOLUTE-MAPS.json --destination ISOLATED-RECOVERY --app-cli NATIVE-CODEX-EXECUTABLE
python kit.py history rollback-lineage --generation ACTIVE-GENERATION --destination ISOLATED-RECOVERY --app-cli NATIVE-CODEX-EXECUTABLE
```

`export-lineage` 需要时追加 `--decompressor ABSOLUTE-ZSTD`。`ABSOLUTE-MAPS.json` 只含与配置中相同的 `path_maps` JSON 数组，不是整个服务配置。导出包保持不可变且与目标分离。不要手工拼接不完整链，或把单分段包当作完整链。

每个恢复目录只服务**一个逻辑聊天**，必须是独立专用空间，绝不能使用日常桌面配置或服务的 `home`。恢复先校验分段大小/哈希、依赖链接和偏移，映射运行路径但不改写历史消息/工具证据，再构建新的原生版本。新开的 App Server 冷读全部轮次和状态通过后才改变 `head.json`。读取此指针，仅在显式隔离的原生环境使用其选中的 `profile-*/home`。保留归档状态不代表获准导入正式配置。

回退参数是恢复返回的**当前活动版本**，命令校验它及此前已验证版本，再切回恢复指针；不是正式配置回滚，也不删除历史。首个版本没有可回退前代。活动版本变化、无效收据、缺段、偏移损坏、不支持构建或原生读回不一致会阻止激活/回退。保留源包、收据和完成的版本；候选失败不意味着可以覆盖最近已验证指针。不要把恢复出的 SQLite 数据库复制进正式配置。

## Markdown 归档与 Obsidian Sync

```text
python kit.py history archive-markdown --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE --thread-id SELECTED-THREAD-ID --out NEW.md
```

该显式命令通过原生接口读取可见对话文本和轮次状态，写入**新的** Markdown 文件。它是可读归档，不是无损原生备份、项目/素材导出、附件镜像或可续聊历史。按敏感数据保存，不覆盖已有笔记。只有明确授权该独立同步渠道时才选择私有 Obsidian vault。

Obsidian Sync 完成后，比较两端文件的实际字节（校验期间不要编辑笔记）：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'ABSOLUTE-VAULT-PATH/NEW.md'
```

```sh
shasum -a 256 '/ABSOLUTE-VAULT-PATH/NEW.md'
```

两端 SHA-256 必须相同。笔记可见或同步图标正常，不能单独证明字节一致、原生可恢复或保密性。

## 拒绝与恢复规则

禁止以最后写入覆盖解决分叉。保留双方历史和交换包，停止受影响流程，私下检查证据。哈希/大小不符、半包、不安全路径、账户/配对不符、不支持构建、依赖缺失、活跃写入或基线改变都不能绕过。等待/忙碌不是收敛成功。服务状态、备份和待处理收据留在本机，明确原因后再重试。恢复中断应用前保留新本机写入，由协调流程校验日志，不手工替换文件。

分段链回退命令不能修复服务的正式配置。若服务协调恢复无法确认安全状态，应停止常驻，保留配置和本机备份，在隔离副本调查。不要删除日志、重置数据库或把恢复目录指向正式 home 来强行继续。

## 可重复验收顺序

1. **盘点与根路径。** 私下记录构建，确认账户、目录互不嵌套及两端实际项目/Git 根。核对 cwd 和全部额外根，包含空格、中文与 Windows 盘符。验证缺失/歧义映射会拒绝，而不是静默选择其它项目。
2. **隔离冷读与回退。** 使用显式选择的合成历史，包括归档/多分段例子及独立恢复目录。关闭验证 App Server 后新开，逐页从最早到最新比较聊天身份、轮次顺序/状态、可见内容和目标项目/cwd。恢复第二个已验证版本，再以其活动版本回退并冷读前代。确认源端与日常桌面配置未变。
3. **Markdown/Obsidian SHA。** 导出新的合成对话笔记，同步期间不编辑，对比两端 SHA-256。确认可见文本及未完成/失败轮次状态符合预期；不能把 Markdown 当作原生备份。
4. **真实隔离双向及断线测试。** 使用两个获授权的隔离原生测试配置和单独配置的私有交换传输，不使用正式 home。分别测试 Windows → Mac、Mac → Windows 和重复执行幂等；断开一端，在另一端追加完整合成轮次，重连后原生冷读验证补传。交换方向重复，确认目标已有聊天不变。再用真正不同物理网络测试；同机两个目录不是跨网证据。
5. **故障注入与停止条件。** 在可丢弃副本测试半包、坏哈希、同 ID 分叉、未完成轮次、缺段、偏移损坏、不支持构建、写锁占用、基线变化、重启/中断应用及账户/配对不符。确认拒绝保留证据和已验证指针，待处理工作协调恢复不重复轮次，冲突绝不自动合并。公开时只保留脱敏结果摘要。

用合成数据和实际 App Server 验证提交前进程退出、重试、去重和回退：

```text
python -m tools.history_fixture build NEW-FIXTURE-DIRECTORY
python -m tools.history_fixture exercise NEW-FIXTURE-DIRECTORY NATIVE-CODEX-EXECUTABLE
```

双端物理设备演练使用 `python -m tools.dual_host_acceptance --help`。把同一组合成数据放到两端，在 Windows 用 `windows` 角色，在 Mac 用 `mac` 角色。每端使用全新的本机 `--root`，`--control` 指向已有可信私有传输中的同一个新目录，同时指定本机数据、可执行文件、tailnet 地址和独立的空闲数据端口及 GUI 端口。工具启动第二个 Syncthing 实例，关闭并重启 Mac 的测试实例，双向补传新轮次，并验证截断、分叉、模拟磁盘写满和锁冲突。用计划部署的实际构建和目录完成验收。
