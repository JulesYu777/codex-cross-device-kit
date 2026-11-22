import os
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import kit


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    return ports


def wait_until(check, time_limit=50):
    end = time.monotonic() + time_limit
    while time.monotonic() < end:
        try:
            if check():
                return
        except (OSError, KeyError):
            pass
        time.sleep(0.25)
    raise AssertionError("Condition not reached")


@contextmanager
def running(home):
    with (home / "test.log").open("ab") as log:
        process = subprocess.Popen(kit.start_args(home, kit.ready(home)), stdout=log, stderr=log)
        try:
            yield
        except Exception:
            print((home / "test.log").read_text(encoding="utf-8", errors="replace")[-4000:])
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)


class InventoryTest(unittest.TestCase):
    def test_exclusions_and_file_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".git").mkdir()
            (root / ".git/index").write_bytes(b"unchanged")
            (root / ".env.local").write_text("sample")
            (root / "keep.txt").write_text("hello")
            expected = {"keep.txt": kit.digest(root / "keep.txt")}
            self.assertEqual(kit.inventory(root), expected)

    def test_windows_names(self):
        for name in ("con.txt", "a?/t", "trailing.", "dir/LPT2"):
            self.assertFalse(kit.portable(name))
        self.assertTrue(kit.portable("dir/更新.txt"))

    def test_offer_rejects_non_tailnet_and_lateral_path(self):
        self.assertEqual(kit.address("100.64.0.1"), "100.64.0.1")
        with self.assertRaises(ValueError):
            kit.address("192.168.1.2")
        self.assertFalse(kit.portable("../outside"))


@unittest.skipUnless(os.getenv("SYNCTHING_BIN"), "set SYNCTHING_BIN for actual two-process integration")
class RealSyncthingTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Startup shortcut only")
    def test_windows_startup_shortcut_in_disposable_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "kit space"
            home.mkdir()
            appdata = root / "测试配置"
            state = {"binary": str(Path(os.environ["SYNCTHING_BIN"]).resolve())}
            with patch.dict(os.environ, {"APPDATA": str(appdata)}):
                kit.service(home, state)
                kit.service(home, state)
            self.assertEqual(len(list(appdata.rglob("CodexCrossDeviceKit-*.lnk"))), 1)

    def test_pair_transfer_partition_restart_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mac, win = tmp / "mac", tmp / "win"
            mac.mkdir()
            win.mkdir()
            for root in (mac, win):
                (root / ".git").mkdir()
                (root / ".git/index").write_bytes(root.name.encode())
            (mac / ".env").write_text("synthetic-fixture")
            (mac / "baseline.txt").write_text("starter")
            (win / "baseline.txt").write_text("unrelated")
            a, b = tmp / "state-mac", tmp / "state-win"
            p, q, g, h = free_ports(4)
            binary = os.environ["SYNCTHING_BIN"]
            a_offer = kit.prepare(a, mac, "mac", "127.0.0.1", p, g, binary, lab=True)
            b_offer = kit.prepare(b, win, "windows", "127.0.0.1", q, h, binary, lab=True)
            with self.assertRaisesRegex(ValueError, "worktrees differ"):
                kit.apply(a, b_offer, lab=True)
            self.assertFalse((mac / ".stignore").exists())
            (win / "baseline.txt").write_text("starter")
            b_offer = kit.prepare(b, win, "windows", "127.0.0.1", q, h, binary, lab=True)
            kit.apply(a, b_offer, lab=True)
            kit.apply(b, a_offer, lab=True)
            kit.apply(a, b_offer, lab=True)
            sa, sb = kit.ready(a), kit.ready(b)
            for home, port in ((a, p), (b, q)):
                root = ET.parse(home / "config.xml").getroot()
                self.assertEqual(root.findtext("options/listenAddress"), f"tcp://127.0.0.1:{port}")
                self.assertEqual(root.findtext("options/globalAnnounceEnabled"), "false")
                self.assertEqual(root.findtext("options/relaysEnabled"), "false")
            with running(a):
                with running(b):
                    wait_until(lambda: kit.doctor(a, sa)["connected"] and kit.doctor(b, sb)["connected"])
                    self.assertEqual(kit.probe(a, sa, "write", "trial-001", 30),
                                     kit.probe(b, sb, "read", "trial-001", 30))
                    self.assertEqual(kit.probe(b, sb, "write", "trial-001", 30),
                                     kit.probe(a, sa, "read", "trial-001", 30))
                wait_until(lambda: not kit.doctor(a, sa)["connected"])
                before = kit.probe(a, sa, "write", "offline-01", 30)
                self.assertFalse((win / "syncthing-acceptance-offline-01-mac.txt").exists())
                with running(b):
                    self.assertEqual(before, kit.probe(b, sb, "read", "offline-01", 40))
                    kit.probe(a, sa, "clean", "trial-001", 30)
                    kit.probe(b, sb, "clean", "trial-001", 30)
                    kit.probe(a, sa, "clean", "offline-01", 30)
                    wait_until(lambda: not list(mac.glob("syncthing-acceptance-*.txt"))
                               and not list(win.glob("syncthing-acceptance-*.txt")))
                    wait_until(lambda: kit.doctor(a, sa)["peer_completion"] == 100
                               and kit.doctor(b, sb)["peer_completion"] == 100)
                    self.assertEqual((mac / ".git/index").read_bytes(), b"mac")
                    self.assertEqual((win / ".git/index").read_bytes(), b"win")
                    self.assertFalse((win / ".env").exists())
