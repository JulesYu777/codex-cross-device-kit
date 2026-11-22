#!/usr/bin/env python3
"""Fetch the verified Syncthing binary used by the deployment and CI."""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import tempfile
import urllib.request
import zipfile

VERSION = "2.1.7"
CHECKSUMS = {
    ("Darwin", "amd64"): "b8be90176d0f24daebae7b5aaddac52ebfae395df6d99f5e0e54656db00868cb",
    ("Darwin", "arm64"): "2df075b463231a307c91da657157beaed06383a941ba7ea04d33d536e3c4af38",
    ("Windows", "amd64"): "3ce7da770f546a8d02b65b2e7ee31f2a3b8b7c3f990ede045590cca4172ecfe1",
    ("Windows", "arm64"): "d535ffd611b3af223de64bc5dfc60c378881eb13d5c8ecb1431b7407094c15ff",
}


def install(target):
    name = platform.system()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    key = (name, arch)
    if key not in CHECKSUMS:
        raise ValueError(f"Unsupported platform: {name} {machine}")
    release = f"syncthing-{'macos' if name == 'Darwin' else 'windows'}-{arch}-v{VERSION}"
    binary = "syncthing.exe" if name == "Windows" else "syncthing"
    dest = target / binary
    target.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/syncthing/syncthing/releases/download/v{VERSION}/{release}.zip"
    with tempfile.TemporaryDirectory(dir=target) as temp:
        archive = Path(temp) / "binary.zip"
        with urllib.request.urlopen(url, timeout=30) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != CHECKSUMS[key]:
            raise ValueError("Syncthing release archive checksum does not match.")
        with zipfile.ZipFile(archive) as package:
            candidates = [x for x in package.namelist() if x.split("/") == [release, binary]]
            if len(candidates) != 1:
                raise ValueError("Unexpected Syncthing release archive layout.")
            data = package.read(candidates[0])
        if dest.exists() and (dest.is_symlink() or dest.read_bytes() != data):
            raise ValueError("A different Syncthing binary already exists in the output directory.")
        if not dest.exists():
            prepared = Path(temp) / binary
            prepared.write_bytes(data)
            prepared.chmod(0o755)
            prepared.replace(dest)
    return dest.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "bin")
    args = parser.parse_args()
    try:
        print(install(args.out_dir))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
