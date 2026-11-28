"""One selected thread, one exchange tick; never apply history to a profile."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import UUID

from history_stage import (_fingerprint, _object, _parse, _read, _without_ordinal,
                           export_history, stage_fast_forward)
from history_storage import read_rollout


def _child(root, relative):
    """Reject portable-path escapes, links and Windows junction escapes."""
    if not isinstance(relative, str):
        raise ValueError("Invalid manifest relative path")
    parts = relative.split("/")
    if (not relative or PurePosixPath(relative).is_absolute()
            or any(not p or p in {".", ".."} or p.endswith((".", " "))
                   or any(ord(c) < 32 or c in '<>:"\\|?*' for c in p)
                   or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", p.split(".")[0], re.I)
                   for p in parts)):
        raise ValueError("Unsafe manifest relative path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("Linked or escaping exchange path")
    return path


def _packet(bundle, thread_id, *, decompressor=None):
    manifest = _child(bundle, "manifest.json").read_bytes()
    receipts = json.loads(manifest, object_pairs_hook=_object)
    if not isinstance(receipts, list) or len(receipts) != 1 or not isinstance(receipts[0], dict):
        raise ValueError("Expected exactly one manifest entry")
    receipt = receipts[0]
    path = _child(bundle, receipt.get("path"))
    if (path.suffix != ".jsonl" or receipt["path"].split("/")[0]
            not in {"sessions", "archived_sessions"}):
        raise ValueError("Expected a standalone history JSONL")
    encoding = receipt.get("encoding")
    stored = path
    if encoding == "zstd":
        stored = _child(bundle, receipt["path"] + ".zst")
        with stored.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if (type(receipt.get("packed_bytes")) is not int or receipt["packed_bytes"] != stat.st_size
                or receipt.get("packed_sha256") != actual):
            raise ValueError("Packed manifest size or SHA-256 mismatch")
        raw = read_rollout(stored, decompressor=decompressor)
        raw, records, turns, timeline = _parse(raw)
    elif encoding is None:
        raw, records, turns, timeline = _read(stored)
    else:
        raise ValueError("Unsupported packet encoding")
    sha = hashlib.sha256(raw).hexdigest()
    ids = [turn[0]["payload"]["turn_id"] for turn in turns]
    if (type(receipt.get("bytes")) is not int or receipt["bytes"] != len(raw)
            or receipt.get("sha256") != sha or receipt.get("turn_ids") != ids
            or records[0]["payload"]["id"] != thread_id):
        raise ValueError("Manifest size, SHA-256, thread ID or completed turn IDs mismatch")
    # Only the original manifest and its one JSONL are allowed in a packet.
    for item in bundle.rglob("*"):
        _child(bundle, item.relative_to(bundle).as_posix())
        if not item.is_dir() and item not in {stored, bundle / "manifest.json"}:
            raise ValueError("Unexpected packet file")
    return {"receipt": receipt, "raw": raw, "manifest": manifest,
            "header": _fingerprint(_without_ordinal(records[0])),
            "events": [_fingerprint(_without_ordinal(r)) for r in timeline]}


def _same_packet(path, packet, thread_id, *, decompressor=None):
    existing = _packet(path, thread_id, decompressor=decompressor)
    if (existing["raw"], existing["manifest"]) != (packet["raw"], packet["manifest"]):
        raise ValueError("Immutable outbound packet already exists with different bytes")


def exchange_once(codex_home, thread_id, exchange, device, peer, stage_dir, cwd, *,
                  source_root=None, target_root=None, decompressor=None):
    """Export once and stage a unique compatible peer prefix; return a receipt.

    Bad packets block staging for this tick. All evidence is retained. The
    exchange root must exist; stage_dir must be outside it and the profile.
    """
    if str(UUID(thread_id)) != thread_id:
        raise ValueError("Expected the canonical thread ID")
    for name in (device, peer):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise ValueError("Device names must be short safe lowercase directory names")
    if device == peer:
        raise ValueError("Device and peer must be different")
    for value in (codex_home, exchange, stage_dir, cwd):
        text = str(value)
        if text.startswith(("\\\\", "//")) or "://" in text:
            raise ValueError("Expected explicit local filesystem paths, not remote paths")
    home = Path(codex_home).resolve(strict=True)
    exchange = Path(exchange).resolve(strict=True)
    stage_dir = Path(stage_dir).resolve()
    if not exchange.is_dir():
        raise ValueError("Exchange root must already be a directory")
    if (exchange.is_relative_to(home) or home.is_relative_to(exchange)
            or stage_dir.is_relative_to(home) or home.is_relative_to(stage_dir)
            or stage_dir.is_relative_to(exchange) or exchange.is_relative_to(stage_dir)):
        raise ValueError("Profile, exchange and staging directories must be separate")
    outbound = _child(exchange, f"{device}/{thread_id}")
    incoming = _child(exchange, f"{peer}/{thread_id}")
    with TemporaryDirectory(prefix="native-exchange-") as private:
        scratch = Path(private).resolve()
        receipt = export_history(home, thread_id, scratch / "local", decompressor=decompressor)
        local = _packet(scratch / "local", thread_id, decompressor=decompressor)
        baseline = receipt["sha256"]
        result = {"status": "no-op", "published": False, "baseline_sha256": baseline,
                  "source_sha256": None, "missing_turn_ids": [], "staged_path": None,
                  "rejected": []}
        candidates = []
        if incoming.exists():
            for bundle in sorted(incoming.iterdir()):
                try:
                    _child(incoming, bundle.name)
                    if not re.fullmatch(r"[0-9a-f]{64}", bundle.name) or not bundle.is_dir():
                        raise ValueError("Incomplete or invalid packet directory")
                    packet = _packet(bundle, thread_id, decompressor=decompressor)
                    if packet["receipt"]["sha256"] != bundle.name:
                        raise ValueError("Packet fingerprint mismatch")
                    candidates.append(packet)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result["rejected"].append({"packet": bundle.name, "reason": str(exc)})
        # Publish only to this device's outbox, preserving the original manifest.
        outbound.mkdir(parents=True, exist_ok=True)
        destination = _child(outbound, baseline)
        if destination.exists():
            _same_packet(destination, local, thread_id)
        else:
            with TemporaryDirectory(prefix=".publishing-", dir=outbound.parent) as temp:
                prepared = Path(temp) / "packet"
                shutil.copytree(scratch / "local", prepared)
                try:
                    prepared.rename(destination)
                    result["published"] = True
                except OSError:
                    if not destination.exists():
                        raise
                    _same_packet(destination, local, thread_id)
        result["outbound_path"] = str(destination)
        if result["rejected"]:
            result["status"] = "rejected"
            return result
        if not candidates:
            return result
        candidates.sort(key=lambda p: (len(p["receipt"]["turn_ids"]),
                                       len(p["events"]), p["receipt"]["sha256"]))
        longest = candidates[-1]
        try:
            # Every candidate must agree, including old evidence beyond our baseline.
            for packet in candidates:
                if (packet["header"] != longest["header"]
                        or packet["events"] != longest["events"][:len(packet["events"])]):
                    raise ValueError("Conflicting peer histories")
            missing, staged = [], None
            for index, packet in enumerate(candidates):
                source = scratch / f"peer-{index}.jsonl"
                source.write_bytes(packet["raw"])
                output = scratch / f"staged-{index}.jsonl"
                ids = stage_fast_forward(source, scratch / "local" / receipt["path"],
                                         output, cwd, source_root=source_root,
                                         target_root=target_root)
                if packet is longest:
                    missing, staged = ids, output
        except ValueError as exc:
            result.update(status="conflict", reason=str(exc))
            return result
        result["source_sha256"] = longest["receipt"]["sha256"]
        if not missing:
            return result
        stage_dir.mkdir(parents=True, exist_ok=True)
        output = _child(stage_dir, f"{thread_id}-{baseline}-{result['source_sha256']}.jsonl")
        expected = staged.read_bytes()
        # Exclusive, atomic publication on the staging filesystem. Never overwrite.
        with TemporaryDirectory(prefix=".exchange-stage-", dir=stage_dir) as temp:
            ready = Path(temp) / "ready.jsonl"
            with ready.open("xb") as stream:
                stream.write(expected)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(ready, output)
                status = "staged"
            except FileExistsError:
                if output.is_symlink() or output.read_bytes() != expected:
                    raise ValueError("Existing staged output differs; preserved for inspection")
                status = "duplicate"
        result.update(status=status, staged_path=str(output), missing_turn_ids=missing)
        return result
