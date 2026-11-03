"""Repository discovery, file scanning, exclusions and hashing.

The scanner never imports the graph or a parser. It answers two questions for
one repository root: what is it (git remote/commit/dirty) and which source
files should the indexer read.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from .exceptions import CodeGraphError, IndexLimitError
from .languages import LanguageDescriptor, LanguageRegistry

_EXCLUDED_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build",
        "vendor", "target", ".next", "coverage", ".coverage", "htmlcov", ".tox",
        ".mypy_cache", ".pytest_cache", "__pycache__", ".idea", ".gradle",
    }
)
_SENSITIVE_NAMES = frozenset(
    {".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials"}
)
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
_SENSITIVE_PREFIXES = (".env.", "credentials.")
_GIT_TIMEOUT_S = 10


@dataclass(frozen=True)
class RepositoryInfo:
    root: Path
    key: str
    repository_id: str
    name: str
    remote_url: str | None
    commit_hash: str
    branch: str
    is_dirty: bool
    is_git: bool

    @property
    def revision_key(self) -> str:
        if not self.is_git or not self.commit_hash:
            return f"{self.key}@nogit@dirty"
        return f"{self.key}@{self.commit_hash}" + ("@dirty" if self.is_dirty else "")

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "key": self.key,
            "repository_id": self.repository_id,
            "name": self.name,
            "remote_url": self.remote_url,
            "commit_hash": self.commit_hash,
            "branch": self.branch,
            "is_dirty": self.is_dirty,
            "is_git": self.is_git,
            "revision_key": self.revision_key,
        }


@dataclass(frozen=True)
class ScannedFile:
    rel_path: str
    path: Path
    size_bytes: int
    language: str


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    limit_error: IndexLimitError | None = None
    total_bytes: int = 0


def git_available(root: Path) -> bool:
    return _git_text(root, ["rev-parse", "--is-inside-work-tree"]) == "true"


def resolve_repository(root: str | os.PathLike[str] | None = None) -> RepositoryInfo:
    base = Path(root if root is not None else os.getcwd())
    if not base.exists():
        raise CodeGraphError(f"repository root does not exist: {base}")
    if not base.is_dir():
        base = base.parent
    base = base.resolve()

    toplevel = _git_text(base, ["rev-parse", "--show-toplevel"])
    is_git = bool(toplevel)
    actual = Path(toplevel).resolve() if toplevel else base
    remote = _git_text(actual, ["config", "--get", "remote.origin.url"]) or None
    sanitized = sanitize_remote(remote)
    commit = _git_text(actual, ["rev-parse", "HEAD"]) or ""
    branch = _git_text(actual, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
    dirty = bool(_git_bytes(actual, ["status", "--porcelain", "-z"])) if is_git else True
    key = (
        f"git:{sanitized}" if sanitized else
        f"path:{hashlib.sha256(str(actual).encode('utf-8')).hexdigest()[:16]}"
    )
    repository_id = "repo:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return RepositoryInfo(
        root=actual,
        key=key,
        repository_id=repository_id,
        name=actual.name,
        remote_url=sanitized,
        commit_hash=commit,
        branch=branch,
        is_dirty=dirty,
        is_git=is_git,
    )


_SCP_RE = re.compile(r"^(?P<user>[^@/\\]+)@(?P<host>[^:/\\]+):(?P<path>.+)$")


def sanitize_remote(url: str | None) -> str | None:
    """Strip credentials, query, fragment and trailing ``.git`` from a remote.

    Returns ``None`` for local paths (which carry no stable remote identity).
    """
    if not url:
        return None
    value = url.strip()
    if not value:
        return None
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return None
        if not parts.hostname:
            return None
        host = parts.hostname.lower()
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path.rstrip("/").removesuffix(".git")
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    if value.startswith("\\\\") or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    match = _SCP_RE.match(value)
    if match:
        host = match.group("host").lower()
        path = match.group("path").rstrip("/").removesuffix(".git")
        return f"{host}:{path}"
    return None


def scan(
    repo: RepositoryInfo,
    registry: LanguageRegistry,
    *,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
    extra_excludes: Iterable[str] = (),
) -> ScanResult:
    result = ScanResult()
    excludes = tuple(extra_excludes)
    try:
        paths = _git_paths(repo.root) if repo.is_git else _walk_paths(repo.root)
        for rel in paths:
            if _excluded_path(rel, excludes):
                continue
            descriptor = registry.by_path(rel)
            if descriptor is None:
                continue
            absolute = _safe_path(repo.root, rel)
            if absolute is None:
                result.skipped.append({"path": rel, "reason": "outside_root"})
                continue
            try:
                info = absolute.lstat()
            except OSError as exc:
                result.skipped.append({"path": rel, "reason": f"stat_failed: {exc}"})
                continue
            if not stat.S_ISREG(info.st_mode):
                result.skipped.append({"path": rel, "reason": "not_a_regular_file"})
                continue
            size = int(info.st_size)
            if size > max_file_bytes:
                result.skipped.append({"path": rel, "reason": "file_too_large"})
                continue
            if len(result.files) >= max_files:
                raise IndexLimitError(
                    f"repository has more than {max_files} source files",
                    details={"max_files": max_files, "path": rel},
                )
            if result.total_bytes + size > max_total_bytes:
                raise IndexLimitError(
                    f"repository source exceeds {max_total_bytes} bytes",
                    details={"max_total_bytes": max_total_bytes, "path": rel},
                )
            result.total_bytes += size
            result.files.append(
                ScannedFile(
                    rel_path=rel,
                    path=absolute,
                    size_bytes=size,
                    language=descriptor.language,
                )
            )
    except IndexLimitError as exc:
        result.limit_error = exc
    result.files.sort(key=lambda item: item.rel_path)
    return result


def read_source(path: Path, *, max_bytes: int) -> bytes | None:
    """Read a source file, or ``None`` when it grew past the limit."""
    if path.stat().st_size > max_bytes:
        return None
    data = path.read_bytes()
    return data if len(data) <= max_bytes else None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _git_paths(root: Path) -> list[str]:
    data = _git_bytes(root, ["ls-files", "-co", "--exclude-standard", "-z"]) or b""
    paths: list[str] = []
    for raw in data.split(b"\x00"):
        if not raw:
            continue
        rel = os.fsdecode(raw).replace("\\", "/")
        if rel:
            paths.append(rel)
    return sorted(set(paths))


def _walk_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            candidate = current / name
            if name.casefold() in _EXCLUDED_DIRS or candidate.is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            absolute = current / name
            try:
                rel = absolute.relative_to(root).as_posix()
            except ValueError:
                continue
            paths.append(rel)
    return paths


def _excluded_path(rel: str, extra: tuple[str, ...]) -> bool:
    parts = [part for part in rel.replace("\\", "/").split("/") if part]
    if any(part.casefold() in _EXCLUDED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else rel
    lowered = name.casefold()
    if lowered in _SENSITIVE_NAMES:
        return True
    if any(lowered.startswith(prefix) for prefix in _SENSITIVE_PREFIXES):
        return True
    if any(lowered.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
        return True
    posix = "/".join(parts)
    for pattern in extra:
        cleaned = pattern.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned.endswith("/"):
            prefix = cleaned.rstrip("/")
            if posix == prefix or posix.startswith(prefix + "/"):
                return True
            continue
        if fnmatch.fnmatchcase(posix, cleaned) or fnmatch.fnmatchcase(name, cleaned):
            return True
    return False


def _safe_path(root: Path, rel: str) -> Path | None:
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _git_text(root: Path, args: list[str]) -> str | None:
    data = _git_bytes(root, args)
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    return text or None


def _git_bytes(root: Path, args: list[str]) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def find_descriptor(registry: LanguageRegistry, language: str) -> LanguageDescriptor:
    return registry.get(language)
