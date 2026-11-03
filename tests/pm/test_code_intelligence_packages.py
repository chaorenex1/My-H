"""Code-intelligence packages: release urls, gaps, staging, and the lock rows.

ast-grep, tree-sitter and CodeQL pin release archives; semgrep has no binary
upstream and is realized as an isolated Python tool environment.
"""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from pm import Lockfile, Store, get_package, paths
from pm.package import PythonToolPackage
from pm.store import ALL_TARGETS


def _version(name: str) -> str:
    version = Lockfile(paths.lockfile_path()).version(name)
    assert version
    return version


_BINARY_EXPECTATIONS = {
    # name: (target -> url, target -> archive suffix)
    "ast-grep": {
        "win32-x64": "app-x86_64-pc-windows-msvc.zip",
        "win32-arm64": "app-aarch64-pc-windows-msvc.zip",
        "linux-x64": "app-x86_64-unknown-linux-gnu.zip",
        "linux-arm64": "app-aarch64-unknown-linux-gnu.zip",
        "darwin-x64": "app-x86_64-apple-darwin.zip",
        "darwin-arm64": "app-aarch64-apple-darwin.zip",
    },
    "tree-sitter": {
        "win32-x64": "tree-sitter-cli-windows-x64.zip",
        "win32-arm64": "tree-sitter-cli-windows-arm64.zip",
        "linux-x64": "tree-sitter-cli-linux-x64.zip",
        "linux-arm64": "tree-sitter-cli-linux-arm64.zip",
        "darwin-x64": "tree-sitter-cli-macos-x64.zip",
        "darwin-arm64": "tree-sitter-cli-macos-arm64.zip",
    },
    "codeql": {
        "win32-x64": "codeql-bundle-win64.tar.gz",
        "win32-arm64": "codeql-bundle-win64.tar.gz",
        "linux-x64": "codeql-bundle-linux64.tar.gz",
        "linux-arm64": "codeql-bundle-linux-arm64.tar.gz",
        "darwin-x64": "codeql-bundle-osx64.tar.gz",
        "darwin-arm64": "codeql-bundle-osx64.tar.gz",
    },
}


@pytest.mark.parametrize("name", sorted(_BINARY_EXPECTATIONS))
def test_lock_covers_every_supported_target(name):
    package = get_package(name)
    version = _version(name)
    pinned = Lockfile(paths.lockfile_path()).pinned_artifacts(name)
    supported = {target for target in ALL_TARGETS if package.missing_reason(target) is None}
    assert set(pinned) == supported
    for target in supported:
        assert pinned[target]["url"] == package.fetch_url(version, target)
        assert len(pinned[target]["sha256"]) == 64


@pytest.mark.parametrize("name,assets", sorted(_BINARY_EXPECTATIONS.items()))
def test_release_archive_names(name, assets):
    package = get_package(name)
    version = _version(name)
    for target, filename in assets.items():
        assert package.fetch_url(version, target).endswith("/" + filename)


@pytest.mark.parametrize("name", sorted(_BINARY_EXPECTATIONS))
def test_unsupported_userlands_are_declared(name):
    package = get_package(name)
    assert "musl" in (package.missing_reason("linux-x64-musl") or "")
    assert "Android" in (package.missing_reason("linux-arm64-bionic") or "")


def test_codeql_runs_x64_bundles_under_arm64_emulation():
    package = get_package("codeql")
    version = _version("codeql")
    assert {"win32-arm64", "darwin-arm64"} <= package.emulated_arch_targets
    assert package.fetch_url(version, "darwin-arm64") == package.fetch_url(version, "darwin-x64")


def test_semgrep_is_a_python_tool_environment():
    package = get_package("semgrep")
    assert isinstance(package, PythonToolPackage)
    assert package.has_artifacts is False
    assert Lockfile(paths.lockfile_path()).pinned_artifacts("semgrep") == {}
    assert package.requirement_for(_version("semgrep")) == f"semgrep=={_version('semgrep')}"


def test_semgrep_declares_targets_without_wheels():
    package = get_package("semgrep")
    assert package.missing_reason("darwin-x64")
    assert package.missing_reason("win32-arm64")
    assert package.missing_reason("linux-arm64-bionic")
    assert package.missing_reason("darwin-arm64") is None


def test_python_tool_apply_installs_the_pinned_distribution(monkeypatch):
    import pm.operations

    calls = []

    def ensure(name, requirements, executable, *, explicit=False):
        calls.append((name, list(requirements), executable, explicit))
        return paths.repo_root() / "fake-tool"

    monkeypatch.setattr(pm.operations, "ensure_python_tool", ensure)
    tool = get_package("semgrep").apply_tool(explicit=True)
    version = _version("semgrep")
    assert calls == [("semgrep", [f"semgrep=={version}"], "semgrep", True)]
    assert tool.name == "fake-tool"


def test_python_tool_activation_env_exposes_its_bin_dir(monkeypatch, tmp_path):
    import pm.operations

    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    tool = scripts / "semgrep.exe"
    tool.write_bytes(b"MZ")
    monkeypatch.setattr(pm.operations, "python_tool", lambda name, executable: tool)

    from pm.install import env_for

    env = env_for("semgrep", base_env={})
    assert str(scripts) in env["PATH"].split(__import__("os").pathsep)


def test_semgrep_versions_come_from_pypi(monkeypatch):
    from pm import code_intelligence_packages as module

    monkeypatch.setattr(module, "pypi_versions", lambda name: ["1.183.0", "1.182.0"])
    assert get_package("semgrep").latest_versions("linux-x64") == ["1.183.0", "1.182.0"]


def test_pypi_versions_skip_yanked_and_prereleases(monkeypatch):
    from pm import update

    payload = {"releases": {
        "1.2.0": [{"yanked": False}],
        "1.10.0": [{"yanked": False}],
        "1.3.0": [{"yanked": True}],
        "2.0.0rc1": [{"yanked": False}],
    }}
    monkeypatch.setattr(update, "_get_json", lambda url: payload)
    assert update.pypi_versions("semgrep") == ["1.10.0", "1.2.0"]


def test_ast_grep_versions_require_this_targets_asset(monkeypatch):
    from pm import update

    releases = [
        {"tag_name": "0.51.1", "draft": False, "prerelease": False,
         "assets": [{"name": "app-x86_64-apple-darwin.zip"}]},
        {"tag_name": "0.51.0", "draft": False, "prerelease": False,
         "assets": [{"name": "app-x86_64-pc-windows-msvc.zip"},
                    {"name": "app-x86_64-unknown-linux-gnu.zip"},
                    {"name": "app-x86_64-apple-darwin.zip"}]},
    ]

    def index(url):
        return releases if "page=1" in url else []

    monkeypatch.setattr(update, "_get_json", index)
    package = get_package("ast-grep")
    assert package.latest_versions("win32-x64") == ["0.51.0"]
    assert package.latest_versions("linux-x64") == ["0.51.0"]
    assert package.latest_versions("darwin-x64") == ["0.51.1", "0.51.0"]


def test_real_lock_bundle_closure_excludes_semgrep_and_codeql():
    from scripts.bundles.native import _bundle_package_names

    names = set(_bundle_package_names())
    assert {"ast-grep", "tree-sitter"} <= names
    assert not {"semgrep", "codeql"} & names


def test_activate_puts_a_python_tool_bin_on_path(monkeypatch, tmp_path):
    import os

    import pm.install as engine
    from pm import registry
    from pm.lock import Lockfile
    from pm.package import PythonToolPackage

    class FakePythonTool(PythonToolPackage):
        name = "fake-python-tool"
        optional = True
        requirement = "fake"
        executable = "fake"

    scripts = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "store"))
    fake = FakePythonTool()
    monkeypatch.setitem(registry._packages, fake.name, fake)
    monkeypatch.setattr(fake, "activation_env", lambda: {"PATH": [str(scripts)]})

    lock = Lockfile(tmp_path / "lock.json")
    lock.set_pin(fake.name, "1", {})
    lock.save()
    monkeypatch.setattr(engine, "_lockfile", lambda: lock)
    monkeypatch.setenv("PATH", str(tmp_path / "user-bin"))

    assert engine.activate(allow_incomplete=True) == []
    assert os.environ["PATH"].split(os.pathsep)[0] == str(scripts)


def test_bundle_closure_excludes_host_built_and_oversized_tools(monkeypatch, tmp_path):
    from pm.lock import Lockfile
    from scripts.bundles.native import _bundle_package_names

    lock = Lockfile(tmp_path / "lock.json")
    for name in ("python", "ast-grep", "tree-sitter", "codeql", "semgrep"):
        lock.set_pin(name, "1", {} if name == "semgrep" else
                     {"any": {"url": "x", "sha256": "0" * 64}})
    lock.save()
    monkeypatch.setattr("scripts.bundles.native._lockfile", lambda: lock)

    names = _bundle_package_names()
    assert "ast-grep" in names and "tree-sitter" in names
    assert "codeql" not in names  # bundle = False: too large for every payload
    assert "semgrep" not in names  # state package: built on the installing host


@pytest.mark.parametrize("target,filename", [
    ("win32-x64", "ast-grep.exe"),
    ("linux-x64", "ast-grep"),
])
def test_ast_grep_flat_zip_stages_the_binary(tmp_path, target, filename):
    package = get_package("ast-grep")
    archive = tmp_path / "ast-grep.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(filename, b"MZ" if filename.endswith(".exe") else b"\x7fELF")

    staged = tmp_path / "staged"
    package.unpack(archive, staged, target)

    assert package.binary(staged, target) == staged / filename


def test_tree_sitter_zip_stages_the_binary(tmp_path):
    package = get_package("tree-sitter")
    archive = tmp_path / "tree-sitter.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("tree-sitter.exe", b"MZ")

    staged = tmp_path / "staged"
    package.unpack(archive, staged, "win32-x64")

    assert package.binary(staged, "win32-x64") == staged / "tree-sitter.exe"


def test_codeql_bundle_keeps_its_codeql_dir(tmp_path):
    package = get_package("codeql")
    archive = tmp_path / "codeql.tar.gz"
    binary = tmp_path / "codeql"
    (binary / "codeql").mkdir(parents=True)
    (binary / "codeql" / "codeql").write_bytes(b"\x7fELF")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="codeql")

    staged = tmp_path / "staged"
    package.unpack(archive, staged, "linux-x64")

    assert package.binary(staged, "linux-x64") == staged / "codeql" / "codeql"
