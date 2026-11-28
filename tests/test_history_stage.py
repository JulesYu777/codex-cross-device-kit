import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import kit
from history_stage import export_history, stage_fast_forward


class StageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.jsonl"
        self.target = self.root / "target.jsonl"
        self.output = self.root / "staged.jsonl"
        self.cwd = self.root / "receiver"
        self.cwd.mkdir()
        self.header = {"type": "session_meta", "payload": {
            "id": "same-id", "cwd": "/source/project", "history_mode": "paginated",
            "history_base": None}}
        self.first = self.turn("one")

    def event(self, kind, **fields):
        return {"type": "event_msg", "payload": {"type": kind, **fields}}

    def turn(self, turn_id):
        return [self.event("task_started", turn_id=turn_id),
                {"type": "turn_context", "payload": {"turn_id": turn_id,
                 "cwd": "/source/project", "evidence": "/source/project/file"}},
                {"type": "response_item", "payload": {"type": "message",
                 "role": "user", "content": [{"text": "/source/project/用户"}]}},
                self.event("task_complete", turn_id=turn_id)]

    def write(self, path, records, crlf=False):
        records = [{**record, "ordinal": ordinal} for ordinal, record in enumerate(records)]
        sep = "\r\n" if crlf else "\n"
        path.write_bytes((sep.join(json.dumps(r, ensure_ascii=False) for r in records)
                          + sep).encode("utf-8"))

    def fixtures(self, source_extra=None, target_extra=None):
        sh = copy.deepcopy(self.header)
        th = copy.deepcopy(self.header)
        th["payload"]["cwd"] = str(self.cwd)
        self.write(self.source, [sh] + self.first + (source_extra or []))
        target = copy.deepcopy(self.first + (target_extra or []))
        for record in target:
            if record["type"] == "turn_context" and record["payload"]["cwd"] == sh["payload"]["cwd"]:
                record["payload"]["cwd"] = str(self.cwd)
        self.write(self.target, [th] + target, crlf=True)

    def stage(self):
        return stage_fast_forward(self.source, self.target, self.output, self.cwd)

    def reject(self, pattern=None):
        with self.assertRaisesRegex(ValueError, pattern or "."):
            self.stage()
        self.assertFalse(self.output.exists())

    def mutate(self, path, change):
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        change(records)
        self.write(path, records)

    def test_fast_forward_preserves_bytes_and_only_maps_context_cwd(self):
        settings = [self.event("thread_settings_applied", cwd=str(self.cwd))] * 2
        self.fixtures(self.turn("two") + self.turn("three") + self.turn("four"), settings)
        source, target = self.source.read_bytes(), self.target.read_bytes()
        self.assertEqual(self.stage(), ["two", "three", "four"])
        result = self.output.read_bytes()
        self.assertTrue(result.startswith(target))
        records = [json.loads(line) for line in result.splitlines()]
        self.assertEqual([r["ordinal"] for r in records], list(range(len(records))))
        expected = self.turn("two") + self.turn("three") + self.turn("four")
        appended = records[len(target.splitlines()):]
        for actual, original in zip(appended, expected):
            actual.pop("ordinal")
            original = copy.deepcopy(original)
            if original["type"] == "turn_context":
                original["payload"]["cwd"] = str(self.cwd)
            self.assertEqual(actual, original)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.target.read_bytes(), target)

    def test_noop_duplicate_or_older(self):
        for extra in ([], self.turn("two")):
            with self.subTest(extra=bool(extra)):
                self.fixtures(target_extra=extra)
                self.assertEqual(self.stage(), [])
                self.assertFalse(self.output.exists())

    def test_repeat_with_mapped_history_is_a_no_op(self):
        self.fixtures(self.turn("two"))
        self.stage()
        self.target.write_bytes(self.output.read_bytes())
        self.output.unlink()
        self.assertEqual(self.stage(), [])
        self.assertFalse(self.output.exists())

    def test_native_world_state_and_token_usage_remain_in_turn(self):
        turn = self.turn("two")
        turn[2:2] = [{"type": "world_state", "payload": {"state": {"revision": 10}}},
                     {"type": "token_usage_record", "payload": {"total_tokens": 40}}]
        self.fixtures(turn)
        self.assertEqual(self.stage(), ["two"])
        output = [json.loads(line) for line in self.output.read_bytes().splitlines()]
        self.assertEqual([r["payload"] for r in output if r["type"] in
                          {"world_state", "token_usage_record"}], [r["payload"] for r in turn[2:4]])

    def test_explicit_parent_workspace_mapping(self):
        self.fixtures(self.turn("two"))
        self.mutate(self.source, lambda r: r[0]["payload"].update(runtime_workspace_roots=["/source"]))
        self.mutate(self.target, lambda r: r[0]["payload"].update(runtime_workspace_roots=[str(self.root)]))
        self.reject("Conflicting creation")
        with self.assertRaisesRegex(ValueError, "do not match"):
            stage_fast_forward(self.source, self.target, self.output, self.cwd,
                               source_root="/wrong", target_root=str(self.root))
        self.assertEqual(stage_fast_forward(self.source, self.target, self.output, self.cwd,
                                            source_root="/source", target_root=str(self.root)), ["two"])

    def test_conflicting_content_same_id(self):
        self.fixtures(self.turn("two"))
        self.mutate(self.target, lambda r: r[3]["payload"].update(role="assistant"))
        self.reject("Conflicting event prefix")

    def test_nested_ordinal_and_json_type_differences_are_not_ignored(self):
        for source_value, target_value in ((1, 2), (True, 1)):
            self.fixtures(self.turn("two"))
            self.mutate(self.source, lambda r: r[3]["payload"].update(ordinal=source_value))
            self.mutate(self.target, lambda r: r[3]["payload"].update(ordinal=target_value))
            self.reject("Conflicting event prefix")

    def test_turn_boundary_aliases(self):
        self.fixtures(self.turn("two"))
        def aliases(records):
            for record in records:
                payload = record["payload"]
                if payload.get("type") == "task_started":
                    payload["type"] = "turn_started"
                elif payload.get("type") == "task_complete":
                    payload["type"] = "turn_completed"
        self.mutate(self.source, aliases)
        self.mutate(self.target, aliases)
        self.assertEqual(self.stage(), ["two"])

    def test_fork(self):
        self.fixtures(self.turn("source-branch"), self.turn("target-branch"))
        self.reject("Conflicting")

    def test_different_session(self):
        self.fixtures(self.turn("two"))
        self.mutate(self.target, lambda r: r[0]["payload"].update(id="other"))
        self.reject("Different session")

    def test_partial_tail_on_either_side(self):
        for path in (self.source, self.target):
            with self.subTest(path=path.name):
                self.fixtures(self.turn("two"))
                self.mutate(path, lambda r: r.append(self.event("task_started", turn_id="partial")))
                self.reject("Incomplete")

    def test_dependencies_and_compaction(self):
        for key, value in (("history_base", {}), ("revision_id", "rev"),
                           ("history_mode", "compact"), ("segment_id", "seg")):
            with self.subTest(key=key):
                self.fixtures(self.turn("two"))
                self.mutate(self.source, lambda r: r[0]["payload"].update({key: value}))
                self.reject("Unsupported")
        self.fixtures(self.turn("two"))
        self.mutate(self.source, lambda r: r.append({"type": "compacted", "payload": {}}))
        self.reject("Unsupported")

    def test_ambiguous_duplicate_turn_or_creation(self):
        for extra in (self.turn("one"), [self.header]):
            with self.subTest(extra=extra[0]["type"]):
                self.fixtures(extra)
                self.reject("Ambiguous")

    def test_settings_only_ignored_outside_turns(self):
        self.fixtures(self.turn("two"), [self.event("other", data="unverified")])
        self.reject("Conflicting")
        self.fixtures(self.turn("two"))
        self.mutate(self.target, lambda r: r.insert(2, self.event("thread_settings_applied")))
        self.reject("Conflicting")

    def test_nonturn_suffix(self):
        self.fixtures(self.turn("two") + [self.event("unknown")])
        self.reject("Non-turn suffix")

    def test_unmapped_context_and_missing_directory(self):
        extra = self.turn("two")
        extra[1]["payload"]["cwd"] = "/another/project"
        self.fixtures(extra)
        self.reject("mapping")
        self.cwd.rmdir()
        self.reject("existing absolute")

    def test_target_cwd_must_match(self):
        self.fixtures(self.turn("two"))
        self.mutate(self.target, lambda r: r[0]["payload"].update(cwd=str(self.root)))
        self.reject("does not match")

    def test_existing_output_untouched(self):
        self.fixtures(self.turn("two"))
        self.output.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "new explicit"):
            self.stage()
        self.assertEqual(self.output.read_bytes(), b"keep")

    def test_malformed_json_ordinals_and_truncated_line(self):
        for raw in (b'{"ordinal":0,"ordinal":1}\n', b'not json\n', b'{}', b'\n'):
            with self.subTest(raw=raw):
                self.fixtures(self.turn("two"))
                self.source.write_bytes(raw)
                self.reject()
        self.fixtures(self.turn("two"))
        raw = self.source.read_bytes().replace(b'"ordinal": 1', b'"ordinal": 999')
        self.source.write_bytes(raw)
        self.reject("Non-contiguous")

    def test_write_failure_removes_partial_output(self):
        self.fixtures(self.turn("two"))
        with patch("history_stage.os.fsync", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.stage()
        self.assertFalse(self.output.exists())

    def test_cli_does_not_load_sync_state(self):
        self.fixtures(self.turn("two"))
        args = ["kit.py", "history", "stage-fast-forward", "--source", str(self.source),
                "--target", str(self.target), "--out", str(self.output), "--cwd", str(self.cwd)]
        with patch("sys.argv", args), patch("kit.ready", side_effect=AssertionError("profile access")):
            with redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(kit.main(), 0)
        self.assertEqual(json.loads(stream.getvalue())["missing_turn_ids"], ["two"])

    def test_export_verified_manifest_and_reject_incomplete_turn(self):
        thread_id = "2184a532-266e-42a2-8a35-2eaae75020ac"
        home, output = self.root / "codex", self.root / "exchange"
        relative = "sessions/2026/11/27/rollout.jsonl"
        indexed = home / relative
        indexed.parent.mkdir(parents=True)
        self.header["payload"]["id"] = thread_id
        db = sqlite3.connect(home / "state_5.sqlite")
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?)", (thread_id, str(indexed)))
        db.commit()
        db.close()
        self.write(indexed, [self.header] + self.first + [self.event("task_started", turn_id="inflight")])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            export_history(home, thread_id, output)
        self.assertFalse(output.exists())
        self.write(indexed, [self.header] + self.first)
        raw = indexed.read_bytes()
        receipt = export_history(home, thread_id, output)
        self.assertEqual(receipt, {"path": relative, "turn_ids": ["one"], "bytes": len(raw),
                                   "sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual((output / relative).read_bytes(), raw)
        self.assertEqual(json.loads((output / "manifest.json").read_bytes()), [receipt])
        with self.assertRaisesRegex(ValueError, "new explicit"):
            export_history(home, thread_id, output)
