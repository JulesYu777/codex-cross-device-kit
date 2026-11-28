#!/usr/bin/env python3
"""Deploy and verify a dedicated two-device Syncthing instance."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FOLDER_ID = "codex-worktree-v1"
DEVICE_ID = re.compile(r"[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}\Z")
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{3,31}\Z")
TAILNET = ipaddress.ip_network("100.64.0.0/10")
IGNORE = """// codex-cross-device-kit v1
(?i).git
(?i).codex
(?i).env
(?i).env.*
(?i)*.pem
(?i)*.key
(?i)node_modules
(?i)__pycache__
(?i).DS_Store
(?i)Thumbs.db
"""
CONTROL = {".stignore", ".stfolder", ".stversions"}


def fail(message):
    raise ValueError(message)


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def write_json(path, obj):
    atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    with path.open("rb") as stream:
        sha = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def skipped(name):
    name = name.casefold()
    return (name in CONTROL | {".git", ".codex", ".env", "node_modules", "__pycache__", ".ds_store", "thumbs.db"}
            or name.startswith(".env.") or name.endswith((".pem", ".key")))


def portable(path):
    # Win32 reserved names and normalization can change the meaning of a replica.
    for name in path.split("/"):
        if (not name or name in {".", ".."} or name.endswith((".", " "))
                or unicodedata.normalize("NFC", name) != name):
            return False
        if any(ord(c) < 32 or c in '<>:"\\|?*' for c in name):
            return False
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", name.split(".")[0], re.I):
            return False
    return True


def inventory(folder):
    result = {}
    folded = set()
    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not skipped(name))
        for name in dirs + sorted(name for name in files if not skipped(name)):
            path = Path(root) / name
            rel = path.relative_to(folder).as_posix()
            if not portable(rel) or rel.casefold() in folded:
                fail(f"Non-portable or case-colliding path: {rel}")
            folded.add(rel.casefold())
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                fail(f"Unsupported link or special file: {rel}")
            if path.is_dir():
                result[rel] = "directory"
            else:
                before = path.stat()
                signature = digest(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    fail(f"File changed during inventory: {rel}")
                result[rel] = signature
    return result


def address(value, lab=False):
    ip = ipaddress.IPv4Address(value)
    if ip not in TAILNET and not (lab and ip.is_loopback):
        fail("Use a Tailscale IPv4 address, obtained with tailscale ip -4.")
    return str(ip)


def port(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        fail("Use an unprivileged TCP port between 1024 and 65535.")
    return value


def load_offer(path, lab=False):
    data = read_json(path)
    if (not isinstance(data, dict) or set(data) != {"version", "role", "device", "address", "port", "files"}
            or data["version"] != 1 or data["role"] not in ("mac", "windows")
            or not isinstance(data["device"], str) or not DEVICE_ID.fullmatch(data["device"])
            or not isinstance(data["files"], dict)):
        fail("Invalid peer offer.")
    address(data["address"], lab)
    port(data["port"])
    cases = set()
    for name, fingerprint in data["files"].items():
        if (not portable(name) or any(skipped(x) for x in name.split("/")) or name.casefold() in cases
                or not isinstance(fingerprint, str)
                or not re.fullmatch(r"directory|[0-9a-f]{64}", fingerprint)):
            fail("Invalid or ambiguous path inventory in peer offer.")
        cases.add(name.casefold())
    return data


def configured_child(root, tag):
    child = root.find(tag)
    if child is None:
        fail(f"Syncthing configuration has no {tag} element.")
    return child


def set_value(root, tag, value):
    for child in root.findall(tag):
        root.remove(child)
    ET.SubElement(root, tag).text = value


def save_config(home, root):
    atomic(home / "config.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))


def socket_open(number):
    with socket.socket() as client:
        client.settimeout(0.3)
        return client.connect_ex(("127.0.0.1", number)) == 0


def check_paths(home, folder):
    if not folder.is_dir() or folder == Path(folder.anchor):
        fail("Choose an existing, dedicated worktree directory.")
    for reserved in (Path.home() / ".codex",):
        if folder == reserved or folder.is_relative_to(reserved) or reserved.is_relative_to(folder):
            fail("The shared directory cannot contain, or be, CODEX_HOME.")
    override = os.environ.get("CODEX_HOME")
    if override:
        reserved = Path(override).resolve()
        if folder == reserved or folder.is_relative_to(reserved) or reserved.is_relative_to(folder):
            fail("The shared directory cannot overlap the configured CODEX_HOME.")
    if home == folder or home.is_relative_to(folder) or folder.is_relative_to(home):
        fail("Keep the dedicated Syncthing home outside the shared worktree.")
    ignore = folder / ".stignore"
    if ignore.is_symlink() or (ignore.exists() and ignore.read_bytes() != IGNORE.encode()):
        fail("This folder already has different .stignore content. Use a fresh replica.")


def prepare(home, folder, role, ip, listen, gui, binary, lab=False):
    ip, listen, gui = address(ip, lab), port(listen), port(gui)
    if listen == gui:
        fail("The GUI and file transfer ports must differ.")
    check_paths(home, folder)
    state_path = home / "kit.json"
    if state_path.exists():
        state = read_json(state_path)
        if [state[k] for k in ("folder", "role", "address", "port", "gui")] != [str(folder), role, ip, listen, gui]:
            fail("These choices differ from the existing deployment.")
        if "peer" not in state:
            if socket_open(gui):
                fail("Stop this instance before refreshing the offer.")
            offer = load_offer(home / "offer.json", lab)
            offer["files"] = inventory(folder)
            write_json(home / "offer.json", offer)
        return home / "offer.json"
    if home.exists() and any(home.iterdir()):
        fail("Choose a new or empty home. Existing Syncthing data is left alone.")
    if socket_open(gui):
        fail("The local GUI port is already occupied.")
    files = inventory(folder)
    exe = shutil.which(str(binary))
    if not exe:
        fail("Install Syncthing or give its executable path with --syncthing.")
    exe = str(Path(exe).resolve())
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ, STNODEFAULTFOLDER="1")
    subprocess.run([exe, "generate", f"--home={home}", "--no-port-probing"],
                   cwd=home, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    root = ET.parse(home / "config.xml").getroot()
    local = root.findall("device")
    if len(local) != 1 or not DEVICE_ID.fullmatch(local[0].get("id", "")):
        fail("Could not identify the generated Syncthing device.")
    for old in root.findall("folder"):
        root.remove(old)
    interface = configured_child(root, "gui")
    set_value(interface, "address", f"127.0.0.1:{gui}")
    options = configured_child(root, "options")
    for tag in ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled",
                "natEnabled", "crashReportingEnabled"):
        set_value(options, tag, "false")
    set_value(options, "urAccepted", "-1")
    set_value(options, "listenAddress", f"tcp://{ip}:{listen}")
    save_config(home, root)
    state = {"folder": str(folder), "role": role, "address": ip, "port": listen,
             "gui": gui, "binary": str(Path(exe).resolve()), "device": local[0].get("id")}
    offer = {k: state[k] for k in ("role", "address", "port", "device")}
    offer.update(version=1, files=files)
    write_json(home / "offer.json", offer)
    write_json(state_path, state)
    return home / "offer.json"


def apply(home, peer_path, lab=False):
    state = read_json(home / "kit.json")
    own = load_offer(home / "offer.json", lab)
    peer = load_offer(peer_path, lab)
    if (own["role"] == peer["role"] or own["device"] == peer["device"]
            or (own["address"], own["port"]) == (peer["address"], peer["port"])):
        fail("Choose the distinct offer from the other host.")
    if "peer" in state:
        if state["peer"] != {k: peer[k] for k in ("device", "address", "port")}:
            fail("This deployment is already paired with a different endpoint.")
        return
    if socket_open(state["gui"]):
        fail("Stop this instance before configuring it.")
    folder = Path(state["folder"])
    check_paths(home, folder)
    if inventory(folder) != own["files"]:
        fail("The local worktree changed since prepare. Take fresh offers before pairing.")
    if own["files"] and peer["files"] and own["files"] != peer["files"]:
        fail("The existing worktrees differ. Reconcile them or use an empty replica.")
    root = ET.parse(home / "config.xml").getroot()
    peers = {x.get("id") for x in root.findall("device")}
    if peers - {own["device"], peer["device"]}:
        fail("Unexpected third device in the dedicated configuration.")
    for item in root.findall("device"):
        if item.get("id") == peer["device"]:
            root.remove(item)
    device = ET.SubElement(root, "device", id=peer["device"], name=f"kit-{peer['role']}")
    ET.SubElement(device, "address").text = f"tcp://{peer['address']}:{peer['port']}"
    for existing in root.findall("folder"):
        if existing.get("id") != FOLDER_ID:
            fail("Unexpected folder in the dedicated configuration.")
        root.remove(existing)
    local = ET.SubElement(root, "folder", id=FOLDER_ID, label="Worktree", path=str(folder),
                          type="sendreceive", rescanIntervalS="60", fsWatcherEnabled="true")
    for ident in (own["device"], peer["device"]):
        ET.SubElement(local, "device", id=ident)
    ET.SubElement(local, "markerName").text = ".stfolder"
    versioning = ET.SubElement(local, "versioning", type="simple")
    ET.SubElement(versioning, "param", key="keep", val="5")
    ignore = folder / ".stignore"
    if not ignore.exists():
        with ignore.open("xb") as stream:
            stream.write(IGNORE.encode())
    save_config(home, root)
    state["peer"] = {k: peer[k] for k in ("device", "address", "port")}
    write_json(home / "kit.json", state)


def ready(home):
    state = read_json(home / "kit.json")
    if "peer" not in state:
        fail("Run apply with the other device's offer first.")
    return state


def endpoint(state):
    peer = state["peer"]
    return f"{peer['address']}:{peer['port']}"


def api(home, state, target, args=None, method="GET"):
    gui = configured_child(ET.parse(home / "config.xml").getroot(), "gui")
    key = gui.findtext("apikey")
    if not key:
        fail("The generated local GUI API key is missing.")
    query = "?" + urllib.parse.urlencode(args) if args else ""
    url = f"http://127.0.0.1:{state['gui']}/rest/{target}{query}"
    req = urllib.request.Request(url, method=method, headers={"X-API-Key": key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as response:
        body = response.read()
    return json.loads(body) if body else None


def doctor(home, state):
    peer = state["peer"]["device"]
    connection = api(home, state, "system/connections")["connections"].get(peer, {})
    local = api(home, state, "db/status", {"folder": FOLDER_ID})
    remote = api(home, state, "db/completion", {"folder": FOLDER_ID, "device": peer})
    # Incoming TCP can have an ephemeral source port.
    pointer = connection.get("address") or ""
    actual = pointer.rpartition(":")[0]
    return {"connected": connection.get("connected", False),
            "expected_peer_ip": actual == state["peer"]["address"],
            "address": pointer, "local_pending": local["needTotalItems"],
            "pull_errors": local["pullErrors"], "peer_completion": remote["completion"],
            "peer_pending": remote["needItems"], "peer_deletes": remote["needDeletes"],
            "peer_folder_state": remote.get("remoteState")}


def start_args(home, state):
    return [state["binary"], "serve", f"--home={home}", "--no-browser", "--no-restart", "--no-upgrade"]


WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def windows_run_entry(name, command, subkey=WINDOWS_RUN_KEY):
    import winreg
    if len(command) > 260:
        fail("The Windows login command is too long. Choose a shorter kit or state path.")
    access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, access) as key:
        try:
            existing = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        else:
            if existing != (command, winreg.REG_SZ):
                fail("A different Windows startup entry already exists.")


def service(home, state):
    suffix = hashlib.sha256(str(home).encode()).hexdigest()[:12]
    if sys.platform == "darwin":
        label = "org.codex-cross-device-kit." + suffix
        dest = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        paths = {"Label": label, "ProgramArguments": start_args(home, state),
                 "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                 "StandardOutPath": str(home / "service.log"), "StandardErrorPath": str(home / "service.log")}
        data = plistlib.dumps(paths)
    elif os.name == "nt":
        name = f"CodexCrossDeviceKit-{suffix}"
        windows_run_entry(name, subprocess.list2cmdline(start_args(home, state)))
        print(f"HKCU Run value: {name}")
        return
    else:
        fail("Login service setup is available on macOS and Windows.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() != data:
        fail("A different startup entry already exists.")
    if not dest.exists():
        with dest.open("xb") as result:
            result.write(data)
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}"
        registered = subprocess.run(["launchctl", "print", f"{target}/{label}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not registered:
            subprocess.run(["launchctl", "bootstrap", target, str(dest)], check=True)
    print(dest)


def probe(home, state, action, run, wait):
    if not RUN_ID.fullmatch(run):
        fail("Use a unique run name of 4–32 lowercase letters, digits or hyphens.")
    role = state["role"]
    other = "windows" if role == "mac" else "mac"
    owner = other if action == "read" else role
    name = f"syncthing-acceptance-{run}-{owner}.txt"
    path = Path(state["folder"]) / name
    payload = f"codex-cross-device-kit {run} {owner}\n".encode("ascii")
    if action != "read":
        api(home, state, "system/ping")
    if action == "write" and not (path.exists() or path.is_symlink()):
        with path.open("xb") as marker:
            marker.write(payload)
    elif action == "read":
        deadline = time.monotonic() + wait
        while not path.is_file():
            if time.monotonic() >= deadline:
                fail(f"The remote marker {name} did not arrive.")
            time.sleep(0.25)
    if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
        fail(f"Unexpected marker content: {name}")
    if action == "clean":
        path.unlink()
    if action != "read":
        api(home, state, "db/scan", {"folder": FOLDER_ID, "sub": name}, method="POST")
    return hashlib.sha256(payload).hexdigest()


def main():
    from history_cli import add_commands as add_history_commands, run as run_history

    parser = argparse.ArgumentParser(description=__doc__)
    default_home = Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "codex-cross-device-kit"
    parser.add_argument("--home", type=Path, default=default_home)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--folder", required=True, type=Path)
    prep.add_argument("--role", required=True, choices=("mac", "windows"))
    prep.add_argument("--address", required=True)
    prep.add_argument("--listen-port", type=int, default=22020)
    prep.add_argument("--gui-port", type=int, default=18384)
    prep.add_argument("--syncthing", default="syncthing")
    commands.add_parser("apply").add_argument("peer_offer", type=Path)
    commands.add_parser("start")
    commands.add_parser("service")
    checks = commands.add_parser("doctor")
    checks.add_argument("--require-ready", action="store_true")
    mark = commands.add_parser("probe")
    mark.add_argument("action", choices=("write", "read", "clean"))
    mark.add_argument("run")
    mark.add_argument("--wait", type=int, default=45)
    add_history_commands(commands.add_parser("history", help="export, synchronize, and recover native history"))
    args = parser.parse_args()
    if args.command == "history":
        return run_history(args)
    home = args.home.expanduser().resolve()
    os.umask(0o077)
    if args.command == "prepare":
        print(prepare(home, args.folder.expanduser().resolve(), args.role, args.address,
                      args.listen_port, args.gui_port, args.syncthing))
    elif args.command == "apply":
        apply(home, args.peer_offer)
        print("Paired configuration ready.")
    else:
        state = ready(home)
        if args.command == "start":
            return subprocess.call(start_args(home, state))
        if args.command == "service":
            service(home, state)
        if args.command == "doctor":
            result = doctor(home, state)
            print(json.dumps(result, indent=2))
            if args.require_ready:
                return 0 if (result["connected"] and result["expected_peer_ip"]
                             and result["peer_completion"] == 100 and result["peer_folder_state"] == "valid"
                             and not any(result[k] for k in ("local_pending", "pull_errors", "peer_pending", "peer_deletes"))) else 1
        if args.command == "probe":
            print(probe(home, state, args.action, args.run, args.wait))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Error: {exc}")
