from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from history_stage import _parse, _read, export_history
from history_storage import file_signature, read_rollout, resolve_rollout

try:
    from compression import zstd
except ImportError:
    zstd = None


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plain = self.root / "rollout.jsonl"
        self.packed = self.root / "rollout.jsonl.zst"

    def test_resolution_missing_suffix_directory_and_native_precedence(self):
        with self.assertRaises(FileNotFoundError):
            resolve_rollout(self.plain)
        with self.assertRaises(ValueError):
            resolve_rollout(self.root / "rollout.gz")
        self.packed.write_bytes(b"compressed")
        self.assertEqual(resolve_rollout(self.plain), (self.plain, self.packed))
        self.assertEqual(resolve_rollout(self.packed), (self.plain, self.packed))
        self.plain.write_bytes(b"native")
        self.assertEqual(resolve_rollout(self.packed), (self.plain, self.plain))
        self.plain.unlink()
        self.plain.mkdir()
        with self.assertRaisesRegex(ValueError, "directory"):
            resolve_rollout(self.packed)

    def test_plain_exact_bytes_limits_signature_and_missing(self):
        self.plain.write_bytes(b"abc\n")
        signature = file_signature(self.plain)
        stat = self.plain.stat()
        self.assertEqual(signature, (stat.st_dev, stat.st_ino, stat.st_size,
                                     stat.st_mtime_ns, stat.st_ctime_ns))
        self.assertEqual(read_rollout(self.plain, max_bytes=4), b"abc\n")
        with self.assertRaisesRegex(ValueError, "limit"):
            read_rollout(self.plain, max_bytes=3)
        for limit in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                read_rollout(self.plain, max_bytes=limit)
        with self.assertRaises(FileNotFoundError):
            read_rollout(self.packed)
        with self.assertRaises(ValueError):
            read_rollout(self.root)
        self.plain.write_bytes(b"changed")
        self.assertNotEqual(signature, file_signature(self.plain))

    @unittest.skipIf(zstd is None, "standard compression.zstd unavailable")
    def test_zstd_exact_bytes_limit_corruption_truncation_and_trailing_garbage(self):
        raw = b"standalone JSONL\n" * 1000
        packet = zstd.compress(raw)
        self.packed.write_bytes(packet)
        self.assertEqual(read_rollout(self.packed, max_bytes=len(raw)), raw)
        with self.assertRaisesRegex(ValueError, "limit"):
            read_rollout(self.packed, max_bytes=len(raw) - 1)
        for bad in (b"not zstd", b"", packet[:-1], packet + b"garbage"):
            with self.subTest(bad=bad[:20]):
                self.packed.write_bytes(bad)
                with self.assertRaises((ValueError, EOFError, OSError, zstd.ZstdError)):
                    read_rollout(self.packed)

    def test_old_python_requires_explicit_existing_decoder(self):
        self.packed.write_bytes(b"packet")
        with patch("history_storage.ZstdFile", None):
            with self.assertRaisesRegex(ValueError, "explicit zstd"):
                read_rollout(self.packed)
            with self.assertRaises(FileNotFoundError):
                read_rollout(self.packed, decompressor=self.root / "missing")
            with self.assertRaisesRegex(ValueError, "existing file"):
                read_rollout(self.packed, decompressor=self.root)

    def test_cli_argv_exit_status_bounded_output_and_cleanup(self):
        self.packed.write_bytes(b"packet")
        executable = self.root / "zstd with spaces.exe"
        executable.write_bytes(b"test executable placeholder")
        for returncode, raw, limit, error in ((0, b"ok\n", 3, None),
                                             (1, b"partial", 100, "failed"),
                                             (0, b"too big", 2, "limit")):
            process = Mock()
            process.stdout = io.BytesIO(raw)
            process.wait.return_value = returncode
            process.poll.return_value = None
            with patch("history_storage.ZstdFile", None), patch(
                    "history_storage.subprocess.Popen", return_value=process) as popen:
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        read_rollout(self.packed, decompressor=executable, max_bytes=limit)
                else:
                    self.assertEqual(read_rollout(self.packed, decompressor=executable,
                                                  max_bytes=limit), raw)
                self.assertEqual(popen.call_args.args[0],
                                 [str(executable.resolve()), "-d", "-c", "--",
                                  str(self.packed.resolve())])
                self.assertFalse(popen.call_args.kwargs["shell"])
                self.assertTrue(process.stdout.closed)
                process.kill.assert_called_once()


class CompressedExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "isolated-home"
        self.relative = Path("archived_sessions/2026/11/27/rollout.jsonl")
        self.plain = self.home / self.relative
        self.plain.parent.mkdir(parents=True)
        self.packed = Path(str(self.plain) + ".zst")
        self.output = self.root / "bundle"
        self.thread_id = "2184a532-266e-42a2-8a35-2eaae75020ac"
        self.records = [
            {"type": "session_meta", "payload": {"id": self.thread_id,
             "cwd": "/source/project", "history_mode": "paginated", "history_base": None}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "one"}}]
        self.raw = self.encode()
        self.plain.write_bytes(self.raw)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?)", (self.thread_id, str(self.plain)))
            db.commit()

    def encode(self):
        return b"".join((json.dumps({**record, "ordinal": ordinal}) + "\r\n").encode()
                        for ordinal, record in enumerate(self.records))

    def index(self, path):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path = ?", (str(path),))
            db.commit()

    def export(self, **kwargs):
        return export_history(self.home, self.thread_id, self.output, **kwargs)

    def test_fork_provenance_without_history_base_is_standalone(self):
        self.records[0]["payload"]["forked_from_id"] = "2184a532-266e-42a2-8a35-2eaae75020ad"
        self.plain.write_bytes(self.encode())
        receipt = self.export()
        result = _read(self.output / receipt["path"])
        self.assertEqual(receipt["turn_ids"], ["one"])
        self.assertEqual(result[1][0]["payload"]["forked_from_id"],
                         self.records[0]["payload"]["forked_from_id"])

    def compress(self):
        packet = zstd.compress(self.raw)
        self.packed.write_bytes(packet)
        self.plain.unlink()
        return packet

    def test_plain_manifest_read_contract_and_precedence_without_decoder(self):
        self.packed.write_bytes(b"invalid packet deliberately ignored")
        self.index(self.packed)
        self.assertEqual(_read(self.plain), _parse(self.raw))
        receipt = self.export(packed=True)
        self.assertEqual(receipt, {"path": self.relative.as_posix(), "turn_ids": ["one"],
                                  "bytes": len(self.raw),
                                  "sha256": hashlib.sha256(self.raw).hexdigest()})
        self.assertEqual((self.output / self.relative).read_bytes(), self.raw)
        self.assertEqual(json.loads((self.output / "manifest.json").read_text()), [receipt])
        self.assertEqual(self.packed.read_bytes(), b"invalid packet deliberately ignored")

    @unittest.skipIf(zstd is None, "standard compression.zstd unavailable")
    def test_compressed_packed_and_decoded_exports_match_plain_sha(self):
        plain_receipt = self.export()
        packet = self.compress()
        signature = file_signature(self.packed)
        for packed in (False, True):
            with self.subTest(packed=packed):
                self.output = self.root / f"compressed-{packed}"
                # Cover both canonical and explicitly compressed SQLite paths.
                self.index(self.packed if packed else self.plain)
                receipt = self.export(packed=packed)
                for key in plain_receipt:
                    self.assertEqual(receipt[key], plain_receipt[key])
                artifact = self.output / self.relative
                if packed:
                    artifact = Path(str(artifact) + ".zst")
                    self.assertFalse((self.output / self.relative).exists())
                    self.assertEqual(artifact.read_bytes(), packet)
                    self.assertEqual(receipt["encoding"], "zstd")
                    self.assertEqual(receipt["packed_bytes"], len(packet))
                    self.assertEqual(receipt["packed_sha256"], hashlib.sha256(packet).hexdigest())
                else:
                    self.assertEqual(receipt, plain_receipt)
                    self.assertEqual(artifact.read_bytes(), self.raw)
                self.assertEqual(json.loads((self.output / "manifest.json").read_text()), [receipt])
                self.assertEqual(self.packed.read_bytes(), packet)
                self.assertEqual(file_signature(self.packed), signature)

    @unittest.skipIf(zstd is None, "standard compression.zstd unavailable")
    def test_compressed_dependencies_corruption_and_incomplete_are_not_published(self):
        for mutation in ("dependency", "incomplete", "corrupt"):
            with self.subTest(mutation=mutation):
                records = json.loads(json.dumps(self.records))
                if mutation == "dependency":
                    records[0]["payload"]["history_base"] = {"id": "parent"}
                elif mutation == "incomplete":
                    records.pop()
                raw = b"".join((json.dumps({**r, "ordinal": i}) + "\n").encode()
                                for i, r in enumerate(records))
                self.packed.write_bytes(zstd.compress(raw) if mutation != "corrupt" else b"invalid")
                self.plain.unlink(missing_ok=True)
                with self.assertRaises((ValueError, EOFError, OSError, zstd.ZstdError)):
                    self.export(packed=True)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".native-export-*")))

    def test_source_and_sqlite_changes_abort_before_publishing(self):
        for mutation in ("source", "index", "stat"):
            with self.subTest(mutation=mutation):
                self.plain.write_bytes(self.raw)
                self.index(self.plain)
                def changed(raw):
                    result = _parse(raw)
                    if mutation == "source":
                        self.plain.write_bytes(raw + b"\n")
                    elif mutation == "index":
                        self.index(self.packed)
                    return result
                with patch("history_stage._parse", side_effect=changed):
                    if mutation == "stat":
                        with patch("history_stage.file_signature", side_effect=[(1,), (2,)]):
                            with self.assertRaisesRegex(ValueError, "changed"):
                                self.export()
                    else:
                        with self.assertRaisesRegex(ValueError, "changed"):
                            self.export()
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".native-export-*")))

    def test_relative_and_outside_index_paths_rejected(self):
        for path in (self.relative, self.root / "outside.jsonl"):
            with self.subTest(path=path):
                if path.is_absolute():
                    path.write_bytes(self.raw)
                self.index(path)
                with self.assertRaisesRegex(ValueError, "absolute|outside"):
                    self.export()
                self.assertFalse(self.output.exists())

    @unittest.skipIf(zstd is None, "standard compression.zstd unavailable")
    def test_compressed_source_edit_or_new_plain_mate_aborts(self):
        self.compress()
        for mutation in ("packet", "plain"):
            with self.subTest(mutation=mutation):
                self.packed.write_bytes(zstd.compress(self.raw))
                self.plain.unlink(missing_ok=True)
                def changed(raw):
                    result = _parse(raw)
                    if mutation == "packet":
                        self.packed.write_bytes(b"changed compressed source")
                    else:
                        self.plain.write_bytes(raw)
                    return result
                with patch("history_stage._parse", side_effect=changed):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        self.export(packed=True)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".native-export-*")))

    def test_containment_checks_actual_compressed_and_canonical_plain_symlinks(self):
        outside = self.root / "outside.jsonl.zst"
        outside.write_bytes(b"not read")
        self.plain.unlink()
        try:
            self.packed.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "outside"):
            self.export()
        self.packed.unlink()
        self.packed.write_bytes(b"not read")
        # A dangling canonical symlink must not evade the canonical path check.
        self.plain.symlink_to(self.root / "missing.jsonl")
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())
