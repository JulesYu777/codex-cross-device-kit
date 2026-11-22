# Windows → Mac native chat migration design

[English](native-history-migration.md) · [简体中文](native-history-migration.zh-CN.md)

This design specifies an optional, one-time migration step for the kit. Users export selected Windows chats and map projects on the Mac. The tool copies native history, adapts the working paths, and verifies each result in local Codex on the Mac. Project files continue to use the kit's existing Syncthing flow. Batch migration requires an explicit selection.

## User flow

| Step | Proposed command | Result |
| --- | --- | --- |
| Export on Windows | `py -3 kit.py history export --select selection.json --out BUNDLE` | Creates a local bundle with selected native chats and required history segments |
| Plan on Mac | `python3 kit.py history plan BUNDLE --map workspace-map.json --out PLAN` | Reports chats eligible for import, already present, eligible for repair, or skipped |
| Apply on Mac | `python3 kit.py history apply PLAN` | Backs up Mac data, applies verified entries, and reads them back through native Codex |

Set up Mac projects first using a verified Git repository or local directory. Transfer the bundle privately. When the Mac app is idle, the apply step quits and reopens it normally. If another chat is active, the step waits or reports that work must finish. After reopening, it opens the original chat in Codex's local view on the Mac.

## Export, path mapping, and isolated verification

- **Windows source.** The selection accepts IDs for local native chats. Use the source Codex to read selected chats, all paginated turns, available project membership, and archive state. Copy the selected native history and its full segment dependencies. The manifest records the format version, source build, original chat IDs, project roots, relative archive paths, byte sizes, SHA-256 digests, and each chat's source turn and content fingerprints. Wait for active or changing source records to settle, then export them again.
- **Bundle.** Enforce an allowed file inventory, accepted file types, and extraction size bounds. Verify manifest hashes and reject traversal, duplicate paths, and symlinks. Export selected history and its required metadata. Account caches, authentication files, configuration, and project media stay on their respective hosts. Store and transfer the bundle with the same privacy as the chats it contains.
- **Mac mapping.** Map Windows project roots to existing absolute Mac paths. For Git projects, verify identity using the normalized remote. Disambiguate matching project names using the root and the Git remote. Match the longest registered prefix by path component. Use the verified Mac project root for the working directory and accept only mapped, existing Mac directories as extra roots. Use an empty array when there are no extra roots. Skip entries with ambiguous or missing primary mappings and report the missing mapping.
- **Native records.** Change the session restoration metadata for the destination host and preserve the original chat ID. Keep messages, tool records, and the historical evidence paths in those records. Preserve dependency segments and referenced byte offsets. For verified segmented formats, replace the active segment's first record at its original length, padding with spaces. Skip the entry if the record cannot fit or a segment link fails verification.
- **Isolated readback.** Restore with the installed Mac App Server in a separate, offline copy. Paginate all turns to verify the original ID, order, statuses, stable content fingerprints, working directory, and intended project. Collect the full segment lineage first. Apply entries only for verified build pairs and formats. Run compatibility checks before adding support for new build pairs. [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)

## Applying and recovering

- **Matching IDs.** Import if the Mac has no matching ID. Treat a matching fingerprint that already opens as present. Repair an existing Mac record when its fingerprint matches and workspace restoration is the sole failure. Skip entries with differing fingerprints or incomplete evidence and preserve both histories. A fresh plan after import must classify completed entries as already present.
- **Project membership.** Prefer the verified local project ID or working directory association. Create or attach a project only through native project interfaces verified for the build, then read back the project listing. Skip entries that cannot be verified in their intended existing project. Restore archive state through a validated interface. Enable sections, pins, and native attachments after each passes compatibility checks.
- **Backup and apply.** Bind the plan to the source manifest hash, path map hash, Codex builds, and Mac baseline. Recheck these before application and regenerate the plan if they differ. Confirm relevant Mac processes are stopped and records are unchanged, then back up and verify the affected history and indexes. Build native indexes using the verified local App Server with a Mac working directory. Journal staged writes and indexing actions. On failure, roll back the batch before reopening the desktop. If new Mac writes are detected after reopening, preserve them and stop automatic replacement.
- **Acceptance.** The local Mac chat list can find and open the original ID in its intended project. The oldest and newest turns are visible, and existing Mac chats and Windows source records remain available. Write a per-chat receipt with imported, repaired, already present, skipped, or rolled back results, reasons, and backup location. Keep message text and credentials out of the receipt.

## Release checks

1. Export public synthetic chats on Windows and import into both an empty Mac target and a target with existing chats. Verify the native list, original IDs, turns, and project membership.
2. Cover drive letters, Chinese characters and spaces in paths, different local Git directories, extra roots, no extra roots, repeated imports, and conflicting histories for the same ID. Exercise missing segments, invalid offsets, unsupported builds, concurrent changes, and interrupted apply. Verify skips and rollback.
3. Reopen the Mac desktop and verify a user-authorized real sample across supported versions. Use a separate synthetic chat to verify that a new turn can append after migration. List only build pairs that pass this process as publicly supported.

Existing evidence validates workspace repair when all segments are available. One real chat restored 59 turns with matching content and order in isolation and then opened natively on the Mac desktop. Other trials have reported partial replay or different segment projections, so preflight must block those entries. The migration commands, packaging, and rollback remain implementation work for this design.
