"""Pure validation of an explicitly supplied, ancestor-first paginated lineage.

No discovery, decompression, filesystem access, turn-completion checks or native
recovery occurs here. Inventory paths are lexical paths relative to CODEX_HOME;
bytes must already be decoded JSONL. Only same-logical-ID linear histories are
supported. Validation proves internal consistency, NOT source authenticity.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from uuid import UUID

from history_stage import _fingerprint, _object


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_NAME = re.compile(
    rf"rollout-(?P<stamp>\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-"
    rf"(?P<logical>{_UUID})(?:_(?P<physical>{_UUID}))?\.jsonl\Z")
_DEPENDENCIES = {"history_revision", "revision", "revision_id", "parent_revision",
                 "parent_revision_id", "previous_segment", "history_segments", "segment_id"}
_KINDS = {"session_meta", "turn_context", "response_item", "event_msg",
          "world_state", "token_usage_record", "retained_context", "compacted",
          "inter_agent_communication_metadata"}


@dataclass(frozen=True)
class HistorySegment:
    path: Path
    physical_id: str
    raw: bytes
    records: tuple[dict, ...]
    line_ends: tuple[int, ...]


@dataclass(frozen=True)
class HistoryChain:
    thread_id: str
    segments: tuple[HistorySegment, ...]
    # Effective inherited prefix plus leaf, including physical session headers.
    # Segments retain even records beyond a child's cutoff for faithful mapping.
    records: tuple[dict, ...]


def _canonical_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _path(value, thread_id):
    if not isinstance(value, (str, Path)):
        raise ValueError("Expected a relative rollout path")
    text = str(value)
    # Reject both platforms' escape syntax on either host, before normalization.
    parts = text.replace("\\", "/").split("/")
    if (PureWindowsPath(text).drive or PurePosixPath(text).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or any(any(ord(c) < 32 or c in ':<>\"|?*' for c in part)
                   or part.endswith((" ", ".")) for part in parts)
            or parts[0] not in {"sessions", "archived_sessions"}):
        raise ValueError("Unsafe or out-of-scope rollout path")
    # Native relative layouts only; arbitrary nested directories are unproven.
    if len(parts) not in {2, 5}:
        raise ValueError("Unsupported rollout directory layout")
    if len(parts) == 5:
        date = "/".join(parts[1:4])
        if not re.fullmatch(r"\d{4}/\d{2}/\d{2}", date):
            raise ValueError("Unsupported rollout date directory")
        try:
            datetime.strptime(date, "%Y/%m/%d")
        except ValueError as exc:
            raise ValueError("Invalid rollout date directory") from exc
    match = _NAME.fullmatch(parts[-1])
    if match is None or match["logical"] != thread_id:
        raise ValueError("Invalid rollout name or logical ID")
    try:
        datetime.strptime(match["stamp"], "%Y-%m-%dT%H-%M-%S")
    except ValueError as exc:
        raise ValueError("Invalid rollout timestamp") from exc
    return Path(*parts), match["physical"] or thread_id


def _constant(_):
    raise ValueError("Invalid JSON constant")


def _records(raw):
    if not isinstance(raw, bytes) or not raw or not raw.endswith(b"\n"):
        raise ValueError("Expected complete decoded JSONL bytes ending in newline")
    records, ends, offset = [], [], 0
    # Split ONLY on LF. Unicode separators are not native byte boundaries.
    for line in raw.split(b"\n")[:-1]:
        offset += len(line) + 1
        try:
            record = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                                parse_constant=_constant)
            # Also rejects numeric overflow (e.g. 1e999) and lone surrogates.
            _fingerprint(record).encode("utf-8")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid UTF-8 JSONL record") from exc
        if (not isinstance(record, dict) or not isinstance(record.get("payload"), dict)
                or not isinstance(record.get("type"), str) or record["type"] not in _KINDS):
            raise ValueError("Unsupported record envelope")
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("Invalid top-level ordinal")
        if records and ordinal != records[-1]["ordinal"] + 1:
            raise ValueError("Non-contiguous segment ordinals")
        kind, payload = record["type"], record["payload"]
        if (not records and kind != "session_meta") or (records and kind == "session_meta"):
            raise ValueError("Expected exactly one leading session_meta")
        if kind in {"session_meta", "turn_context"}:
            if not isinstance(payload.get("cwd"), str) or not payload["cwd"]:
                raise ValueError("Missing structured cwd")
        if kind == "event_msg":
            if not isinstance(payload.get("type"), str):
                raise ValueError("Invalid event type")
            if payload["type"] in {"compacted", "history_compacted", "thread_rolled_back"}:
                raise ValueError("Unsupported compaction or rollback")
        records.append(record)
        ends.append(offset)
    return tuple(records), tuple(ends)


def _cutoff(parent, base):
    offset, ordinal = base["end_byte_offset"], base["end_ordinal_exclusive"]
    if type(offset) is not int or type(ordinal) is not int or offset <= 0 or ordinal <= 0:
        raise ValueError("Invalid history_base boundary")
    try:
        index = parent.line_ends.index(offset)
    except ValueError as exc:
        raise ValueError("history_base cutoff is not a raw record boundary") from exc
    if parent.records[index]["ordinal"] + 1 != ordinal:
        raise ValueError("history_base ordinal does not match cutoff record")
    return index + 1


def parse_chain(thread_id, inventory):
    """Validate a closed linear inventory of (relative path, decoded bytes).

    A root starts at ordinal zero. A child header starts at its history_base's
    exclusive ordinal, references the immediately preceding physical ID, and
    inherits through exactly the corresponding raw line end. Ancestors may have
    a non-inherited tail; all supplied bytes are still syntax-validated. An open
    or interrupted turn is valid evidence and is never filtered out.
    """
    if not _canonical_id(thread_id):
        raise ValueError("Expected canonical logical thread UUID")
    segments, effective, paths, identities = [], [], set(), set()
    for value, raw in inventory:
        path, physical = _path(value, thread_id)
        key = path.as_posix().casefold()
        if key in paths or physical in identities:
            raise ValueError("Duplicate path or physical ID (possible cycle)")
        records, ends = _records(raw)
        meta = records[0]["payload"]
        if meta.get("id") != thread_id or meta.get("history_mode") != "paginated":
            raise ValueError("Expected same-logical-ID paginated session")
        if any(meta.get(key) is not None for key in _DEPENDENCIES):
            raise ValueError("Unsupported lineage dependency")
        base = meta.get("history_base")
        if not segments:
            if base is not None or records[0]["ordinal"] != 0:
                raise ValueError("Missing standalone lineage root")
        else:
            if (not isinstance(base, dict) or set(base) != {
                    "thread_id", "end_byte_offset", "end_ordinal_exclusive"}
                    or base["thread_id"] != segments[-1].physical_id):
                raise ValueError("Missing, forged or out-of-order physical dependency")
            count = _cutoff(segments[-1], base)
            if records[0]["ordinal"] != base["end_ordinal_exclusive"]:
                raise ValueError("Child ordinal does not continue inherited prefix")
            # The previous segment is currently the leaf, hence its full records
            # occupy the end of effective. Replace only its non-inherited tail.
            del effective[len(effective) - len(segments[-1].records) + count:]
        segment = HistorySegment(path, physical, raw, records, ends)
        segments.append(segment)
        effective.extend(records)
        paths.add(key)
        identities.add(physical)
    if not segments:
        raise ValueError("Empty lineage inventory")
    return HistoryChain(thread_id, tuple(segments), tuple(effective))


def _checked(chain):
    # Dataclass dictionaries are intentionally convenient for native callers;
    # never trust a mutated parsed view over the authoritative supplied bytes.
    return parse_chain(chain.thread_id, ((s.path, s.raw) for s in chain.segments))


def _without_paths(records):
    result = copy.deepcopy(records)
    for record in result:
        kind, payload = record["type"], record["payload"]
        fields = None
        if kind in {"session_meta", "turn_context"}:
            fields = payload
        elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
            fields = payload.get("thread_settings")
            if not isinstance(fields, dict):
                raise ValueError("Invalid structured thread settings")
        if fields is not None:
            if "cwd" in fields:
                if not isinstance(fields["cwd"], str) or not fields["cwd"]:
                    raise ValueError("Invalid mapped cwd")
                fields["cwd"] = "<cwd>"
            if kind != "turn_context" and "runtime_workspace_roots" in fields:
                roots = fields["runtime_workspace_roots"]
                if not isinstance(roots, list) or any(not isinstance(r, str) or not r for r in roots):
                    raise ValueError("Invalid mapped workspace roots")
                # Keep cardinality: callbacks must not add/remove roots.
                fields["runtime_workspace_roots"] = ["<root>" for _ in roots]
    return _fingerprint(result)


def _encode(records):
    return b"".join((json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                    for r in records)


def map_chain(chain, map_records):
    """Map each segment with a records -> records callback, then revalidate.

    Bind history_daemon.map_records's mappings argument in the callback. Only
    its structured cwd/root fields may change. This function alone recomputes
    dependency offsets; callbacks must preserve all lineage/ordinal evidence.
    """
    chain = _checked(chain)
    inventory = []
    mapped_parent = None
    for segment in chain.segments:
        original = list(segment.records)
        mapped = map_records(copy.deepcopy(original))
        if not isinstance(mapped, (list, tuple)):
            raise ValueError("Mapping callback must return records")
        mapped = copy.deepcopy(list(mapped))
        if _without_paths(original) != _without_paths(mapped):
            raise ValueError("Mapping callback changed non-path evidence")
        if mapped_parent is not None:
            base = original[0]["payload"]["history_base"]
            count = _cutoff(chain.segments[len(inventory) - 1], base)
            mapped[0]["payload"]["history_base"]["end_byte_offset"] = mapped_parent.line_ends[count - 1]
        raw = _encode(mapped)
        records, ends = _records(raw)
        mapped_parent = HistorySegment(segment.path, segment.physical_id, raw, records, ends)
        inventory.append((segment.path, raw))
    return parse_chain(chain.thread_id, inventory)


def _evidence(chain):
    result = []
    for index, segment in enumerate(chain.segments):
        count = len(segment.records)
        if index + 1 < len(chain.segments):
            count = _cutoff(segment, chain.segments[index + 1].records[0]["payload"]["history_base"])
        for record in segment.records[:count]:
            record = copy.deepcopy(record)
            if record["type"] == "session_meta" and record["payload"].get("history_base") is not None:
                # Serialization may differ; both offsets were independently
                # proven to refer to this same ordinal in the same parent.
                record["payload"]["history_base"].pop("end_byte_offset")
            result.append((segment.path, segment.physical_id, _fingerprint(record)))
    return result


def is_forward(current, incoming):
    """Whether current is an exact effective-evidence prefix of incoming.

    Equality is a safe no-op (True). Forks, lost current tails and different
    physical identities/paths return False. Invalid inputs raise ValueError.
    Compare after mapping both chains into the same target path namespace.
    This is a read-only prefix primitive, not authorization to deploy/recover.
    """
    current, incoming = _checked(current), _checked(incoming)
    if current.thread_id != incoming.thread_id:
        return False
    before, after = _evidence(current), _evidence(incoming)
    return len(before) <= len(after) and before == after[:len(before)]
