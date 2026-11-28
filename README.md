# Codex Workspace Sync

[English](README.md) · [简体中文](README.zh-CN.md)

## Project type and purpose

This is an open-source automation and deployment toolkit for one Mac and one Windows PC, with an optional Codex Skill. Run the Python scripts directly, or load the Skill to guide a Codex agent through the same setup and checks.

- **Sync project files across networks.** Keep one dedicated working directory, including uncommitted and untracked files, in sync between two trusted devices. Syncthing copies files over a private Tailscale connection.
- **Continue native Codex chats remotely.** Use official Codex Remote Connections in either direction to open and continue chats on their originating device. Native chat history stays on that device; this workflow does not replicate chat databases, sidebar sections, or pins.
- **Opt in to native history exchange and recovery.** Separately configure same-account, private history exchange for compatible single-segment chats; explicitly export archived or multi-segment chats for isolated recovery, or archive visible conversation text as Markdown. This is not worktree synchronization or Remote Connections. See the [native history guide](docs/native-history-migration.md) for configuration, supported builds, refusal behavior and acceptance checks.

Never put `CODEX_HOME`, SQLite databases or actual conversation data in the synchronized project or Git. History exchange needs its own private directory, separate from both native profiles and the worktree. Native history commands require Python 3.12 or newer; compressed input needs Python 3.14's `compression.zstd` or an explicitly supplied zstd executable. Synthetic tests do not authorize automatic deployment to a live profile.

## How to use it

1. Install the dependencies and join both devices to the same Tailscale network.
2. Run `bootstrap.py` and `kit.py prepare` on each device. Exchange the two offers, apply them on both devices, and start synchronization.
3. Pair the devices through Codex, then verify file transfer and remote replies in both directions.

## What to install

| Tool | Role | Official source |
| --- | --- | --- |
| Python 3.9+ for worktree deployment; 3.12+ for native history | Runs the scripts and checks; use 3.12+ for the full test suite | [Python downloads](https://www.python.org/downloads/) |
| Git | Gets the kit and manages your project independently | [Git downloads](https://git-scm.com/downloads) |
| Tailscale | Gives each device a private endpoint outside the LAN | [Tailscale download](https://tailscale.com/download), [CLI](https://tailscale.com/docs/reference/tailscale-cli) |
| Syncthing | Replicates the project worktree | [Syncthing downloads](https://syncthing.net/downloads/), [Pinned release used by the kit](https://github.com/syncthing/syncthing/releases/tag/v2.1.7) |
| ChatGPT desktop app with Codex | Pairs devices for authenticated remote access | [ChatGPT desktop download](https://chatgpt.com/download), [Remote Connections](https://learn.chatgpt.com/docs/remote-connections) |

The bootstrap downloads the pinned Syncthing executable for macOS or Windows on AMD64 or ARM64, verifies the release archive's SHA‑256 and extracts only the executable. You can also supply an existing compatible Syncthing executable. Install Tailscale and ChatGPT through their official installers, sign in on both devices and complete their normal OS and account authorization prompts. Use the same tailnet and confirm each device can see the other. Check the account, workspace and remote-control availability in Codex.

## Deploy the worktree

Start with one populated project and an empty replica, or with two worktrees whose synchronized files are byte-for-byte identical. Pause edits until both sides finish pairing. Choose an existing project directory or create an empty replica on each host. Keep the kit’s state directory outside the synchronized folder. The configuration uses private TCP port `22020` and loopback GUI port `18384` by default.

### macOS

```sh
git clone https://github.com/JulesYu777/codex-workspace-sync.git
cd codex-workspace-sync
python3 bootstrap.py
tailscale ip -4
mkdir -p "$HOME/Projects/my-project"
python3 kit.py prepare --role mac --address YOUR_MAC_TAILSCALE_IPV4 --folder "$HOME/Projects/my-project" --syncthing ./bin/syncthing
```

Replace the IPv4 placeholder with the address printed by Tailscale and use your actual project path if it differs from the example. If the standalone Tailscale app has no shell alias, run `/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`.

### Windows PowerShell

```powershell
git clone https://github.com/JulesYu777/codex-workspace-sync.git
Set-Location codex-workspace-sync
py -3 bootstrap.py
tailscale ip -4
New-Item -ItemType Directory -Force -Path "$HOME\Projects\my-project"
py -3 kit.py prepare --role windows --address YOUR_WINDOWS_TAILSCALE_IPV4 --folder "$HOME\Projects\my-project" --syncthing .\bin\syncthing.exe
```

Replace the IPv4 placeholder and use your actual project path if it differs from the example. If `tailscale` is unavailable on PATH, try `& "$env:ProgramFiles\Tailscale\tailscale.exe" ip -4`. Use the directory that actually holds the intended replica. An empty replica can be used even when the other host has tracked and untracked files.

### Exchange and activate

1. Exchange the two `offer.json` files directly with the other trusted device. Each offer contains the Syncthing device ID, private endpoint and the synchronized-file path/hash inventory. Verify the device ID and endpoint against the other host before applying. Keep these offers private.
2. From the kit folder on each host, run `python3 kit.py apply "PEER_OFFER_PATH"` on macOS or `py -3 kit.py apply "PEER_OFFER_PATH"` on Windows. Replace `PEER_OFFER_PATH` with the offer file received from the other host. Applying on both hosts verifies each host’s local files.
3. Start it with `kit.py start` using the same Python launcher. This runs in the foreground. To start at login, run `kit.py service` instead. macOS registers and starts a LaunchAgent. Windows registers a current-user Run entry for the next login. On Windows you can also run `kit.py start` for the current session.
4. Run `kit.py doctor --require-ready` using the same Python launcher. It must report a connection using the expected peer IP, 100% peer completion and zero pending items, pending deletions and pull errors.

Commands use per-user state in `~/.local/share/codex-cross-device-kit` on macOS and `%LOCALAPPDATA%\codex-cross-device-kit` on Windows. For a different isolated instance, set `--home PATH` before every subcommand and choose unused GUI and listening ports. If the input changes before applying, run `prepare` again with the same options and re-exchange the updated offers. If two populated worktrees differ, reconcile them before pairing or use an empty replica. A preexisting custom `.stignore` signals an independently configured folder. Use a separate replica with this kit.

The synchronizer excludes `.git`, `.codex`, common key and environment-file names, and generated caches. This keeps Git objects and indices local. Review the selected project for other sensitive files before sharing. An empty replica receives working files and does not acquire Git history. The dedicated Syncthing instance uses only the paired peer and Tailscale listening address and keeps five simple versions of remotely replaced or deleted files. The GUI is bound to loopback. Tailnet policy and machine firewall rules still apply.

To limit Windows firewall permission to this pair, an administrator can use the following in PowerShell when that port is blocked. Substitute the two verified Tailscale IPs and the configured Windows listening port. [Windows firewall cmdlet](https://learn.microsoft.com/powershell/module/netsecurity/new-netfirewallrule)

```powershell
New-NetFirewallRule -DisplayName 'Codex Kit Syncthing' -Direction Inbound -Action Allow `
  -Protocol TCP -LocalAddress 'WINDOWS_TAILSCALE_IPV4' -LocalPort 22020 `
  -RemoteAddress 'MAC_TAILSCALE_IPV4'
```

## Pair Codex and validate

On each host, enable control of that host under **Settings → Connections**, then use **Control other devices** to add the other host. Complete the official account authorization on each device. In each direction, open the peer's remote chat and ask it to return a different short marker. Verify the peer identity and response. A successful remote listing does not by itself verify prompt execution. [Official remote setup](https://learn.chatgpt.com/docs/remote-connections)

For an automated file check, use the same unique run name on both machines, for example `trial-001`. On each host run `kit.py probe write trial-001`, then `kit.py probe read trial-001` using that host's Python launcher. Compare each write hash to the other host's read hash. After both reads, run `kit.py probe clean trial-001` on both hosts to delete their respective test files. Re-run `kit.py doctor --require-ready` and confirm both markers have disappeared. For a genuine WAN test, keep one device on its current network and move the other to a different connection.

The original deployment tested both directions across separate physical networks, including native Codex replies, new and uncommitted files, offline catch-up, restart recovery, and deletion propagation. The repository’s automated tests use synthetic directories and two isolated Syncthing instances.

### Diagnose and stop

- If `doctor` cannot reach its local endpoint, start the dedicated Syncthing instance and check its GUI port. If the local API is available but the peer is absent, compare both offers and inspect Tailscale, tailnet policy and the scoped firewall rule. [Syncthing connections API](https://docs.syncthing.net/rest/system-connections-get.html)
- If Codex reports a WebSocket timeout, check the designated host's live remote-control registration and test DNS and TLS to the official destination on the initiating device. A wrong certificate hostname indicates a path or resolver problem. Restore a valid DNS/HTTPS route and leave certificate verification enabled. Use an approved resolver for the affected domain if necessary. [Encrypted DNS reference](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/)
- To stop a foreground deployment, use Ctrl+C on the two kit instances. For macOS login launch, use `launchctl bootout gui/$(id -u) "PATH_PRINTED_BY_SERVICE"`, then remove that specific LaunchAgent file to keep it disabled. On Windows, remove the exact Run value name printed by `service` from `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run` and shut down only this kit instance using its local Syncthing GUI. [Windows Run keys](https://learn.microsoft.com/windows/win32/setupapi/run-and-runonce-registry-keys) The project and private identity/configuration can be retained for rollback.

## Deployment kit and optional Skill

The deployment scripts compare file inventories, configure an isolated Syncthing instance, register login startup, and verify both directions. The [optional Codex Skill](skills/codex-cross-device-kit/SKILL.md) guides an agent through those scripts and the native authorization prompts.

To install the Skill, copy `skills/codex-cross-device-kit` into `~/.agents/skills/` on macOS or `%USERPROFILE%\.agents\skills\` on Windows. Create the destination directory if needed. Keep the repository checkout available and select `codex-cross-device-kit` in Codex when asking it to deploy or diagnose the setup. [Codex Skill guide](https://learn.chatgpt.com/docs/build-skills)

## Engineering checks

```sh
python3 bootstrap.py
SYNCTHING_BIN="$PWD/bin/syncthing" python3 -m unittest discover -s tests -v
```

```powershell
py -3 bootstrap.py
$env:SYNCTHING_BIN = (Resolve-Path .\bin\syncthing.exe).Path
py -3 -m unittest discover -s tests -v
```

CI runs the same tests in GitHub-hosted macOS and Windows environments. The suite verifies conflict refusal and re-prepare, two-way hash equality, an offline write followed by catch-up after restart, cleanup propagation, and Git and environment-file exclusions.

## License

The community-maintained kit is released under the [MIT License](LICENSE). Tailscale, Syncthing, and Codex are maintained separately.
