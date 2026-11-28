"""Read-only access to native plain and Zstandard rollout siblings."""
from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryFile

try:
    from compression.zstd import ZstdFile
except ImportError:
    ZstdFile = None


# The largest observed archived packet is 67 MiB compressed. Keep the decoded
# allocation bounded independently of frame metadata; callers may lower/raise it.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


def _plain_path(path):
    path = Path(path)
    if path.name.endswith(".jsonl.zst"):
        return path.with_suffix("")
    if path.suffix == ".jsonl":
        return path
    raise ValueError("Expected a .jsonl or .jsonl.zst rollout path")


def resolve_rollout(indexed: Path) -> tuple[Path, Path]:
    """Return the canonical plain name and existing source, preferring plain.

    Paths retain their lexical names: export must separately resolve and check
    containment of both the transport name and the actual source.
    """
    indexed = Path(indexed)
    canonical = _plain_path(indexed)
    if indexed.is_dir():
        raise ValueError("Rollout path is a directory")
    for candidate in (canonical, Path(str(canonical) + ".zst")):
        if candidate.is_dir():
            raise ValueError("Rollout path is a directory")
        if candidate.is_file():
            return canonical, candidate
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("Rollout path is not a regular file")
    raise FileNotFoundError(f"No rollout file for {indexed}")


def file_signature(path):
    """Stat evidence for replacement or in-place edits of a source snapshot."""
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_bounded(stream, max_bytes):
    chunks, total = [], 0
    while True:
        chunk = stream.read(min(_CHUNK_BYTES, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Rollout exceeds the decoded byte limit")
        chunks.append(chunk)


def read_rollout(path: Path, *, decompressor: str | Path | None = None,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Read exactly one file, never discovering profiles or changing storage.

    Prefer the standard decoder when available. Older Python requires an
    explicitly supplied existing zstd executable, invoked without a shell.
    Both decoders must reach EOF successfully, including checksum validation.
    """
    path = Path(path)
    canonical = _plain_path(path)
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    if not path.is_file():
        if path.is_dir():
            raise ValueError("Rollout path is a directory")
        raise FileNotFoundError(path)
    if path == canonical:
        with path.open("rb") as stream:
            return _read_bounded(stream, max_bytes)
    if path.stat().st_size == 0:
        raise ValueError("Empty compressed rollout is not a Zstandard frame")
    if ZstdFile is not None:
        with ZstdFile(path, "rb") as stream:
            return _read_bounded(stream, max_bytes)
    if decompressor is None:
        raise ValueError("Compressed rollout requires an explicit zstd executable")
    executable = Path(decompressor).resolve(strict=True)
    if not executable.is_file():
        raise ValueError("zstd executable must be an existing file")
    # A file for stderr prevents pipe deadlock and unbounded captured diagnostics.
    with TemporaryFile() as errors:
        process = subprocess.Popen(
            [str(executable), "-d", "-c", "--", str(path.resolve(strict=True))],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
            shell=False)
        try:
            raw = _read_bounded(process.stdout, max_bytes)
            if process.wait() != 0:
                raise ValueError("zstd failed to decode rollout (corrupt or truncated input)")
            return raw
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()