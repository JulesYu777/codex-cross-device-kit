"""Synthetic byte-level contract fixtures; never read profiles or native data."""
import copy
import json
from pathlib import Path
import unittest

from history_lineage import is_forward, map_chain, parse_chain


THREAD = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
THIRD = "00000000-0000-4000-8000-000000000003"


def path(physical=None):
    suffix = "_" + physical if physical else ""
    return Path("sessions/2026/11/28/rollout-2026-11-28T12-00-00-" + THREAD + suffix + ".jsonl")


def encode(records, newline="\n"):
    return (newline.join(json.dumps(r, ensure_ascii=False) for r in records) + newline).encode("utf-8")


def event(ordinal, kind, **fields):
    return {"ordinal": ordinal, "type": "event_msg", "payload": {"type": kind, **fields}}


def header(ordinal=0, base=None):
    return {"ordinal": ordinal, "type": "session_meta", "payload": {
        "id": THREAD, "cwd": "/source/项目", "history_mode": "paginated",
        "runtime_workspace_roots": ["/source/项目"], "history_base": base}}


def context(ordinal):
    return {"ordinal": ordinal, "type": "turn_context", "payload": {
        "cwd": "/source/项目", "note": "/source/项目 must remain evidence"}}


def child(parent, identity, start, *tail):
    return [header(start, {"thread_id": identity, "end_byte_offset": len(parent),
                           "end_ordinal_exclusive": start}), *tail]


def fixtures():
    # A checked-in synthetic golden contract: CRLF and Unicode exercise raw
    # decoded byte offsets; an interrupted task is intentionally not completed.
    root = [header(), event(1, "task_started", turn_id="interrupted"), context(2),
            event(3, "turn_aborted", turn_id="interrupted")]
    a = encode(root, "\r\n")
    middle = child(a, THREAD, 4, event(5, "task_started", turn_id="two"), context(6),
                   event(7, "task_complete", turn_id="two"))
    b = encode(middle)
    leaf = child(b, SECOND, 8, event(9, "task_started", turn_id="open"), context(10))
    return [(path(), a), (path(SECOND), b), (path(THIRD), encode(leaf))]


def mutate(inventory, index, change):
    result = list(inventory)
    records = [json.loads(line) for line in result[index][1].split(b"\n")[:-1]]
    change(records)
    result[index] = (result[index][0], encode(records))
    return result


def map_paths(records):
    # Same callback shape and exact field surface as history_daemon.map_records,
    # without importing filesystem-dependent project mapping or creating roots.
    for record in records:
        payload = record["payload"]
        fields = payload if record["type"] in {"session_meta", "turn_context"} else None
        if record["type"] == "event_msg" and payload.get("type") == "thread_settings_applied":
            fields = payload["thread_settings"]
        if fields is not None:
            if "cwd" in fields:
                fields["cwd"] = fields["cwd"].replace("/source/项目", "/receiver/更长的目录/项目")
            if record["type"] != "turn_context" and "runtime_workspace_roots" in fields:
                fields["runtime_workspace_roots"] = [
                    r.replace("/source/项目", "/receiver/更长的目录/项目")
                    for r in fields["runtime_workspace_roots"]]
    return records


class LineageTest(unittest.TestCase):
    def parse(self, inventory=None):
        return parse_chain(THREAD, fixtures() if inventory is None else inventory)

    def test_three_segment_gold_contract_preserves_interrupted_and_open_turns(self):
        inventory = fixtures()
        chain = self.parse(inventory)
        self.assertEqual([s.physical_id for s in chain.segments], [THREAD, SECOND, THIRD])
        self.assertEqual([r["ordinal"] for r in chain.records], list(range(11)))
        self.assertEqual(chain.records[3]["payload"]["type"], "turn_aborted")
        self.assertEqual(chain.records[-1]["type"], "turn_context")
        for segment, (_, raw) in zip(chain.segments, inventory):
            self.assertEqual(segment.raw, raw)
            self.assertEqual(segment.line_ends[-1], len(raw))
        # The interrupted root alone is valid; no completion semantics are used.
        self.assertEqual(len(self.parse(inventory[:1]).records), 4)

    def test_map_recalculates_both_boundaries_after_utf8_expansion(self):
        chain = self.parse()
        before = copy.deepcopy(chain)
        mapped = map_chain(chain, map_paths)
        self.assertEqual(chain, before)
        self.assertEqual([s.path for s in mapped.segments], [s.path for s in chain.segments])
        self.assertEqual([r["ordinal"] for r in mapped.records], list(range(11)))
        for index in (1, 2):
            base = mapped.segments[index].records[0]["payload"]["history_base"]
            self.assertEqual(base["end_byte_offset"], len(mapped.segments[index - 1].raw))
            self.assertNotEqual(base["end_byte_offset"],
                                chain.segments[index].records[0]["payload"]["history_base"]["end_byte_offset"])
            self.assertEqual(base["end_ordinal_exclusive"], index * 4)
        self.assertEqual(mapped.records[2]["payload"]["note"], chain.records[2]["payload"]["note"])
        self.assertIn("更长的目录", mapped.records[2]["payload"]["cwd"])
        self.assertEqual(self.parse([(s.path, s.raw) for s in mapped.segments]), mapped)

    def test_cutoff_can_exclude_valid_parent_tail_and_map_exact_record(self):
        inventory = fixtures()
        root = inventory[0][1] + encode([event(4, "task_started", turn_id="discarded")])
        inventory[0] = (path(), root)
        chain = self.parse(inventory)
        self.assertEqual([r["ordinal"] for r in chain.records], list(range(11)))
        mapped = map_chain(chain, map_paths)
        base = mapped.segments[1].records[0]["payload"]["history_base"]
        self.assertEqual(base["end_byte_offset"], mapped.segments[0].line_ends[3])
        self.assertLess(base["end_byte_offset"], len(mapped.segments[0].raw))
        self.assertFalse(is_forward(self.parse(inventory[:1]), chain))

    def test_forward_equal_append_reverse_and_changed_evidence(self):
        inventory = fixtures()
        root, middle, leaf = (self.parse(inventory[:n]) for n in (1, 2, 3))
        self.assertTrue(is_forward(root, middle))
        self.assertTrue(is_forward(middle, leaf))
        self.assertTrue(is_forward(leaf, leaf))
        self.assertFalse(is_forward(leaf, middle))
        fork = mutate(inventory, 2, lambda r: r[-1]["payload"].update(note="different"))
        self.assertFalse(is_forward(leaf, self.parse(fork)))
        longer = list(inventory)
        longer[-1] = (longer[-1][0], longer[-1][1] + encode([event(11, "turn_aborted", turn_id="open")]))
        self.assertTrue(is_forward(leaf, self.parse(longer)))
        self.assertFalse(is_forward(self.parse(longer), leaf))

    def test_middle_segment_cutoff_and_normalized_three_segment_prefix(self):
        inventory = fixtures()
        # The final child's byte offset remains the original middle line end.
        inventory[1] = (inventory[1][0], inventory[1][1] + encode([
            event(8, "task_started", turn_id="non-inherited-middle-tail")]))
        chain = self.parse(inventory)
        self.assertEqual([r["ordinal"] for r in chain.records], list(range(11)))
        mapped = map_chain(chain, map_paths)
        base = mapped.segments[2].records[0]["payload"]["history_base"]
        self.assertEqual(base["end_byte_offset"], mapped.segments[1].line_ends[3])
        self.assertLess(base["end_byte_offset"], len(mapped.segments[1].raw))
        normalized = map_chain(chain, lambda r: r)
        self.assertTrue(is_forward(chain, normalized))
        self.assertTrue(is_forward(normalized, chain))

    def test_forward_ignores_only_serialization_not_paths_or_json_types(self):
        inventory = fixtures()
        root = self.parse(inventory[:1])
        normalized = map_chain(root, lambda records: records)
        self.assertTrue(is_forward(root, normalized))
        self.assertFalse(is_forward(root, map_chain(root, map_paths)))
        changed = [(Path(str(path()).replace("sessions/", "archived_sessions/").replace("sessions\\", "archived_sessions\\")), inventory[0][1])]
        self.assertFalse(is_forward(root, self.parse(changed)))
        a = mutate(inventory[:1], 0, lambda r: r[-1]["payload"].update(flag=True))
        b = mutate(inventory[:1], 0, lambda r: r[-1]["payload"].update(flag=1))
        self.assertFalse(is_forward(self.parse(a), self.parse(b)))

    def test_raw_boundaries_not_reserialized_or_character_offsets(self):
        inventory = fixtures()[:2]
        raw = inventory[0][1]
        wrong = [len(raw) - 1, len(raw) + 1, len(raw.decode("utf-8")), 0, -1, True,
                 len(encode([json.loads(line) for line in raw.split(b"\n")[:-1]]))]
        for offset in wrong:
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.parse(mutate(inventory, 1, lambda r: r[0]["payload"]["history_base"].update(end_byte_offset=offset)))

    def test_boundary_ordinal_and_child_start_must_agree(self):
        inventory = fixtures()[:2]
        for ordinal in (0, 3, 5, True, "4"):
            with self.subTest(ordinal=ordinal), self.assertRaises(ValueError):
                self.parse(mutate(inventory, 1, lambda r: r[0]["payload"]["history_base"].update(end_ordinal_exclusive=ordinal)))
        with self.assertRaises(ValueError):
            self.parse(mutate(inventory, 1, lambda r: [record.update(ordinal=record["ordinal"] + 1) for record in r]))

    def test_missing_out_of_order_extra_or_cyclic_dependencies(self):
        inventory = fixtures()
        for bad in ([], inventory[1:], [inventory[0], inventory[2]], list(reversed(inventory)),
                    inventory + [inventory[0]], inventory[:1] * 2):
            with self.subTest(bad=len(bad)), self.assertRaises(ValueError):
                self.parse(bad)
        for identity in (SECOND, THIRD, "not-a-uuid"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.parse(mutate(inventory, 1, lambda r: r[0]["payload"]["history_base"].update(thread_id=identity)))
        with self.assertRaises(ValueError):
            self.parse(mutate(inventory, 1, lambda r: r[0]["payload"]["history_base"].update(previous_segment=THREAD)))

    def test_wrong_logical_physical_names_and_unsafe_paths(self):
        raw = fixtures()[0][1]
        name = path().name
        bad = ["../sessions/" + name, "/sessions/" + name, "C:/sessions/" + name,
               "C:sessions/" + name, "\\\\server\\sessions\\" + name,
               "sessions/../" + name, "sessions//" + name, "sessions/./" + name,
               "other/" + name, "sessions /" + name, "sessions/x:/" + name,
               "sessions/" + name + ".zst", "sessions/" + name + ".",
               "sessions/2026/99/28/" + name, "sessions/arbitrary/" + name,
               "sessions/" + name.replace(THREAD, SECOND)]
        for value in bad:
            with self.subTest(path=value), self.assertRaises(ValueError):
                self.parse([(value, raw)])
        inventory = fixtures()[:2]
        with self.assertRaises(ValueError):
            self.parse([(path(THIRD), inventory[0][1]), inventory[1]])
        with self.assertRaises(ValueError):
            self.parse([(path(), raw), (str(path()).upper(), raw)])
        with self.assertRaises(ValueError):
            self.parse(mutate(inventory, 1, lambda r: r[0]["payload"].update(id=SECOND)))

    def test_reject_corrupt_ambiguous_nonpaginated_and_unproven_formats(self):
        raw = fixtures()[0][1]
        bad_bytes = [b"", raw[:-1], raw + b"\n", b"not json\n", b"\xff\n", b"[]\n",
                     b'{"type":"session_meta","type":"event_msg"}\n',
                     raw.replace(b'"ordinal": 0', b'"ordinal": true', 1),
                     raw.replace(b'"ordinal": 0', b'"ordinal": 1', 1),
                     raw.replace(b'"ordinal": 0', b'"ordinal": NaN', 1),
                     raw.replace(b'"ordinal": 0', b'"ordinal": 1e999', 1),
                     raw.replace(b'"type": "session_meta"', b'"type": []', 1),
                     raw.replace(b'"cwd": ', b'"cwd": "\\ud800", "other": ', 1)]
        for value in bad_bytes:
            with self.subTest(raw=value[:30]), self.assertRaises(ValueError):
                self.parse([(path(), value)])
        changes = [lambda r: r[0]["payload"].update(history_mode="full"),
                   lambda r: r[0]["payload"].update(history_revision="forged"),
                   lambda r: r[0]["payload"].update(history_base={}),
                   lambda r: r[0]["payload"].update(cwd=""),
                   lambda r: r[1].update(type="session_meta"),
                   lambda r: r[1].update(type="unknown"),
                   lambda r: r[1]["payload"].update(type="thread_rolled_back"),
                   lambda r: r[1].update(ordinal=9)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.parse(mutate(fixtures()[:1], 0, change))
        with self.assertRaises(ValueError):
            parse_chain("not-a-uuid", fixtures())

    def test_callback_cannot_tamper_with_evidence_or_alias_source(self):
        chain = self.parse()
        for change in (lambda r: r[-1]["payload"].update(note="forged"),
                       lambda r: r[0]["payload"].update(id=SECOND),
                       lambda r: r[0]["payload"].update(runtime_workspace_roots=[]),
                       lambda r: r[0]["payload"].update(cwd=17),
                       lambda r: r.pop()):
            def callback(records):
                change(records)
                return records
            with self.subTest(change=change), self.assertRaises(ValueError):
                map_chain(chain, callback)
        with self.assertRaises(ValueError):
            map_chain(chain, lambda records: None)
        def tamper_dependency(records):
            base = records[0]["payload"].get("history_base")
            if base is not None:
                base["end_byte_offset"] += 1
            return records
        with self.assertRaises(ValueError):
            map_chain(chain, tamper_dependency)
        self.assertEqual(chain, self.parse())
        # A mutated convenience view cannot bypass raw-byte validation.
        chain.records[0]["payload"]["id"] = SECOND
        self.assertEqual(map_chain(chain, lambda r: r).thread_id, THREAD)

    def test_settings_mapping_and_unicode_line_separator_preserved(self):
        records = [header(), event(1, "thread_settings_applied", thread_settings={
            "cwd": "/source/项目", "runtime_workspace_roots": ["/source/项目"]}),
            {"ordinal": 2, "type": "response_item", "payload": {"text": "one\u2028two"}}]
        chain = self.parse([(path(), encode(records))])
        mapped = map_chain(chain, map_paths)
        self.assertIn("更长的目录", mapped.records[1]["payload"]["thread_settings"]["cwd"])
        self.assertEqual(mapped.records[2]["payload"]["text"], "one\u2028two")


if __name__ == "__main__":
    unittest.main()