---
name: codex-cross-device-kit
description: Deploy or diagnose a paired Mac and Windows Codex setup that shares a Git working tree over Tailscale and Syncthing, including cross-network acceptance tests. Use for two-device setup, remote-control disconnects, and uncommitted-file sync.
---

Locate the checkout of [Codex Cross-Device Kit](https://github.com/JulesYu777/codex-cross-device-kit) and read its README. Use its scripts for configuration and tests.

- Confirm the two trusted devices, intended worktree paths and actual Tailscale addresses. If both worktrees already contain data, compare offers before activation. Preserve existing instances and Git state.
- Install the pinned Syncthing executable with the bootstrap or use an installed binary. Run prepare, exchange verified offers privately, apply on both sides, and confirm doctor. Desktop login, network extension approval and Codex pairing go through their native prompts.
- Verify paths independently. For worktrees, compare write/read hashes in both directions and confirm synchronized cleanup. For Codex, send and read an actual reply in each direction using the remote host identity. Label same-LAN tests accurately.
- On failure, identify whether the affected path is local Syncthing, Tailscale, or the Codex relay. For a Codex TLS hostname mismatch, verify the resolver and routing before asking for a new pairing.
- Keep native history on its originating host. This kit uses the original remote host to continue chats. It has no bulk history importer.
