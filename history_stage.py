"""Offline, fail-closed staging for standalone paginated rollout JSONL.

Supported envelope: session_meta, turn_context, response_item, event_msg,
world_state and token_usage_record;
turn boundaries are task_started/task_complete (or turn_started/turn_completed).
This module neither discovers profiles nor applies staged history.
"""
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import UUID

from history_storage import file_signature, read_rollout, resolve_rollout


def _error(message):
    raise ValueError(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error("Ambiguous JSON: duplicate key")
        result[key] = value
    return result


def _without_ordinal(record):
    return {key: value for key, value in record.items() if key != "ordinal"}


def _fingerprint(value):
    # Python equality conflates True and 1; JSON evidence must not do so.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _read(path):
    return _parse(path.read_bytes())


def _parse(raw):
    """Validate decoded JSONL, preserving the existing _read return contract."""
    if not raw or not raw.endswith(b"\n"):
        _error("Incomplete JSONL: input must end in a newline")
    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                                parse_constant=lambda _: _error("Invalid JSON constant"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSONL record") from exc
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            _error("Unsupported record envelope")
        if not isinstance(record.get("type"), str):
            _error("Unsupported record type")
        if record["type"] == "event_msg" and not isinstance(record["payload"].get("type"), str):
            _error("Unsupported event type")
        ordinal = record.get("ordinal")
        if type(ordinal) is not int:
            _error("Unsupported or invalid top-level ordinal")
        if ordinal != len(records):
            _error("Non-contiguous ordinals")
        records.append(record)
    header = records[0]
    if header.get("type") != "session_meta":
        _error("Unsupported creation record; expected one session_meta")
    meta = header["payload"]
    dependencies = {"history_base", "history_revision", "revision", "revision_id",
                    "parent_revision", "parent_revision_id", "previous_segment",
                    "history_segments", "segment_id"}
    if meta.get("history_mode") != "paginated" or any(meta.get(k) is not None for k in dependencies):
        _error("Unsupported history: requires standalone paginated single segment")
    if not isinstance(meta.get("id"), str) or not meta["id"]:
        _error("Missing session ID")
    if not isinstance(meta.get("cwd"), str) or not meta["cwd"]:
        _error("Missing session working directory")
    turns, timeline, active, seen = [], [], None, set()
    starts = {"task_started": "task_complete", "turn_started": "turn_completed"}
    end_kind = None
    for record in records[1:]:
        kind, payload = record.get("type"), record["payload"]
        event = payload.get("type") if kind == "event_msg" else None
        if kind == "session_meta":
            _error("Ambiguous creation records")
        if kind not in {"event_msg", "turn_context", "response_item", "world_state", "token_usage_record"}:
            _error("Unsupported record type")
        if event in {"compacted", "history_compacted", "thread_rolled_back"}:
            _error("Unsupported compaction or rollback event")
        if kind == "turn_context" and (not isinstance(payload.get("cwd"), str) or not payload["cwd"]):
            _error("Unsupported or missing source turn_context cwd mapping")
        if event == "thread_settings_applied" and active is None:
            continue
        if event in starts:
            turn_id = payload.get("turn_id")
            if active is not None or not isinstance(turn_id, str) or not turn_id or turn_id in seen:
                _error("Ambiguous or duplicate turn start")
            seen.add(turn_id)
            active = [record]
            end_kind = starts[event]
        elif event in starts.values():
            if (active is None or event != end_kind
                    or payload.get("turn_id") != active[0]["payload"]["turn_id"]):
                _error("Unmatched turn completion")
            active.append(record)
            turns.append(active)
            timeline.extend(active)
            active = None
        elif active is not None:
            if "turn_id" in payload and payload["turn_id"] != active[0]["payload"]["turn_id"]:
                _error("Conflicting turn ID inside turn")
            active.append(record)
        else:
            # Unknown out-of-turn evidence must still match, not be discarded.
            if kind != "event_msg":
                _error("Partial turn outside completed boundaries")
            timeline.append(record)
    if active is not None:
        _error("Incomplete trailing turn")
    if not turns:
        _error("Target and source must already contain a completed turn")
    return raw, records, turns, timeline


def stage_fast_forward(source, target, output, cwd, *, source_root=None, target_root=None):
    """Validate explicit snapshots, exclusively create output, return missing IDs.

    No-op returns [] without creating output. cwd must be the target header's
    existing absolute local directory. Source appended contexts must use the
    source header cwd; multi-root mapping is intentionally unsupported.
    """
    source, target, output, cwd = map(Path, (source, target, output, cwd))
    if not cwd.is_absolute() or not cwd.is_dir():
        _error("Receiver cwd must be an existing absolute directory")
    if output.exists() or output.is_symlink():
        _error("Output must be a new explicit file")
    if output.resolve() in {source.resolve(), target.resolve()}:
        _error("Output must not replace an input")
    source_raw, source_records, source_turns, source_events = _read(source)
    target_raw, target_records, target_turns, target_events = _read(target)
    sm, tm = source_records[0]["payload"], target_records[0]["payload"]
    if sm["id"] != tm["id"]:
        _error("Different session IDs")
    if Path(tm["cwd"]).resolve() != cwd.resolve():
        _error("Receiver cwd does not match target session metadata")
    if (source_root is None) != (target_root is None):
        _error("Supply both source and target workspace roots")
    if target_root is not None:
        root = Path(target_root)
        if not root.is_absolute() or not root.is_dir():
            _error("Receiver workspace root must be an existing absolute directory")
        if sm.get("runtime_workspace_roots") != [source_root] or tm.get("runtime_workspace_roots") != [target_root]:
            _error("Explicit workspace roots do not match the session metadata")
    # Compare only the explicit source-to-target workspace mapping.
    sh, th = map(_without_ordinal, (source_records[0], target_records[0]))
    sh, th = copy.deepcopy(sh), copy.deepcopy(th)
    for header, meta, mapped_root in ((sh, sm, source_root), (th, tm, target_root)):
        header["payload"].pop("cwd")
        if meta.get("runtime_workspace_roots") == [mapped_root or meta["cwd"]]:
            header["payload"]["runtime_workspace_roots"] = []
    if _fingerprint(sh) != _fingerprint(th):
        _error("Conflicting creation metadata")

    def workspace_fingerprint(record, workspace):
        record = _without_ordinal(record)
        if record["type"] == "turn_context" and record["payload"].get("cwd") == workspace:
            record["payload"] = {**record["payload"], "cwd": None}
        return _fingerprint(record)

    se = [workspace_fingerprint(record, sm["cwd"]) for record in source_events]
    te = [workspace_fingerprint(record, tm["cwd"]) for record in target_events]
    shared = min(len(se), len(te))
    if se[:shared] != te[:shared]:
        _error("Conflicting event prefix (not a safe fast-forward)")
    if len(source_turns) <= len(target_turns):
        if len(se) > len(te):
            _error("Non-turn suffix is not a safe fast-forward")
        return []
    missing = source_turns[len(target_turns):]
    # Every non-settings source suffix record must belong to a missing turn.
    suffix = [record for turn in missing for record in turn]
    if source_events[len(target_events):] != suffix:
        _error("Non-turn suffix is not a safe fast-forward")
    appended = []
    ordinal = target_records[-1]["ordinal"]
    for record in suffix:
        record = copy.deepcopy(record)
        ordinal += 1
        record["ordinal"] = ordinal
        if record["type"] == "turn_context":
            if record["payload"].get("cwd") != sm["cwd"]:
                _error("Unsupported or missing source turn_context cwd mapping")
            record["payload"]["cwd"] = tm["cwd"]
        appended.append((json.dumps(record, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")) + "\n").encode("utf-8"))
    staged = target_raw + b"".join(appended)
    # Recheck snapshots immediately before the sole write, without touching profiles.
    if source.read_bytes() != source_raw or target.read_bytes() != target_raw:
        _error("Input changed during validation")
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            stream.write(staged)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            output.unlink()
        raise
    return [turn[0]["payload"]["turn_id"] for turn in missing]


def export_history(codex_home, thread_id, output, *, decompressor=None, packed=False):
    """Publish a checked read-only snapshot of one selected completed thread."""
    if str(UUID(thread_id)) != thread_id:
        _error("Expected the canonical thread ID")
    home, output = Path(codex_home).resolve(strict=True), Path(output)
    if output.exists() or output.is_symlink():
        _error("Bundle must be a new explicit directory")
    database = (home / "state_5.sqlite").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as db:
        selected = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if selected is None:
        _error("Thread ID is absent from the local index")
    path = Path(selected[0])
    if not path.is_absolute():
        _error("Expected an absolute local rollout path")
    if os.name == "nt" and path.drive.startswith("\\\\?\\") and len(path.drive) == 6:
        path = Path(str(path)[4:])
    canonical, source = resolve_rollout(path)
    # Check both independently: a compressed sibling can be a symlink even
    # when its absent canonical .jsonl name is safely inside the home.
    for candidate in (canonical, source):
        resolved = candidate.resolve()
        if (not resolved.is_relative_to(home)
                or not resolved.relative_to(home).parts
                or resolved.relative_to(home).parts[0] not in {"sessions", "archived_sessions"}):
            _error("Indexed rollout is outside the local history directories")
    relative = canonical.resolve().relative_to(home)
    signature = file_signature(source)
    raw, records, turns, _ = _parse(read_rollout(source, decompressor=decompressor))
    if records[0]["payload"]["id"] != thread_id:
        _error("Indexed rollout belongs to another thread")
    receipt = {"path": relative.as_posix(), "turn_ids": [t[0]["payload"]["turn_id"] for t in turns],
               "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    payload = raw
    snapshot_relative = relative
    compressed = source.name.endswith(".jsonl.zst")
    if packed and compressed:
        # Read original bytes only after validating the decoded standalone
        # history. Stat checks below bind both reads to the same source.
        with source.open("rb") as stream:
            payload = stream.read(signature[2] + 1)
        if len(payload) != signature[2]:
            _error("Indexed history changed during export")
        snapshot_relative = Path(str(relative) + ".zst")
        receipt.update(encoding="zstd", packed_bytes=len(payload),
                       packed_sha256=hashlib.sha256(payload).hexdigest())
    with TemporaryDirectory(prefix=".native-export-", dir=output.parent) as scratch:
        bundle = Path(scratch) / "bundle"
        snapshot = bundle / snapshot_relative
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(payload)
        (bundle / "manifest.json").write_text(json.dumps([receipt], indent=2) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(database, uri=True)) as db:
            current = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if (current != selected or file_signature(source) != signature
                or resolve_rollout(path) != (canonical, source)
                or (not compressed and source.read_bytes() != raw)):
            _error("Indexed history changed during export")
        if output.exists() or output.is_symlink():
            _error("Bundle must be a new explicit directory")
        bundle.rename(output)
    return receipt
