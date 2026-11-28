"""Explicit-profile scheduler; no profile writes except through history_live.

Run: python history_daemon.py --config /absolute/config.json [--once]
Required config: home, exchange, state_dir, app_cli (absolute paths), device,
peer, pair_id, account_fingerprint, path_maps ([{"from": ..., "to": ...}]).
Optional interval_seconds=60, batch_size=8. Each device owns
exchange/<device>/ready.json and <thread UUID>/<original SHA-256>/ packets.
Only matching reciprocal ready manifests permit history publication/reception.
State is independent of the profile; busy/stale/pending-index are retried.
"""
import argparse
from collections import Counter
from contextlib import closing
import copy
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import time
from uuid import UUID

from history_exchange import _child, _packet, _same_packet
from history_stage import _fingerprint, _object, _parse, _read
from history_stage import export_history, stage_fast_forward
from history_storage import file_signature, read_rollout, resolve_rollout


class MappingError(ValueError):
    pass


class UnsupportedHistory(ValueError):
    pass


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)


def _save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _pure(value):
    if not isinstance(value, str):
        raise MappingError("invalid-path")
    if value.startswith("\\\\?\\") and value[5:7] == ":\\":
        value = value[4:]
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise MappingError("non-absolute-path")
    return path


def map_path(value, mappings, missing_projects=None):
    """Longest component prefix, respecting the input OS's case semantics."""
    source = _pure(value)
    matches = []
    for item in mappings:
        root = _pure(item["from"])
        if type(source) is not type(root):
            continue
        try:
            suffix = source.relative_to(root)
        except ValueError:
            continue
        target = Path(item["to"])
        if not target.is_absolute():
            raise MappingError("missing-map-root")
        # Do not reinterpret a peer component as local path syntax.
        if any("\\" in part or ":" in part for part in suffix.parts):
            raise MappingError("unsafe-map-component")
        matches.append((len(root.parts), target, suffix.parts, item.get("create_projectless", False)))
    if not matches:
        raise MappingError("unmapped-path")
    matches.sort(key=lambda entry: entry[0], reverse=True)
    length, root, parts, create_projectless = matches[0]
    targets = {(str(target.joinpath(*tail)), create) for size, target, tail, create in matches if size == length}
    if len(targets) != 1:
        raise MappingError("ambiguous-map")
    target = root.joinpath(*parts)
    if not root.is_dir() or not target.resolve().is_relative_to(root.resolve()):
        raise MappingError("missing-or-escaping-target")
    if not target.is_dir():
        if missing_projects is None or not create_projectless or len(parts) != 2 or target.exists():
            raise MappingError("missing-or-escaping-target")
        try:
            day, slug = parts
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("Noncanonical date")
            checked = _child(root, f"{day}/{slug}")
            if checked.is_junction() or checked.parent.is_junction():
                raise ValueError("Junction in generated workspace path")
        except ValueError as exc:
            raise MappingError("missing-or-escaping-target") from exc
        missing_projects.add(checked)
    return str(target)


def map_records(records, mappings, missing_projects=None):
    """Change only structured cwd/root fields; preserve all other evidence."""
    result = copy.deepcopy(records)
    for record in result:
        payload = record["payload"]
        fields = None
        if record["type"] in {"session_meta", "turn_context"}:
            fields = payload
        elif (record["type"] == "event_msg"
              and payload.get("type") == "thread_settings_applied"):
            fields = payload.get("thread_settings")
            if not isinstance(fields, dict):
                raise MappingError("invalid-settings")
        if fields is not None:
            if "cwd" in fields:
                fields["cwd"] = map_path(fields["cwd"], mappings, missing_projects)
            if record["type"] != "turn_context" and "runtime_workspace_roots" in fields:
                roots = fields["runtime_workspace_roots"]
                if not isinstance(roots, list):
                    raise MappingError("invalid-roots")
                fields["runtime_workspace_roots"] = [map_path(root, mappings, missing_projects) for root in roots]
    return result


def _encode(records):
    return b"".join((json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                    for r in records)


def _canonical(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def service_paths(config):
    paths = {}
    for name in ("home", "exchange", "state_dir", "app_cli"):
        path = Path(config[name])
        if not path.is_absolute():
            raise ValueError("Explicit absolute paths required")
        paths[name] = path.resolve()
    for first, second in (("home", "exchange"), ("home", "state_dir"), ("exchange", "state_dir")):
        a, b = paths[first], paths[second]
        if a.is_relative_to(b) or b.is_relative_to(a):
            raise ValueError("Home, exchange and state must be disjoint")
    return paths


class Daemon:
    def __init__(self, config, apply=None):
        self.config = config
        for name, path in service_paths(config).items():
            setattr(self, name, path)
        for key in ("device", "peer"):
            value = config[key]
            if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
                raise ValueError("Invalid device token")
            _child(self.exchange, value)
        if config["device"] == config["peer"]:
            raise ValueError("Devices must differ")
        for key in ("pair_id", "account_fingerprint"):
            if not isinstance(config[key], str) or not config[key]:
                raise ValueError("Missing pairing identity")
        if not isinstance(config["path_maps"], list):
            raise ValueError("path_maps must be a list")
        for mapping in config["path_maps"]:
            _pure(mapping["from"])
            if not Path(mapping["to"]).is_absolute():
                raise ValueError("Map target must be absolute")
            if type(mapping.get("create_projectless", False)) is not bool:
                raise ValueError("Invalid projectless workspace policy")
        self.batch = config.get("batch_size", 8)
        self.interval = config.get("interval_seconds", 60)
        self.compressed = config.get("sync_compressed", False)
        self.decompressor = config.get("decompressor")
        if type(self.compressed) is not bool:
            raise ValueError("Invalid compressed sync mode")
        if self.decompressor is not None and (not Path(self.decompressor).is_absolute()
                                             or not Path(self.decompressor).is_file()):
            raise ValueError("Configured decompressor must exist at an absolute path")
        if (type(self.batch) is not int or not 1 <= self.batch <= 128
                or type(self.interval) not in (int, float) or not 1 <= self.interval <= 86400):
            raise ValueError("Invalid scheduler limits")
        self.apply = apply
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "daemon-state.json"
        # Bind cursors/receipts to this exact configuration, without persisting identity plaintext.
        identity = hashlib.sha256(_fingerprint(config).encode()).hexdigest()
        self.state = _json(self.state_file) if self.state_file.exists() else {}
        if self.state.get("identity") != identity:
            self.state = {"identity": identity, "out_cursor": "", "in_cursor": "", "applied": {}, "peer_heads": {}}
        self.out_checked = self.state.setdefault("out_checked", {})
        self.in_checked = self.state.setdefault("in_checked", {})

    def _ready(self):
        c = self.config
        own = _child(self.exchange, c["device"])
        own.mkdir(parents=True, exist_ok=True)
        expected = {"version": 2 if self.compressed else 1, "device": c["device"], "peer": c["peer"],
                    "pair_id": c["pair_id"], "account_fingerprint": c["account_fingerprint"]}
        ready = _child(own, "ready.json")
        if not ready.exists() or _json(ready) != expected:
            _save(ready, expected)
        remote = _child(_child(self.exchange, c["peer"]), "ready.json")
        return remote.exists() and _json(remote) == {
            **expected, "device": c["peer"], "peer": c["device"]}

    def _db(self):
        return sqlite3.connect((self.home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)

    def _local_path(self, row):
        thread, rollout, cwd, mode, archived = row
        if not _canonical(thread) or mode != "paginated":
            raise UnsupportedHistory("unsupported-index")
        if rollout.startswith("\\\\?\\") and rollout[5:7] == ":\\":
            rollout = rollout[4:]
        path = Path(rollout)
        if not path.is_absolute() or not path.name.endswith((".jsonl", ".jsonl.zst")):
            raise UnsupportedHistory("unsupported-segment")
        plain, physical = resolve_rollout(path)
        plain = plain.resolve()
        physical = physical.resolve(strict=True)
        relative = plain.relative_to(self.home)
        if (relative.parts[0] not in {"sessions", "archived_sessions"}
                or not physical.is_relative_to(self.home)):
            raise ValueError("outside-history")
        if physical.suffix == ".zst" and not self.compressed:
            raise UnsupportedHistory("unsupported-segment")
        return physical, relative

    def _local(self, thread, path):
        try:
            raw, records, turns, timeline = _parse(read_rollout(path, decompressor=self.decompressor))
        except ValueError as exc:
            raise UnsupportedHistory("unsupported-or-incomplete-rollout") from exc
        if records[0]["payload"]["id"] != thread:
            raise ValueError("wrong-local-id")
        return raw

    def _publish(self, row):
        thread = row[0]
        path, _ = self._local_path(row)
        signature = [str(path), *file_signature(path)]
        parent = _child(_child(self.exchange, self.config["device"]), thread)
        head = _child(parent, "head.json")
        previous = _json(head)["sha256"] if head.is_file() else None
        if previous is not None and self.out_checked.get(thread) == [signature, previous]:
            return "reused"
        raw = self._local(thread, path)
        sha = hashlib.sha256(raw).hexdigest()
        if [str(path), *file_signature(path)] != signature:
            raise ValueError("export-changed")
        parent.mkdir(parents=True, exist_ok=True)
        destination = _child(parent, sha)
        if previous == sha:
            if self._read_packet(parent, previous)["raw"] != raw:
                raise ValueError("outbox-corrupt")
            self.out_checked[thread] = [signature, sha]
            return "reused"
        if previous is not None and not raw.startswith(self._read_packet(parent, previous)["raw"]):
            raise ValueError("source-fork")
        if destination.exists():
            packet = _packet(destination, thread, decompressor=self.decompressor)
            if packet["raw"] != raw:
                raise ValueError("outbox-corrupt")
        else:
            with TemporaryDirectory(dir=self.state_dir, prefix="export-") as temp:
                bundle = Path(temp) / "packet"
                receipt = export_history(self.home, thread, bundle, decompressor=self.decompressor,
                                         packed=self.compressed)
                if receipt["sha256"] != sha:
                    raise ValueError("export-changed")
                with TemporaryDirectory(dir=parent, prefix=".publishing-") as publishing:
                    prepared = Path(publishing) / "packet"
                    shutil.copytree(bundle, prepared)
                    if destination.exists():
                        _same_packet(destination, _packet(bundle, thread, decompressor=self.decompressor), thread,
                                     decompressor=self.decompressor)
                    else:
                        prepared.rename(destination)
        _save(head, {"sha256": sha, "previous": previous})
        self.out_checked[thread] = [signature, sha]
        return "published"

    def _read_packet(self, directory, name):
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}", name):
            raise ValueError("invalid-packet-name")
        packet = _packet(_child(directory, name), directory.name, decompressor=self.decompressor)
        if packet["receipt"]["sha256"] != name:
            raise ValueError("wrong-packet-hash")
        return packet

    def _receive(self, directory):
        thread = directory.name
        head = _child(directory, "head.json")
        if not head.is_file():
            return "empty"
        pointer = _json(head)
        source_sha = pointer["sha256"]
        with closing(self._db()) as db:
            row = db.execute("SELECT id,rollout_path,cwd,history_mode,archived FROM threads WHERE id=?",
                             (thread,)).fetchone()
        signature = None
        if row is not None:
            path, relative = self._local_path(row)
            signature = [str(path), *file_signature(path)]
            if self.in_checked.get(thread) == [pointer, signature]:
                return "unchanged"
        packet = self._read_packet(directory, source_sha)
        verified = self.state["peer_heads"].get(thread)
        for older in {pointer["previous"], verified} - {None, source_sha}:
            predecessor = self._read_packet(directory, older)["raw"]
            if older == verified and predecessor.startswith(packet["raw"]):
                return "stale"
            if not packet["raw"].startswith(predecessor):
                raise ValueError("source-fork")
        self.state["peer_heads"][thread] = source_sha
        baseline = None
        if row is None and Path(packet["receipt"]["path"]).parts[0] == "archived_sessions":
            # Native resume cannot initialize a newly received archived file.
            return "archived-recovery"
        if row is not None:
            raw = self._local(thread, path)
            if raw == packet["raw"]:
                if [str(path), *file_signature(path)] != signature:
                    return "stale"
                self.in_checked[thread] = [pointer, signature]
                return "unchanged"
            baseline = hashlib.sha256(raw).hexdigest()
        with TemporaryDirectory(dir=self.state_dir, prefix="receive-") as temp:
            temp = Path(temp)
            original = temp / "original.jsonl"
            original.write_bytes(packet["raw"])
            missing_projects = set() if row is None else None
            mapped = map_records(_read(original)[1], self.config["path_maps"], missing_projects)
            if missing_projects:
                for directory in sorted(missing_projects):
                    directory.mkdir(parents=True, exist_ok=True)
            source = temp / "mapped.jsonl"
            source.write_bytes(_encode(mapped))
            staged = temp / "candidate.jsonl"
            cwd = Path(mapped[0]["payload"]["cwd"])
            if row is not None:
                target = temp / "baseline.jsonl"
                target.write_bytes(raw)
                missing = stage_fast_forward(source, target, staged, cwd)
                if not missing:
                    if [str(path), *file_signature(path)] == signature:
                        self.in_checked[thread] = [pointer, signature]
                    return "unchanged"
                if relative.parts[0] == "archived_sessions":
                    return "archived-recovery"
                if path.suffix == ".zst":
                    from history_live import materialize_for_sync
                    return materialize_for_sync(self.home, thread, relative, cwd, self.app_cli)
            else:
                relative = Path(packet["receipt"]["path"])
                staged.write_bytes(source.read_bytes())
            candidate, records, turns, timeline = _read(staged)
            expected = [turn[0]["payload"]["turn_id"] for turn in turns]
            candidate_sha = hashlib.sha256(candidate).hexdigest()
            receipt_key = source_sha + ":" + candidate_sha
            if self.state["applied"].get(thread) == receipt_key:
                return "already-applied"
            apply = self.apply
            if apply is None:
                from history_live import apply_rollout
                apply = apply_rollout
            status = apply(home=self.home, thread_id=thread, relative=relative,
                           staged=staged, cwd=cwd, expected_turn_ids=expected,
                           baseline_sha256=baseline, backup_dir=self.state_dir / "backups",
                           app_cli=self.app_cli)
            if status not in {"applied", "unchanged", "busy", "stale", "pending-index"}:
                raise ValueError("invalid-apply-status")
            if status in {"applied", "unchanged"}:
                self.state["applied"][thread] = receipt_key
            return status

    def tick(self):
        counts = Counter()
        details = []

        def process(kind, identifier, operation):
            try:
                counts[kind + ":" + operation()] += 1
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error, ImportError) as exc:
                # Never echo exceptions: paths, contents and account details may be embedded.
                category = ("mapping-skip" if isinstance(exc, MappingError) else
                            "unsupported" if isinstance(exc, UnsupportedHistory) else "rejected")
                counts[kind + ":" + category] += 1
                if len(details) < 8:
                    safe_reasons = {"source-fork", "target-fork", "packet-limit", "wrong-packet-hash",
                                    "invalid-packet-name", "unmapped-path", "missing-or-escaping-target",
                                    "ambiguous-map", "unsupported-index", "unsupported-segment",
                                    "unsupported-or-incomplete-rollout"}
                    reason = str(exc) if str(exc) in safe_reasons else category
                    details.append({"id": identifier, "reason": reason,
                                    "error_type": type(exc).__name__})

        if not self._ready():
            return {"counts": {"waiting-pair": 1}, "details": []}
        with closing(self._db()) as db:
            rows = db.execute("SELECT id,rollout_path,cwd,history_mode,archived FROM threads "
                              "WHERE id>? ORDER BY id LIMIT ?",
                              (self.state["out_cursor"], self.batch)).fetchall()
        for row in rows:
            process("out", row[0] if _canonical(row[0]) else "invalid-id", lambda row=row: self._publish(row))
        self.state["out_cursor"] = rows[-1][0] if len(rows) == self.batch else ""
        peer = _child(self.exchange, self.config["peer"])
        # Directory enumeration is metadata-only; history reads are bounded per tick.
        candidates = sorted(p.name for p in peer.iterdir()
                            if _canonical_safe(p.name) and p.name > self.state["in_cursor"])
        selected = candidates[:self.batch]
        for thread in selected:
            process("in", thread, lambda thread=thread: self._receive(_child(peer, thread)))
        self.state["in_cursor"] = selected[-1] if len(selected) == self.batch else ""
        _save(self.state_file, self.state)
        return {"counts": dict(counts), "details": details}


def _canonical_safe(value):
    try:
        return _canonical(value)
    except (ValueError, TypeError, AttributeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.config.is_absolute():
            raise ValueError("Explicit config path required")
        daemon = Daemon(_json(args.config))
        while True:
            failed = False
            try:
                result = daemon.tick()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                result = {"counts": {"scheduler-error": 1}}
                failed = True
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once:
                return 1 if failed else 0
            time.sleep(daemon.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print(json.dumps({"counts": {"scheduler-error": 1}}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
