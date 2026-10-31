"""Git TUI contracts: the working tree of a session's repository, read in that session's terminal
backend (never the gateway host's). Handler: ``gateway/session_git.py``."""

from __future__ import annotations

from pydantic import Field

from .base import Result
from .common import SessionParams
from .registry import canonical_method


class GitStatusParams(SessionParams):
    pass


class GitFileStatus(Result):
    path: str
    xy: str
    staged: bool = False


class GitStatusResult(Result):
    branch: str = ""
    ahead: int = 0
    behind: int = 0
    files: list[GitFileStatus] = Field(default_factory=list)


canonical_method("git.status", params=GitStatusParams, result=GitStatusResult,
                 doc="Working-tree status of the session's repository, read in its terminal backend.")


class GitDiffParams(SessionParams):
    path: str | None = None
    staged: bool = False
    context: int | None = None


class GitDiffResult(Result):
    diff: str = ""
    truncated: bool = False


canonical_method("git.diff", params=GitDiffParams, result=GitDiffResult,
                 doc="One file's (or the whole tree's) diff; ``staged`` reads the index.")


class GitLogParams(SessionParams):
    limit: int | None = None


class GitCommitInfo(Result):
    sha: str
    subject: str = ""
    author: str = ""
    date: str = ""


class GitLogResult(Result):
    commits: list[GitCommitInfo] = Field(default_factory=list)


canonical_method("git.log", params=GitLogParams, result=GitLogResult,
                 doc="The session repository's recent commits.")


class GitBranchesParams(SessionParams):
    pass


class GitBranchInfo(Result):
    name: str
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0


class GitBranchesResult(Result):
    current: str = ""
    branches: list[GitBranchInfo] = Field(default_factory=list)


canonical_method("git.branches", params=GitBranchesParams, result=GitBranchesResult,
                 doc="Local branches with upstream tracking counts.")


class GitPathsParams(SessionParams):
    paths: list[str]
    request_id: str | None = None


class GitStagedResult(Result):
    staged: list[str] = Field(default_factory=list)


canonical_method("git.stage", params=GitPathsParams, result=GitStagedResult,
                 doc="Stage the named paths; answers the index's staged paths.")
canonical_method("git.unstage", params=GitPathsParams, result=GitStagedResult,
                 doc="Unstage the named paths; answers the index's staged paths.")


class GitCommitParams(SessionParams):
    message: str
    sign: bool = False
    request_id: str | None = None


class GitCommitResult(Result):
    sha: str
    summary: str = ""


canonical_method("git.commit", params=GitCommitParams, result=GitCommitResult,
                 doc="Commit the index with ``message``; never amends.")
