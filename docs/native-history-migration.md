# Native history exchange, isolated recovery and Markdown archives

[English](native-history-migration.md) · [简体中文](native-history-migration.zh-CN.md)

## Choose the right workflow

| Workflow | Data and destination | Automation boundary |
| --- | --- | --- |
| Project worktree synchronization | Project files over Syncthing/Tailscale | Existing `prepare`, `apply`, `start` and `service`; see the [README](../README.md) |
| Official Remote Connections | Chats execute on their originating device | Official account authorization and device pairing; history remains there |
| Opt-in native history tools | Selected history bundles, a private exchange, isolated recovery profiles or Markdown | Compatible single-segment service only; archived/multi-segment recovery and Markdown export are explicit operations |

The history service can write compatible history into its explicitly configured local native profile. It is not merely a file preview, a database mirror or an official synchronization feature. `history service` is separate from the worktree's `kit.py service`; `--watch` stays in the foreground and does not install login startup. Sidebar sections, pins, attachments and project material are not promised to migrate.

## Requirements and data boundaries

- Use Python **3.12 or newer**. Reading Zstandard-compressed history requires Python 3.14's `compression.zstd` or an existing, explicitly supplied zstd executable. Do not silently install or discover an untrusted decoder.
- Native App Server operations are restricted to the code allowlist: **`codex-cli 0.171.0-alpha.13` and `codex-cli 0.171.0-alpha.13.2`** on Windows/macOS. Use the actual installed native executable as `--app-cli`; do not infer compatibility from a similar version number. A listed build still needs destination-specific acceptance.
- Install and authorize Codex, Tailscale and Syncthing through the normal official flows described in the README. Both native profiles must belong to the **same real person's ChatGPT account**. Fingerprint equality is a pairing check, not permission to share another person's history.
- Never place `CODEX_HOME`, SQLite databases, authentication material or actual conversations in the project worktree or Git. Do not synchronize an entire native profile, even with exclusions. Backups, service state, bundles and Markdown are private data too.
- Use a **private, dedicated exchange directory** shared only by the two trusted devices, separate from the project worktree. Configure its transport separately; worktree pairing does not create a history channel. Keep the profile, exchange and local service state mutually disjoint; keep staging, bundles and recovery outside them and outside Git. Use local filesystems supporting hard links for staging/publication.
- Exchange packets include conversation content and metadata. Account fingerprints, pairing tokens, receipts and paths must remain private. Do not paste production configuration, logs, notes, persistent IDs or hashes into issues or public examples.

All names and paths below are placeholders, not account or host samples. Replace them with actual absolute paths on each device. Commands use `python`; select a Python launcher meeting the requirement on your OS. Run from the kit checkout, but keep all private input/output elsewhere.

## Configure identity and the service

Read the local account fingerprint through the installed native App Server on each device:

```text
python kit.py history identity --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE
```

Use the returned fingerprint as `account_fingerprint`; compare it privately on both devices. Do not calculate or publish account samples. A missing/non-ChatGPT account or changed fingerprint blocks the service; recheck authorization rather than bypassing the check.

Create a private JSON configuration. This Windows-side template lists the configuration fields; every `REPLACE_*` string must be replaced before running:

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

On macOS, use local absolute paths for `home`, `exchange`, `state_dir` and `app_cli`, reverse `device`/`peer`, and map the Windows project root to the actual Mac project root. Use the same private `pair_id`, fingerprint and `sync_compressed` setting on both sides. Keep `device` and `peer` distinct; `windows` and `mac` are generic role tokens. The exchange directory must already exist. The service waits for reciprocal matching readiness metadata before exchanging history.

`interval_seconds` accepts 1–86400 and `batch_size` accepts 1–128. Enable compressed packets with `sync_compressed: true` on both sides. Where the standard decoder is unavailable, specify an existing executable in `decompressor`. Copy [the placeholder configuration](../examples/history-service.json) outside the repository and fill in the values for each device.

`path_maps` is an array of `from`/`to` objects using absolute source and target roots. Matching uses the longest path-component prefix. Verify the actual repository, project and extra workspace roots, not just directory names. Targets must exist and resolve inside the mapped local root. Reject missing, ambiguous or escaping mappings; do not redirect unknown projects into an arbitrary common folder. To handle generated non-project workspaces, a mapping may explicitly set `"create_projectless": true`: only validated `ROOT/YYYY-MM-DD/SESSION-DIRECTORY` targets may be created after all work paths validate. Existing chats keep their current working directory; Git projects and other roots must already exist.

```text
python kit.py history service --config ABSOLUTE-CONFIG.json
python kit.py history service --config ABSOLUTE-CONFIG.json --watch
```

Start with one cycle on both devices and inspect private results before enabling `--watch`. Stop a foreground watcher with Ctrl+C. The service applies only compatible standalone, completed single-segment histories after mapping, baseline, writer coordination and isolated native readback checks. Busy, stale and pending-index cases require retry/reconciliation, not forced overwrite. Archived or dependent/multi-segment histories belong in explicit isolated recovery, not automatic live application.

The watcher rereads its configuration and verifies the local account each cycle. Inspect `last-run.json` and `runs.jsonl` in the local state directory. An incoming archive that requires a separate native recovery reports `in:archived-recovery`. Measure scan, transfer and native readback times before considering any history cleanup. Rollouts can contain original history and segments needed to read later turns.

## Export and stage a single segment

```text
python kit.py history export --codex-home LOCAL-CODEX-HOME --thread-id SELECTED-THREAD-ID --out NEW-BUNDLE
python kit.py history stage-fast-forward --source SOURCE.jsonl --target TARGET.jsonl --out NEW-STAGED.jsonl --cwd EXISTING-LOCAL-PROJECT
python kit.py history exchange-once --codex-home LOCAL-CODEX-HOME --thread-id SELECTED-THREAD-ID --exchange EXISTING-PRIVATE-EXCHANGE --device windows --peer mac --stage-dir LOCAL-STAGING --cwd EXISTING-LOCAL-PROJECT
```

`export` locates the explicitly selected thread using a read-only local index and produces a history/manifest bundle with sizes and SHA-256. For compressed input, append `--decompressor ABSOLUTE-ZSTD` when required. Optional `--packed` selects packed export; it does not widen support to dependent lineages. Staging accepts decoded JSONL snapshots, not packed files.

Staging compares snapshots of the same existing thread. It retains the target bytes as a prefix and appends only compatible completed turns, mapping structured working-directory metadata. If parent workspace roots are recorded, pass both `--source-root EXACT-SOURCE-ROOT` and `--target-root EXISTING-TARGET-ROOT`. Older/equal source snapshots yield `no-op` without a new staged file. Divergent turns, incomplete tails, dependencies and invalid mappings are refused. Staging alone never applies to a native profile.

`exchange-once` publishes one selected local single-segment snapshot, verifies the peer's packet inventory and stages a compatible extension. Reverse `--device` and `--peer` on the other side; role tokens must be lowercase, distinct and not reserved Windows names. Identical output is reused as `duplicate`; receipts bind source/baseline hashes and missing turns. Conflict or invalid/partial packets produce `conflict`/`rejected` and exit code 1, retaining evidence. This command does not schedule a watcher or apply staged data. Do not copy its output into a live profile manually.

## Explicit lineage export, isolated recovery and rollback

Use this route for archived or multi-segment histories. Pause source writes and select one thread explicitly:

```text
python kit.py history export-lineage --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE --thread-id SELECTED-THREAD-ID --out NEW-LINEAGE-BUNDLE
python kit.py history recover-lineage --bundle LINEAGE-BUNDLE --path-maps ABSOLUTE-MAPS.json --destination ISOLATED-RECOVERY --app-cli NATIVE-CODEX-EXECUTABLE
python kit.py history rollback-lineage --generation ACTIVE-GENERATION --destination ISOLATED-RECOVERY --app-cli NATIVE-CODEX-EXECUTABLE
```

Append `--decompressor ABSOLUTE-ZSTD` to `export-lineage` when needed. `ABSOLUTE-MAPS.json` contains just the same `path_maps` JSON array, not the whole service configuration. Keep the bundle immutable and separate from the destination. Do not hand-build a partial chain or treat a single-segment bundle as a complete lineage.

Each recovery destination serves **one logical thread** and must be a dedicated, independent space, never the normal desktop profile or the service's `home`. Recovery verifies segment sizes/hashes, dependency links and offsets, maps runtime paths without rewriting historical message/tool evidence, and constructs a fresh native generation. A new App Server cold-reads all turns and statuses before `head.json` is changed. Read `head.json` and use the selected `profile-*/home` only in an explicitly isolated native environment. Archive state does not imply permission for live import.

Rollback takes the **currently active generation** returned by recovery, verifies it and its previously verified generation, then switches the recovery pointer back. It is not a live-profile rollback and does not delete history. A first generation has no predecessor to restore. Changed generations, invalid receipts, missing segments, corrupt offsets, unsupported builds or native readback mismatch block activation/rollback. Retain source bundles, receipts and completed generations; a failed candidate is not permission to overwrite the last verified head. Do not copy a recovered SQLite database into a live profile.

## Markdown archive and Obsidian Sync

```text
python kit.py history archive-markdown --codex-home LOCAL-CODEX-HOME --app-cli NATIVE-CODEX-EXECUTABLE --thread-id SELECTED-THREAD-ID --out NEW.md
```

This explicit command reads visible conversation text and turn statuses through the native interface into a **new** Markdown file. It is a readable archive, not a lossless native backup, project/material export, attachment mirror or a resumable chat. Treat the file as sensitive; do not overwrite an existing note. Only choose a private Obsidian vault if you authorize that separate sync channel.

After Obsidian Sync completes, compare the exact file bytes on both devices (do not edit the note during verification):

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'ABSOLUTE-VAULT-PATH/NEW.md'
```

```sh
shasum -a 256 '/ABSOLUTE-VAULT-PATH/NEW.md'
```

The two SHA-256 values must match. Vault visibility or a sync icon alone does not prove byte equality, native recoverability or confidentiality.

## Refusal and recovery rules

Never resolve a fork by last-writer-wins. Preserve both histories and packets; stop the affected flow and inspect private evidence. Hash/size mismatch, partial packet, unsafe path, account/pair mismatch, unsupported build, missing dependency, active writer or changed baseline must not be bypassed. Waiting/busy is not successful convergence. Service state/backups and pending receipts stay local; retry only after the cause is understood. Before restarting a pending apply, preserve new local writes and let reconciliation validate its journal rather than replacing files by hand.

The lineage rollback command cannot repair the service's live profile. If service reconciliation cannot establish a safe state, stop the watcher, retain the profile and local backups, and investigate in an isolated copy. Do not delete a journal, reset a database or point recovery at the live home to force progress.

## Repeatable acceptance, in order

1. **Inventory and roots.** Record build versions privately, confirm account identity, disjoint directories and actual project/Git roots on both hosts. Check mapped cwd and every extra root, including spaces, Unicode and Windows drive syntax. Verify missing/ambiguous mappings refuse rather than silently selecting another project.
2. **Isolated cold read and rollback.** Use synthetic selected histories, including archived/multi-segment examples, and a dedicated recovery destination. Close each verification App Server and start a fresh one; paginate oldest to newest and compare thread identity, turn order/status, visible content and intended project/cwd. Recover a second verified generation, then roll back using its active generation; cold-read the previous generation again. Verify the source and normal desktop profile remain untouched.
3. **Markdown/Obsidian SHA.** Export a new synthetic conversation note, sync without edits and compare SHA-256 at both ends. Confirm visible text and incomplete/failed turn statuses are represented as expected; do not interpret Markdown as a native backup.
4. **Real isolated bidirectional/offline test.** Use two authorized, isolated native test profiles and a separately configured private exchange transport, not production homes. Exercise Windows → Mac and Mac → Windows, repeat for idempotence, disconnect one device, append a completed synthetic turn on the other, reconnect and verify catch-up by native cold read. Reverse roles and repeat. Check pre-existing target chats remain unchanged. Then test across genuinely separate networks; two directories on one host are not WAN evidence.
5. **Fault injection and stop conditions.** In disposable copies, test partial packets, bad hashes, divergent same-ID histories, unfinished turns, missing segments, invalid offsets, unsupported builds, occupied writer locks, baseline changes, restart/interrupted apply and account/pair mismatch. Confirm refusals preserve evidence and verified heads, pending work reconciles without duplicate turns, and conflicts never merge automatically. Retain only sanitized result summaries for publication.

Generate a disposable fixture and exercise hard process exit before commit, retry, deduplication and rollback with the actual App Server:

```text
python -m tools.history_fixture build NEW-FIXTURE-DIRECTORY
python -m tools.history_fixture exercise NEW-FIXTURE-DIRECTORY NATIVE-CODEX-EXECUTABLE
```

For a physical two-host rehearsal, copy the same fixture to both hosts and run `python -m tools.dual_host_acceptance --help`. Start the helper with role `windows` on one host and `mac` on the other. Give each a fresh local `--root`, point `--control` to the same new directory in an existing trusted private transfer channel, and provide the local fixture, binaries, tailnet IP and separate unused data and GUI ports. The helper builds a second Syncthing connection, stops and restarts the macOS test instance, exchanges one new turn in each direction, and verifies truncated data, divergent history, simulated disk exhaustion and lock contention. Apply these checks to the exact builds and paths intended for a live deployment.
