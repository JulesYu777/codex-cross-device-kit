from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import kit
from history_exchange import exchange_once
from history_stage import export_history


class ExchangeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.thread = "2184a532-266e-42a2-8a35-2eaae75020ac"
        self.exchange = self.root / "exchange"
        self.exchange.mkdir()
        self.stage = self.root / "stage"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.home = self.root / "local"
        self.remote = self.root / "remote"
        self.local_file = self.profile(self.home, str(self.cwd), ["one"])
        self.remote_file = self.profile(self.remote, "/peer/project", ["one", "two"])

    def records(self, cwd, turns):
        result = [{"type": "session_meta", "payload": {
            "id": self.thread, "cwd": cwd, "history_mode": "paginated"}}]
        for turn in turns:
            result.extend([
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                {"type": "turn_context", "payload": {"turn_id": turn, "cwd": cwd}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"text": "evidence " + turn}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}])
        return result

    def write(self, path, records):
        path.write_bytes(b"".join((json.dumps({**r, "ordinal": i}, ensure_ascii=False)
                                   + "\n").encode() for i, r in enumerate(records)))

    def profile(self, home, cwd, turns):
        path = home / "sessions" / "rollout.jsonl"
        path.parent.mkdir(parents=True)
        self.write(path, self.records(cwd, turns))
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?)", (self.thread, str(path)))
            db.commit()
        return path

    def packet(self, records=None):
        if records is not None:
            self.write(self.remote_file, records)
        pending = self.root / "bundle"
        receipt = export_history(self.remote, self.thread, pending)
        parent = self.exchange / "mac" / self.thread
        parent.mkdir(parents=True, exist_ok=True)
        output = parent / receipt["sha256"]
        pending.rename(output)
        return output

    def tick(self, **kwargs):
        args = dict(codex_home=self.home, thread_id=self.thread, exchange=self.exchange,
                    device="win", peer="mac", stage_dir=self.stage, cwd=self.cwd)
        args.update(kwargs)
        return exchange_once(**args)

    def snapshot(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}

    def test_empty_queue_publishes_once_original_bytes_and_manifest(self):
        before = self.snapshot(self.home)
        first = self.tick()
        self.assertTrue(first["published"])
        self.assertEqual(first["status"], "no-op")
        packet = Path(first["outbound_path"])
        manifest = json.loads((packet / "manifest.json").read_bytes())
        self.assertEqual((packet / manifest[0]["path"]).read_bytes(), self.local_file.read_bytes())
        after = self.snapshot(self.exchange)
        self.assertFalse(self.tick()["published"])
        self.assertEqual(after, self.snapshot(self.exchange))
        self.assertEqual(before, self.snapshot(self.home))
        self.assertFalse(self.stage.exists())
        self.assertFalse((self.exchange / "mac").exists())

    def test_stage_and_retry_preserve_profile_peer_and_target_prefix(self):
        peer = self.packet()
        peer_before, local_before = self.snapshot(peer), self.snapshot(self.home)
        result = self.tick()
        self.assertEqual(result["status"], "staged")
        self.assertEqual(result["source_sha256"], peer.name)
        self.assertEqual(result["missing_turn_ids"], ["two"])
        output = Path(result["staged_path"])
        self.assertTrue(output.read_bytes().startswith(self.local_file.read_bytes()))
        self.assertIn(result["baseline_sha256"], output.name)
        self.assertIn(peer.name, output.name)
        stat = output.stat()
        again = self.tick()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["staged_path"], str(output))
        self.assertEqual(output.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(local_before, self.snapshot(self.home))
        self.assertEqual(peer_before, self.snapshot(peer))

    def test_old_and_equal_never_stage(self):
        self.packet(self.records("/peer/project", ["one"]))
        self.assertEqual(self.tick()["status"], "no-op")
        self.write(self.local_file, self.records(str(self.cwd), ["one", "two"]))
        self.assertEqual(self.tick()["status"], "no-op")
        self.assertFalse(self.stage.exists())

    def test_unique_longest_is_selected_regardless_of_directory_order(self):
        self.packet(self.records("/peer/project", ["one"]))
        self.packet(self.records("/peer/project", ["one", "two"]))
        longest = self.packet(self.records("/peer/project", ["one", "two", "three"]))
        result = self.tick()
        self.assertEqual(result["status"], "staged")
        self.assertEqual(result["source_sha256"], longest.name)
        self.assertEqual(result["missing_turn_ids"], ["two", "three"])

    def test_equal_turn_ids_different_content_conflict_and_preserve_old_stage(self):
        self.packet()
        old = self.tick()
        before = Path(old["staged_path"]).read_bytes()
        records = self.records("/peer/project", ["one", "two"])
        records[7]["payload"]["content"][0]["text"] = "fork"
        self.packet(records)
        self.assertEqual(self.tick()["status"], "conflict")
        self.assertEqual(Path(old["staged_path"]).read_bytes(), before)
        self.assertEqual(len(list(self.stage.glob("*.jsonl"))), 1)

    def test_old_peer_fork_beyond_local_baseline_blocks_longest(self):
        self.packet(self.records("/peer/project", ["one", "branch-a"]))
        self.packet(self.records("/peer/project", ["one", "branch-b", "three"]))
        self.assertEqual(self.tick()["status"], "conflict")
        self.assertFalse(self.stage.exists())

    def test_local_fork_rejected_even_when_same_turn_ids(self):
        self.packet()
        records = self.records(str(self.cwd), ["one", "two"])
        records[7]["payload"]["content"][0]["text"] = "local fork"
        self.write(self.local_file, records)
        before = self.snapshot(self.home)
        self.assertEqual(self.tick()["status"], "conflict")
        self.assertEqual(before, self.snapshot(self.home))
        self.assertFalse(self.stage.exists())

    def test_bad_packets_block_all_staging(self):
        for damage in ("missing", "hash", "size", "turns", "id", "partial", "fingerprint"):
            with self.subTest(damage=damage):
                shutil.rmtree(self.exchange / "mac", ignore_errors=True)
                packet = self.packet(self.records("/peer/project", ["one", "two"]))
                manifest_path = packet / "manifest.json"
                manifest = json.loads(manifest_path.read_bytes())
                source = packet / manifest[0]["path"]
                if damage == "missing":
                    source.unlink()
                elif damage == "hash":
                    source.write_bytes(source.read_bytes().replace(b"evidence", b"tampered"))
                elif damage == "size":
                    manifest[0]["bytes"] += 1
                elif damage == "turns":
                    manifest[0]["turn_ids"] = ["one", "lie"]
                elif damage in {"id", "partial"}:
                    records = self.records("/peer/project", ["one", "two"])
                    if damage == "id":
                        records[0]["payload"]["id"] = "other-thread"
                    else:
                        records.pop()
                    self.write(source, records)
                    manifest[0]["bytes"] = source.stat().st_size
                    manifest[0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                    packet = packet.rename(packet.parent / manifest[0]["sha256"])
                    manifest_path = packet / "manifest.json"
                else:
                    packet = packet.rename(packet.parent / ("0" * 64))
                    manifest_path = packet / "manifest.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                result = self.tick()
                self.assertEqual(result["status"], "rejected")
                self.assertTrue(result["rejected"][0]["reason"])
                self.assertFalse(self.stage.exists())

    def test_traversal_in_any_candidate_blocks_before_staging(self):
        good = self.packet()
        for path in ("../outside.jsonl", "/absolute.jsonl", "sessions/../../outside.jsonl",
                     "C:/escape.jsonl", "sessions\\escape.jsonl", "sessions//file.jsonl",
                     "sessions/NUL.jsonl"):
            with self.subTest(path=path):
                bad = good.parent / ("0" * 64)
                bad.mkdir(exist_ok=True)
                receipt = json.loads((good / "manifest.json").read_bytes())
                receipt[0]["path"] = path
                (bad / "manifest.json").write_text(json.dumps(receipt), encoding="utf-8")
                with patch("history_exchange.stage_fast_forward", side_effect=AssertionError("too early")):
                    self.assertEqual(self.tick()["status"], "rejected")
                self.assertFalse(self.stage.exists())

    def test_incomplete_transfer_and_unrelated_thread_not_imported(self):
        other = self.exchange / "mac" / "other-thread"
        other.mkdir(parents=True)
        (other / "junk").write_bytes(b"bad")
        self.assertEqual(self.tick()["status"], "no-op")
        partial = self.exchange / "mac" / self.thread / ("0" * 64)
        partial.mkdir(parents=True)
        self.assertEqual(self.tick()["status"], "rejected")
        self.assertFalse(self.stage.exists())

    def test_configuration_rejected_without_writes(self):
        for args in ({"device": "../escape"}, {"peer": "win"}, {"device": "CON"},
                     {"device": "con"}, {"peer": "Mac"},
                     {"stage_dir": self.home / "staging"},
                     {"stage_dir": self.exchange / "staging"},
                     {"exchange": self.root / "missing"}):
            with self.subTest(args=args):
                with self.assertRaises((ValueError, OSError)):
                    self.tick(**args)
                self.assertEqual(self.snapshot(self.exchange), {})

    def test_new_baseline_binds_new_output_and_does_not_replace_old(self):
        self.packet(self.records("/peer/project", ["one", "two", "three"]))
        first = self.tick()
        old = Path(first["staged_path"]).read_bytes()
        self.write(self.local_file, self.records(str(self.cwd), ["one", "two"]))
        second = self.tick()
        self.assertEqual(second["status"], "staged")
        self.assertNotEqual(first["staged_path"], second["staged_path"])
        self.assertEqual(Path(first["staged_path"]).read_bytes(), old)
        self.assertEqual(second["missing_turn_ids"], ["three"])

    def test_corrupt_existing_stage_is_not_overwritten(self):
        self.packet()
        first = self.tick()
        output = Path(first["staged_path"])
        output.write_bytes(b"preserve evidence")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.tick()
        self.assertEqual(output.read_bytes(), b"preserve evidence")

    def test_packet_symlink_rejected_without_staging(self):
        packet = self.packet()
        source = packet / "sessions" / "rollout.jsonl"
        source.unlink()
        try:
            source.symlink_to(self.remote_file)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.assertEqual(self.tick()["status"], "rejected")
        self.assertFalse(self.stage.exists())

    def test_explicit_workspace_roots_forwarded(self):
        remote_records = self.records("/peer/project", ["one", "two"])
        remote_records[0]["payload"]["runtime_workspace_roots"] = ["/peer/root"]
        local_records = self.records(str(self.cwd), ["one"])
        local_records[0]["payload"]["runtime_workspace_roots"] = [str(self.root)]
        self.write(self.local_file, local_records)
        self.packet(remote_records)
        self.assertEqual(self.tick(source_root="/peer/root", target_root=str(self.root))["status"],
                         "staged")

    def test_partial_local_history_never_publishes(self):
        records = self.records(str(self.cwd), ["one", "two"])
        self.write(self.local_file, records[:-1])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.tick()
        self.assertEqual(self.snapshot(self.exchange), {})
        self.assertFalse(self.stage.exists())

    def test_cli_does_not_load_sync_state(self):
        args = ["kit.py", "history", "exchange-once", "--codex-home", str(self.home),
                "--thread-id", self.thread, "--exchange", str(self.exchange),
                "--device", "win", "--peer", "mac", "--stage-dir", str(self.stage),
                "--cwd", str(self.cwd)]
        with patch("sys.argv", args), patch("kit.ready", side_effect=AssertionError("sync state")):
            with redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(kit.main(), 0)
        self.assertTrue(json.loads(stream.getvalue())["published"])


if __name__ == "__main__":
    unittest.main()
