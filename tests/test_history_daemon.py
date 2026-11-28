from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from history_daemon import Daemon, MappingError, map_path, map_records
from history_stage import _read, export_history


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-daemon-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.thread = "2184a532-266e-42a2-8a35-2eaae75020ac"
        self.home = self.root / "home"
        self.peer = self.root / "peer"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.exchange = self.root / "exchange"
        self.exchange.mkdir()
        self.calls = []
        self.config = dict(home=str(self.home), exchange=str(self.exchange),
                           state_dir=str(self.root / "state"), app_cli=str(self.root / "app"),
                           device="win", peer="mac", pair_id="fixture-pair",
                           account_fingerprint="fixture-fingerprint",
                           path_maps=[{"from": "/peer/project", "to": str(self.cwd)}])
        self.profile(self.home, str(self.cwd), [])
        self.profile(self.peer, "/peer/project", ["one", "two"])

    def records(self, cwd, turns):
        records = [{"type": "session_meta", "payload": {
            "id": self.thread, "cwd": cwd, "history_mode": "paginated"}}]
        for turn in turns:
            records.extend([
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                {"type": "turn_context", "payload": {"cwd": cwd, "turn_id": turn}},
                {"type": "response_item", "payload": {"text": "untouched /peer/project", "turn_id": turn}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}])
        return [{**record, "ordinal": i} for i, record in enumerate(records)]

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def profile(self, home, cwd, turns):
        home.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, "
                       "history_mode TEXT, archived INTEGER)")
            if turns:
                path = home / "sessions" / "rollout.jsonl"
                self.write(path, self.records(cwd, turns))
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                           (self.thread, str(path), cwd, "paginated", 0))
            db.commit()

    def local(self, turns):
        path = self.home / "sessions" / "rollout.jsonl"
        self.write(path, self.records(str(self.cwd), turns))
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)",
                       (self.thread, str(path), str(self.cwd), "paginated", 0))
            db.commit()
        return path

    def ready(self, **updates):
        directory = self.exchange / "mac"
        directory.mkdir(exist_ok=True)
        value = dict(version=1, device="mac", peer="win", pair_id=self.config["pair_id"],
                     account_fingerprint=self.config["account_fingerprint"])
        value.update(updates)
        (directory / "ready.json").write_text(json.dumps(value), encoding="utf-8")

    def packet(self, turns=None):
        if turns:
            self.write(self.peer / "sessions" / "rollout.jsonl", self.records("/peer/project", turns))
        bundle = self.root / "export"
        receipt = export_history(self.peer, self.thread, bundle)
        directory = self.exchange / "mac" / self.thread
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / receipt["sha256"]
        bundle.rename(destination)
        head = directory / "head.json"
        previous = json.loads(head.read_bytes())["sha256"] if head.is_file() else None
        head.write_text(json.dumps({"sha256": receipt["sha256"], "previous": previous}), encoding="utf-8")
        return destination

    def apply(self, **arguments):
        self.calls.append({**arguments, "raw": arguments["staged"].read_bytes(),
                           "records": _read(arguments["staged"])[1]})
        return "applied"

    def daemon(self, apply=None):
        return Daemon(self.config, apply=apply or self.apply)

    def test_pair_gate_and_immutable_reuse(self):
        self.local(["one"])
        daemon = self.daemon()
        self.assertEqual(daemon.tick()["counts"], {"waiting-pair": 1})
        self.assertEqual(list((self.exchange / "win").iterdir()),
                         [self.exchange / "win" / "ready.json"])
        self.ready(account_fingerprint="wrong")
        self.assertEqual(daemon.tick()["counts"], {"waiting-pair": 1})
        self.ready()
        self.assertEqual(daemon.tick()["counts"]["out:published"], 1)
        snapshot = {p: p.read_bytes() for p in (self.exchange / "win").rglob("*") if p.is_file()}
        self.assertEqual(daemon.tick()["counts"]["out:reused"], 1)
        self.assertEqual(snapshot, {p: p.read_bytes() for p in snapshot})
        self.assertEqual(len(list((self.exchange / "win" / self.thread).iterdir())), 2)

    def test_new_id_candidate_and_persistent_dedup(self):
        self.ready()
        packet = self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:applied"], 1)
        call = self.calls[0]
        self.assertIsNone(call["baseline_sha256"])
        self.assertEqual(call["relative"], Path("sessions/rollout.jsonl"))
        self.assertEqual(call["expected_turn_ids"], ["one", "two"])
        self.assertEqual(call["cwd"], self.cwd)
        self.assertEqual(call["records"][0]["payload"]["cwd"], str(self.cwd))
        self.assertEqual(call["records"][3]["payload"]["text"], "untouched /peer/project")
        original = (packet / "sessions" / "rollout.jsonl").read_bytes()
        self.assertIn(b"/peer/project", original)
        self.assertEqual(self.daemon().tick()["counts"]["in:already-applied"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_existing_id_strict_candidate(self):
        path = self.local(["one"])
        raw = path.read_bytes()
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:applied"], 1)
        call = self.calls[0]
        self.assertEqual(call["baseline_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(call["raw"].startswith(raw))
        self.assertEqual(call["expected_turn_ids"], ["one", "two"])
        self.assertEqual(path.read_bytes(), raw)  # Injected layer never writes the profile.

    def test_forks_and_bad_packet_never_apply(self):
        self.ready()
        self.packet(["one", "two"])
        bad = self.packet(["one", "fork"])
        self.assertEqual(self.daemon().tick()["counts"]["in:rejected"], 1)
        self.assertFalse(self.calls)
        (bad / "manifest.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.daemon().tick()["counts"]["in:rejected"], 1)
        self.assertFalse(self.calls)

    def test_target_fork_preserves_profile(self):
        path = self.local(["different"])
        raw = path.read_bytes()
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:rejected"], 1)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.calls)

    def test_mapping_failure_leaves_old_index_and_data(self):
        path = self.local(["one"])
        before = path.read_bytes()
        self.config["path_maps"] = []
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:mapping-skip"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_identical_history_does_not_require_mapping_or_write(self):
        path = self.local(["one", "two"])
        before = (self.peer / "sessions" / "rollout.jsonl").read_bytes()
        path.write_bytes(before)
        self.config["path_maps"] = []
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:unchanged"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_projectless_directory_is_created_for_a_new_import(self):
        remote = "/peer/documents/2026-11-28/new-chat"
        target = self.cwd / "2026-11-28" / "new-chat"
        self.config["path_maps"] = [
            {"from": "/peer/documents", "to": str(self.cwd), "create_projectless": True}
        ]
        source = self.peer / "sessions" / "rollout.jsonl"
        self.write(source, self.records(remote, ["one", "two"]))
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:applied"], 1)
        self.assertTrue(target.is_dir())
        self.assertEqual(self.calls[0]["cwd"], target)

    def test_unmapped_extra_root_prevents_directory_creation(self):
        remote = "/peer/documents/2026-11-28/new-chat"
        target = self.cwd / "2026-11-28" / "new-chat"
        self.config["path_maps"] = [
            {"from": "/peer/documents", "to": str(self.cwd), "create_projectless": True}
        ]
        source = self.peer / "sessions" / "rollout.jsonl"
        invalid = self.records(remote, ["one", "two"])
        invalid[0]["payload"]["runtime_workspace_roots"] = ["/peer/unmapped"]
        self.write(source, invalid)
        self.ready()
        self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:mapping-skip"], 1)
        self.assertFalse(target.exists())
        self.assertFalse(self.calls)

    def test_projectless_creation_rejects_invalid_date_and_nested_slug(self):
        mappings = [{"from": "/peer/documents", "to": str(self.cwd), "create_projectless": True}]
        for value in ("/peer/documents/2026-02-30/new-chat",
                      "/peer/documents/2026-11-28/worktree/new-chat",
                      "/peer/documents/2026-11-28/COM1"):
            with self.subTest(value=value), self.assertRaises(MappingError):
                map_path(value, mappings, set())
        with self.assertRaises(MappingError):
            map_path("/peer/documents/2026-11-28/new-chat", mappings)

    def test_busy_stale_pending_are_retried(self):
        self.ready()
        self.packet()
        for status in ("busy", "stale", "pending-index", "applied"):
            with self.subTest(status=status):
                def apply(status=status, **arguments):
                    self.calls.append(arguments["thread_id"])
                    return status
                self.assertEqual(self.daemon(apply).tick()["counts"]["in:" + status], 1)
        self.assertEqual(len(self.calls), 4)

    def test_archived_path_and_invalid_hash_name(self):
        source = self.peer / "sessions" / "rollout.jsonl"
        target = self.peer / "archived_sessions" / "archive.jsonl"
        target.parent.mkdir()
        source.rename(target)
        with closing(sqlite3.connect(self.peer / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?,archived=1", (str(target),))
            db.commit()
        self.ready()
        packet = self.packet()
        self.assertEqual(self.daemon().tick()["counts"]["in:archived-recovery"], 1)
        self.assertFalse(self.calls)
        packet.rename(packet.with_name("0" * 64))
        self.assertEqual(self.daemon().tick()["counts"]["in:rejected"], 1)
        self.assertFalse(self.calls)

    def test_bounded_output_and_unsupported_classification(self):
        self.local(["one"])
        second = "2184a532-266e-42a2-8a35-2eaae75020ad"
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                       (second, "unsupported.gz", str(self.cwd), "legacy", 1))
            db.commit()
        self.config["batch_size"] = 1
        self.ready()
        daemon = self.daemon()
        self.assertEqual(daemon.tick()["counts"], {"out:published": 1})
        result = daemon.tick()
        self.assertEqual(result["counts"], {"out:unsupported": 1})
        self.assertNotIn("fixture-fingerprint", json.dumps(result))
        self.assertEqual(daemon.tick()["counts"], {})
        self.assertEqual(daemon.tick()["counts"], {"out:reused": 1})

    def test_component_mapping_and_structured_fields_only(self):
        nested = self.cwd / "nested"
        nested.mkdir()
        other = self.root / "other"
        other.mkdir()
        mappings = [{"from": "C:\\WORK", "to": str(other)},
                    {"from": "c:\\work\\specific", "to": str(self.cwd)}]
        self.assertEqual(map_path("C:\\Work\\Specific\\nested", mappings), str(nested))
        with self.assertRaises(MappingError):
            map_path("C:\\work-other", mappings)
        with self.assertRaises(MappingError):
            map_path("C:\\work\\specific\\missing", mappings)
        with self.assertRaises(MappingError):
            map_path("/peer/project", [{"from": "/peer/project", "to": str(self.root / "absent")}])
        records = self.records("/peer/project", ["one"])
        records[0]["payload"]["runtime_workspace_roots"] = ["/peer/project"]
        records.append({"type": "event_msg", "ordinal": len(records), "payload": {
            "type": "thread_settings_applied", "thread_settings": {
                "cwd": "/peer/project", "runtime_workspace_roots": ["/peer/project"]},
            "text": "/peer/project"}})
        mapped = map_records(records, self.config["path_maps"])
        self.assertEqual(mapped[0]["payload"]["runtime_workspace_roots"], [str(self.cwd)])
        self.assertEqual(mapped[-1]["payload"]["thread_settings"]["cwd"], str(self.cwd))
        self.assertEqual(mapped[-1]["payload"]["text"], "/peer/project")
        self.assertEqual(records[0]["payload"]["cwd"], "/peer/project")

    def test_partial_paginated_history_is_not_published(self):
        path = self.local(["one", "two"])
        records = self.records(str(self.cwd), ["one", "two"])
        self.write(path, records[:-1])
        self.ready()
        result = self.daemon().tick()
        self.assertEqual(result["counts"], {"out:unsupported": 1})
        self.assertEqual(list((self.exchange / "win").iterdir()),
                         [self.exchange / "win" / "ready.json"])


if __name__ == "__main__":
    unittest.main()
