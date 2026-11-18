"""Optional code-intelligence executables. Acquisition and publication belong to PM.

ast-grep and tree-sitter ship prebuilt archives; CodeQL ships a CLI bundle whose
tag carries the CLI version. Semgrep publishes no binary at all: it is a Python
distribution, so PM builds it an isolated environment instead of pinning bytes.
"""

from __future__ import annotations

from typing import Optional

from pm.package import InstallError, PythonToolPackage
from pm.packages import BinaryPackage, _RUST_TRIPLE, _github_release_digests
from pm.registry import register
from pm.store import MUSL_TARGETS
from pm.update import github_release_tags, pypi_versions


@register
class AstGrep(BinaryPackage):
    """ast-grep's `app-<triple>.zip`: one flat archive with `ast-grep` and `sg`.

    Optional on purpose: the ast-grep skill provisions itself, and a search tool
    cannot be what every install pays for. Upstream builds glibc only, so musl
    userlands and Termux stay gaps; the `sg` alias is not
    the binary PM verifies because util-linux owns that name on Linux.
    """

    name = "ast-grep"
    optional = True
    binary_rel = {"win32": "ast-grep.exe", "posix": "ast-grep"}
    gaps = {
        **{target: "upstream publishes no musl build; use a distro/cargo package"
           for target in MUSL_TARGETS},
        "linux-arm64-bionic": "upstream publishes no Android build",
    }

    def fetch_url(self, version: str, target: str) -> str:
        return (f"https://github.com/ast-grep/ast-grep/releases/download/{version}/"
                f"app-{_RUST_TRIPLE[target]}.zip")

    def latest_versions(self, target: str, locked: Optional[str] = None) -> list[str]:
        # A release that shipped only some platforms must not become the
        # update candidate for the others (0.51.1 carried darwin only).
        return github_release_tags("ast-grep/ast-grep",
                                   require_asset=f"app-{_RUST_TRIPLE[target]}.zip")

    def known_sha256(self, version: str, url: str) -> Optional[str]:
        return _github_release_digests("ast-grep/ast-grep", version).get(url.rsplit("/", 1)[-1])


_TS_PLAT = {
    "win32-x64": "windows-x64",
    "win32-arm64": "windows-arm64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "darwin-x64": "macos-x64",
    "darwin-arm64": "macos-arm64",
}


@register
class TreeSitter(BinaryPackage):
    """The tree-sitter CLI: `tree-sitter-cli-<platform>-<arch>.zip`, one binary.

    Optional: parsers, editors and agent tooling load the library, not this CLI.
    The Linux artifacts link glibc, so musl userlands and Termux are gaps.
    """

    name = "tree-sitter"
    optional = True
    binary_rel = {"win32": "tree-sitter.exe", "posix": "tree-sitter"}
    gaps = {
        **{target: "upstream publishes a glibc Linux build, not musl"
           for target in MUSL_TARGETS},
        "linux-arm64-bionic": "upstream publishes no Android build",
    }

    def fetch_url(self, version: str, target: str) -> str:
        platform = _TS_PLAT.get(target)
        if platform is None:
            raise InstallError(self.name, f"no tree-sitter CLI build for target {target}")
        return (f"https://github.com/tree-sitter/tree-sitter/releases/download/v{version}/"
                f"tree-sitter-cli-{platform}.zip")

    def latest_versions(self, target: str, locked: Optional[str] = None) -> list[str]:
        return github_release_tags("tree-sitter/tree-sitter", strip_prefix="v")

    def known_sha256(self, version: str, url: str) -> Optional[str]:
        return _github_release_digests("tree-sitter/tree-sitter", f"v{version}").get(
            url.rsplit("/", 1)[-1]
        )


_CODEQL_ARCHIVE = {
    "win32-x64": "codeql-bundle-win64.tar.gz",
    "win32-arm64": "codeql-bundle-win64.tar.gz",
    "linux-x64": "codeql-bundle-linux64.tar.gz",
    "linux-arm64": "codeql-bundle-linux-arm64.tar.gz",
    "darwin-x64": "codeql-bundle-osx64.tar.gz",
    "darwin-arm64": "codeql-bundle-osx64.tar.gz",
}


@register
class CodeQL(BinaryPackage):
    """GitHub's CodeQL CLI bundle (`github/codeql-action` release assets).

    The bundle carries the CLI and its standard query packs; upstream tags it
    `codeql-bundle-v<cli version>`, and only arm64 Linux has its own archive —
    Windows ARM64 and Apple Silicon run the x64 bundle under the host's
    emulation, hence `emulated_arch_targets`. Bundles are glibc builds, so
    musl userlands and Termux are gaps. Optional: the download is ~0.7-1.3 GB
    and most installs never analyze code locally.
    """

    name = "codeql"
    optional = True
    binary_rel = {"win32": "codeql/codeql.exe", "posix": "codeql/codeql"}
    gaps = {
        **{target: "CodeQL bundles are glibc builds, not musl"
           for target in MUSL_TARGETS},
        "linux-arm64-bionic": "upstream publishes no Android build",
    }
    flatten = False
    probe_args = ["version"]
    emulated_arch_targets = frozenset({"win32-arm64", "darwin-arm64"})
    # A 0.7-1.3 GB bundle must not ride in every native payload; install it
    # on demand with `hermes pm install codeql`.
    bundle = False

    def fetch_url(self, version: str, target: str) -> str:
        archive = _CODEQL_ARCHIVE.get(target)
        if archive is None:
            raise InstallError(self.name, f"no CodeQL bundle for target {target}")
        return (f"https://github.com/github/codeql-action/releases/download/"
                f"codeql-bundle-v{version}/{archive}")

    def latest_versions(self, target: str, locked: Optional[str] = None) -> list[str]:
        return github_release_tags("github/codeql-action", strip_prefix="codeql-bundle-v")

    def known_sha256(self, version: str, url: str) -> Optional[str]:
        return _github_release_digests("github/codeql-action", f"codeql-bundle-v{version}").get(
            url.rsplit("/", 1)[-1]
        )


@register
class Semgrep(PythonToolPackage):
    """Semgrep's CLI as an isolated Python tool environment.

    `semgrep/semgrep` publishes no binary assets (PyPI wheels only), so the lock
    pins the distribution version and PM builds `semgrep==<version>` through
    `ensure_python_tool`. Upstream's macOS wheel is arm64-only, Windows has no
    arm64 wheel, and Termux has neither, so those targets are gaps.
    """

    name = "semgrep"
    optional = True
    requirement = "semgrep"
    executable = "semgrep"
    gaps = {
        "darwin-x64": "upstream publishes no macOS x86_64 wheel",
        "win32-arm64": "upstream publishes no win_arm64 wheel",
        "linux-arm64-bionic": "upstream publishes no Android/bionic wheel",
    }

    def latest_versions(self, target: str, locked: Optional[str] = None) -> list[str]:
        return pypi_versions(self.requirement)
