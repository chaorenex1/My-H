"""Bun package: release urls, the Termux gap, zip staging, and the lock row."""

from __future__ import annotations

import zipfile

import pytest

from pm import Lockfile, Store, get_package, paths
from pm.package import InstallError

# target -> the platform token in Bun's release asset name.
_TARGETS = {
    "win32-x64": "windows-x64",
    "win32-arm64": "windows-aarch64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-aarch64",
    "linux-x64-musl": "linux-x64-musl",
    "linux-arm64-musl": "linux-aarch64-musl",
    "darwin-x64": "darwin-x64",
    "darwin-arm64": "darwin-aarch64",
}


@pytest.mark.parametrize("target,platform_token", sorted(_TARGETS.items()))
def test_bun_release_url_per_target(target, platform_token):
    package = get_package("bun")
    assert package.fetch_url("1.4.4", target) == (
        "https://github.com/oven-sh/bun/releases/download/bun-v1.4.4/"
        f"bun-{platform_token}.zip"
    )


def test_bun_has_no_termux_build():
    package = get_package("bun")
    assert package.missing_reason("linux-arm64-bionic")
    with pytest.raises(InstallError):
        package.fetch_url("1.4.4", "linux-arm64-bionic")


@pytest.mark.parametrize("target,member,relative", [
    ("win32-x64", "bun-windows-x64/bun.exe", "bun.exe"),
    ("linux-x64", "bun-linux-x64/bun", "bun"),
])
def test_bun_zip_stages_the_binary(tmp_path, target, member, relative):
    package = get_package("bun")
    archive = tmp_path / "bun.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(member, b"#!fixture")
    staged = tmp_path / "staged"
    package.unpack(archive, staged, target)
    package.stage(Store(tmp_path / "store"), staged, "1.4.4", target)
    assert package.binary(staged, target) == staged / relative


def test_bun_lock_covers_every_supported_target():
    package = get_package("bun")
    lock = Lockfile(paths.lockfile_path())
    version = lock.version("bun")
    assert version
    pinned = lock.pinned_artifacts("bun")
    assert set(pinned) == set(_TARGETS)
    for target in _TARGETS:
        assert pinned[target]["url"] == package.fetch_url(version, target)
        assert len(pinned[target]["sha256"]) == 64


def test_bun_digest_comes_from_the_release_api(monkeypatch):
    from pm import packages

    monkeypatch.setattr(packages, "_github_release_digests",
                        lambda repo, tag: {"bun-linux-x64.zip": "a" * 64})
    package = get_package("bun")
    assert package.known_sha256("1.4.4", package.fetch_url("1.4.4", "linux-x64")) == "a" * 64


def test_bun_versions_strip_the_release_prefix(monkeypatch):
    from pm import packages

    monkeypatch.setattr(packages, "github_release_tags",
                        lambda repo, strip_prefix="": ["1.4.4", "1.4.3"] if strip_prefix == "bun-v" else [])
    assert get_package("bun").latest_versions("linux-x64") == ["1.4.4", "1.4.3"]
