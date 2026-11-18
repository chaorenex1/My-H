"""gopass: release urls, the Termux gap, flat staging, and the lock row."""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from pm import Lockfile, Store, get_package, paths

# target -> (release platform, release arch, archive suffix)
_TARGETS = {
    "win32-x64": ("windows", "amd64", "zip"),
    "win32-arm64": ("windows", "arm64", "zip"),
    "linux-x64": ("linux", "amd64", "tar.gz"),
    "linux-arm64": ("linux", "arm64", "tar.gz"),
    "linux-x64-musl": ("linux", "amd64", "tar.gz"),
    "linux-arm64-musl": ("linux", "arm64", "tar.gz"),
    "darwin-x64": ("darwin", "amd64", "tar.gz"),
    "darwin-arm64": ("darwin", "arm64", "tar.gz"),
}


def _locked_version() -> str:
    version = Lockfile(paths.lockfile_path()).version("gopass")
    assert version
    return version


@pytest.mark.parametrize("target,asset", sorted(_TARGETS.items()))
def test_release_url_per_target(target, asset):
    platform, arch, ext = asset
    version = _locked_version()
    assert get_package("gopass").fetch_url(version, target) == (
        f"https://github.com/gopasspw/gopass/releases/download/v{version}/"
        f"gopass-{version}-{platform}-{arch}.{ext}"
    )


def test_musl_reuses_the_static_linux_archive():
    package = get_package("gopass")
    version = _locked_version()
    assert package.fetch_url(version, "linux-x64-musl") == package.fetch_url(version, "linux-x64")
    assert package.fetch_url(version, "linux-arm64-musl") == package.fetch_url(version, "linux-arm64")


def test_no_termux_build():
    package = get_package("gopass")
    assert "Android" in (package.missing_reason("linux-arm64-bionic") or "")


def test_lock_covers_every_supported_target():
    package = get_package("gopass")
    version = _locked_version()
    pinned = Lockfile(paths.lockfile_path()).pinned_artifacts("gopass")
    assert set(pinned) == set(_TARGETS)
    for target in _TARGETS:
        assert pinned[target]["url"] == package.fetch_url(version, target)
        assert len(pinned[target]["sha256"]) == 64


def test_versions_strip_the_release_prefix(monkeypatch):
    from pm import security_packages

    monkeypatch.setattr(security_packages, "github_release_tags",
                        lambda repo, strip_prefix="": ["1.17.4", "1.17.3"] if strip_prefix == "v" else [])
    assert get_package("gopass").latest_versions("linux-x64") == ["1.17.4", "1.17.3"]


@pytest.mark.parametrize("target", ["win32-x64", "linux-x64"])
def test_flat_archive_stages_the_binary(tmp_path, target):
    package = get_package("gopass")
    version = _locked_version()
    if target.startswith("win32"):
        archive = tmp_path / "gopass.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("gopass.exe", b"MZ")
        expected = "gopass.exe"
    else:
        archive = tmp_path / "gopass.tar.gz"
        payload = tmp_path / "gopass"
        payload.write_bytes(b"\x7fELF")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname="gopass")
        expected = "gopass"

    staged = tmp_path / "staged"
    package.unpack(archive, staged, target)
    package.stage(Store(tmp_path / "store"), staged, version, target)

    assert package.binary(staged, target) == staged / expected
