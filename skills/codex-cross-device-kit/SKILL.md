---
name: codex-cross-device-kit
description: Deploy or diagnose a Mac and Windows pair using Tailscale, Syncthing, and Codex Remote Connections. Use for uncommitted file sync, remote control disconnects, or two-way tests across networks.
---

# Codex Workspace Sync

## English

Locate the [kit checkout](https://github.com/JulesYu777/codex-workspace-sync) and follow its [English setup guide](https://github.com/JulesYu777/codex-workspace-sync/blob/main/README.md) for exact commands.

1. **Prepare.** Identify the two trusted devices, their actual Tailscale addresses, and the intended worktree paths. Use the bootstrap for the pinned Syncthing binary or supply an installed compatible binary. Preserve existing Syncthing instances and Git state.
2. **Pair.** Verify and exchange the offers privately, then apply each offer on the receiving host. Reconcile differing populated worktrees or use an empty replica. Use the native prompts for account login, network extension approval, and Codex pairing.
3. **Verify.** Require `doctor --require-ready` on both hosts. Compare file hashes in both directions and verify cleanup. Open a remote chat on each peer and check an actual reply and the host identity. Use separate networks for a cross-network result.
4. **Diagnose.** Check local Syncthing, the Tailscale path, and the Codex connection independently. On a Codex TLS hostname mismatch, check DNS and routing before re-pairing. Continue native chats on the originating host.

## 简体中文

找到 [工具包仓库](https://github.com/JulesYu777/codex-workspace-sync) 的本地副本，按 [中文部署指南](https://github.com/JulesYu777/codex-workspace-sync/blob/main/README.zh-CN.md) 执行具体命令。

1. **准备。** 核对两台受信设备、实际 Tailscale 地址和工作目录路径。用引导脚本安装固定版本的 Syncthing，或指定本机已有的兼容版本。保留现有 Syncthing 实例及 Git 状态。
2. **配对。** 核对并通过私密渠道交换配对文件，在两端分别应用收到的文件。若两个非空目录内容不同，先处理差异，或选用空副本。账号登录、网络扩展授权和 Codex 配对按应用原生提示完成。
3. **验收。** 两端都通过 `doctor --require-ready`，双向比对文件哈希并确认清理结果。分别打开对端的远程聊天，核对主机身份和实际回复。跨网验收要在不同网络上进行。
4. **诊断。** 分别检查本机 Syncthing、Tailscale 路径和 Codex 远程连接。遇到 Codex TLS 证书主机名不匹配时，先查 DNS 和路由，再考虑重新配对。在原设备上继续原生聊天。
