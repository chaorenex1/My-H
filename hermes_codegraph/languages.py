"""Language descriptors: the single source of truth for language support.

Both registry files use the same shape (``schema_version: 1`` plus a
``languages`` list). The packaged registry loads first; a profile registry at
``<HERMES_HOME>/code-intelligence/tree-sitter/languages.yaml`` may only add
languages. A profile entry whose ``language`` is already loaded is skipped, and
an entry that would reuse an alias/extension/filename is skipped and reported.
A malformed file is rejected as a whole; there is deliberately no
extension-to-tree-sitter fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .exceptions import LanguageDescriptorError, LanguageUnsupportedError

SCHEMA_VERSION = 1
REQUIRED_QUERIES = ("symbols", "imports", "calls", "types", "tests")
CAPABILITY_KEYS = (
    "symbols",
    "references",
    "calls",
    "types",
    "inheritance",
    "tests",
    "change",
)
TOP_LEVEL_KEYS = frozenset({"schema_version", "languages"})
REQUIRED_FIELDS = frozenset(
    {
        "language",
        "aliases",
        "extensions",
        "filenames",
        "grammar_package",
        "grammar_version",
        "grammar_install",
        "tree_sitter_queries",
        "capabilities",
        "parser_version",
    }
)
OPTIONAL_FIELDS = frozenset(
    {
        "parser",
        "kind_map",
        "container_rules",
        "visibility_rules",
        "signature_rules",
        "module_naming",
        "import_resolution",
        "test_detection",
        "semantic_provider",
    }
)
RULE_FIELDS = (
    "kind_map",
    "container_rules",
    "visibility_rules",
    "signature_rules",
    "module_naming",
    "import_resolution",
    "test_detection",
)
_SEQUENCE_FIELDS = ("aliases", "extensions", "filenames")
_LANGUAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_INSTALL_POLICY = re.compile(r"(?:builtin|on_demand:[a-z0-9][a-z0-9._-]*)\Z")

DESCRIPTOR_DIR = Path(__file__).resolve().parent / "adapters" / "languages"
BUILTIN_REGISTRY = DESCRIPTOR_DIR / "languages.yaml"
PROFILE_REGISTRY_PARTS = ("code-intelligence", "tree-sitter", "languages.yaml")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_artifact_path(value: Any, *, base: Path | None, what: str) -> str:
    """Reject absolute/escaping query paths and (when a root is known) missing files."""
    if not _nonempty_string(value):
        raise LanguageDescriptorError(f"{what} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise LanguageDescriptorError(
            f"{what} must stay inside the registry root: {value!r}"
        )
    if relative.suffix.lower() != ".scm":
        raise LanguageDescriptorError(f"{what} must be a .scm file: {value!r}")
    if base is not None:
        root = base.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise LanguageDescriptorError(
                f"{what} escapes the registry root: {value!r}"
            )
        if not target.is_file():
            raise LanguageDescriptorError(f"{what} does not exist: {value!r}")
        if target.stat().st_size == 0:
            raise LanguageDescriptorError(f"{what} is empty: {value!r}")
    return value


@dataclass(frozen=True)
class LanguageDescriptor:
    language: str
    aliases: tuple[str, ...]
    extensions: tuple[str, ...]
    filenames: tuple[str, ...]
    grammar_package: str
    grammar_version: str
    grammar_install: str
    tree_sitter_queries: dict[str, str]
    capabilities: dict[str, bool]
    parser_version: str
    semantic_provider: str | None = None
    parser: str | None = None
    kind_map: dict[str, Any] = field(default_factory=dict)
    container_rules: dict[str, Any] = field(default_factory=dict)
    visibility_rules: dict[str, Any] = field(default_factory=dict)
    signature_rules: dict[str, Any] = field(default_factory=dict)
    module_naming: dict[str, Any] = field(default_factory=dict)
    import_resolution: dict[str, Any] = field(default_factory=dict)
    test_detection: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._validate_identity()
        self._validate_sources()
        self._validate_queries()
        self._normalize_capabilities()
        for name in RULE_FIELDS:
            if not isinstance(getattr(self, name), dict):
                raise LanguageDescriptorError(
                    f"{self.language}: {name} must be a mapping"
                )

    def _validate_identity(self) -> None:
        if not _nonempty_string(self.language) or not _LANGUAGE_ID.match(self.language):
            raise LanguageDescriptorError(
                "language id must match [a-z0-9][a-z0-9-]*"
            )
        for name in _SEQUENCE_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise LanguageDescriptorError(f"{self.language}: {name} must be a list")
            if any(not _nonempty_string(item) for item in value):
                raise LanguageDescriptorError(
                    f"{self.language}: {name} entries must be non-empty strings"
                )
            object.__setattr__(self, name, tuple(value))
        for extension in self.extensions:
            if not extension.startswith("."):
                raise LanguageDescriptorError(
                    f"{self.language}: extensions must start with '.': {extension!r}"
                )

    def _validate_sources(self) -> None:
        for name in ("grammar_package", "grammar_version", "parser_version"):
            if not _nonempty_string(getattr(self, name)):
                raise LanguageDescriptorError(
                    f"{self.language}: {name} must be a non-empty string"
                )
        if not isinstance(self.grammar_install, str) or not _INSTALL_POLICY.match(
            self.grammar_install
        ):
            raise LanguageDescriptorError(
                f"{self.language}: grammar_install must be 'builtin' or "
                "'on_demand:<extra>'"
            )
        if self.semantic_provider is not None and not _nonempty_string(
            self.semantic_provider
        ):
            raise LanguageDescriptorError(
                f"{self.language}: semantic_provider must be a string or null"
            )
        if self.parser is not None and not _nonempty_string(self.parser):
            raise LanguageDescriptorError(
                f"{self.language}: parser must be a non-empty string or null"
            )

    def _validate_queries(self) -> None:
        if not isinstance(self.tree_sitter_queries, dict):
            raise LanguageDescriptorError(
                f"{self.language}: tree_sitter_queries must be a mapping"
            )
        purposes = set(self.tree_sitter_queries)
        if purposes != set(REQUIRED_QUERIES):
            raise LanguageDescriptorError(
                f"{self.language}: tree_sitter_queries keys must be "
                f"{list(REQUIRED_QUERIES)}, got {sorted(purposes)}"
            )
        for purpose, path in self.tree_sitter_queries.items():
            _validate_artifact_path(
                path, base=None, what=f"{self.language}.tree_sitter_queries.{purpose}"
            )

    def _normalize_capabilities(self) -> None:
        if not isinstance(self.capabilities, dict):
            raise LanguageDescriptorError(
                f"{self.language}: capabilities must be a mapping"
            )
        unknown = sorted(set(self.capabilities) - set(CAPABILITY_KEYS))
        if unknown:
            raise LanguageDescriptorError(
                f"{self.language}: unknown capabilities: {', '.join(unknown)}"
            )
        for name, value in self.capabilities.items():
            if not isinstance(value, bool):
                raise LanguageDescriptorError(
                    f"{self.language}: capabilities.{name} must be a bool"
                )
        object.__setattr__(
            self,
            "capabilities",
            {name: bool(self.capabilities.get(name, False)) for name in CAPABILITY_KEYS},
        )

    @classmethod
    def from_mapping(
        cls,
        data: Any,
        *,
        source: str = "<memory>",
        registry_root: Path | None = None,
        require_all_capabilities: bool = False,
    ) -> LanguageDescriptor:
        """Build a descriptor from one ``languages[]`` mapping.

        Unknown/missing fields and type errors fail loudly. ``registry_root``,
        when given, also requires every referenced ``.scm`` query to exist and
        be non-empty.
        """
        if not isinstance(data, dict):
            raise LanguageDescriptorError(f"{source}: descriptor must be a mapping")
        unknown = sorted(str(key) for key in set(data) - REQUIRED_FIELDS - OPTIONAL_FIELDS)
        if unknown:
            raise LanguageDescriptorError(
                f"{source}: unknown descriptor field(s): {', '.join(unknown)}"
            )
        missing = sorted(REQUIRED_FIELDS - set(data))
        if missing:
            raise LanguageDescriptorError(
                f"{source}: missing descriptor field(s): {', '.join(missing)}"
            )
        payload = dict(data)
        for name in _SEQUENCE_FIELDS:
            value = payload[name]
            if not isinstance(value, list):
                raise LanguageDescriptorError(f"{source}: {name} must be a list")
            if any(not _nonempty_string(item) for item in value):
                raise LanguageDescriptorError(
                    f"{source}: {name} entries must be non-empty strings"
                )
            payload[name] = tuple(value)
        capabilities = payload["capabilities"]
        if not isinstance(capabilities, dict):
            raise LanguageDescriptorError(f"{source}: capabilities must be a mapping")
        if require_all_capabilities:
            absent = sorted(set(CAPABILITY_KEYS) - set(capabilities))
            if absent:
                raise LanguageDescriptorError(
                    f"{source}: built-in descriptor must declare capabilities: "
                    f"{', '.join(absent)}"
                )
        for name in RULE_FIELDS:
            if name in payload and not isinstance(payload[name], dict):
                raise LanguageDescriptorError(f"{source}: {name} must be a mapping")
        queries = payload["tree_sitter_queries"]
        if not isinstance(queries, dict) or not queries:
            raise LanguageDescriptorError(
                f"{source}: tree_sitter_queries must be a non-empty mapping"
            )
        unknown_queries = sorted(set(queries) - set(REQUIRED_QUERIES))
        if unknown_queries:
            raise LanguageDescriptorError(
                f"{source}: unknown tree_sitter_queries purposes: "
                f"{', '.join(unknown_queries)}"
            )
        for purpose, path in queries.items():
            _validate_artifact_path(
                path,
                base=registry_root,
                what=f"{source}.tree_sitter_queries.{purpose}",
            )
        try:
            return cls(**payload)
        except LanguageDescriptorError as exc:
            raise LanguageDescriptorError(
                f"{source}: {exc.message}",
                details={**exc.details, "source": source},
            ) from exc

    @property
    def extra(self) -> str | None:
        if self.grammar_install.startswith("on_demand:"):
            return self.grammar_install.split(":", 1)[1] or None
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "aliases": list(self.aliases),
            "extensions": list(self.extensions),
            "filenames": list(self.filenames),
            "grammar_package": self.grammar_package,
            "grammar_version": self.grammar_version,
            "grammar_install": self.grammar_install,
            "semantic_provider": self.semantic_provider,
            "capabilities": dict(self.capabilities),
            "parser_version": self.parser_version,
            "extra": self.extra,
        }


@dataclass(frozen=True)
class RegistryFile:
    path: Path
    descriptors: tuple[LanguageDescriptor, ...]


@dataclass(frozen=True)
class RegistryReport:
    """What the packaged + profile merge did, for ``status``/``doctor``."""

    builtin_path: str
    profile_path: str | None
    builtin_languages: int
    profile_languages: int = 0
    profile_skipped: int = 0
    profile_conflicts: int = 0
    profile_error: str | None = None
    conflicts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "builtin_path": self.builtin_path,
            "profile_path": self.profile_path,
            "builtin_languages": self.builtin_languages,
            "profile_languages": self.profile_languages,
            "profile_skipped": self.profile_skipped,
            "profile_conflicts": self.profile_conflicts,
            "profile_error": self.profile_error,
            "conflicts": [dict(item) for item in self.conflicts],
        }


class LanguageRegistry:
    """Descriptors by id and alias, plus extension/filename lookup."""

    def __init__(
        self,
        descriptors: tuple[LanguageDescriptor, ...],
        *,
        report: RegistryReport | None = None,
    ):
        self._by_language: dict[str, LanguageDescriptor] = {}
        self._by_alias: dict[str, str] = {}
        self._extensions: dict[str, str] = {}
        self._filenames: dict[str, str] = {}
        self.report = report
        for descriptor in descriptors:
            self._register(descriptor)

    def _register(self, descriptor: LanguageDescriptor) -> None:
        if descriptor.language in self._by_language:
            raise LanguageDescriptorError(
                f"duplicate language descriptor {descriptor.language!r}"
            )
        if descriptor.language in self._by_alias:
            raise LanguageDescriptorError(
                f"language id {descriptor.language!r} collides with an alias"
            )
        for alias in descriptor.aliases:
            if alias in self._by_language:
                raise LanguageDescriptorError(
                    f"alias {alias!r} collides with language {alias!r}"
                )
            if alias in self._by_alias:
                raise LanguageDescriptorError(f"duplicate language alias {alias!r}")
            self._by_alias[alias] = descriptor.language
        for extension in descriptor.extensions:
            key = extension.lower()
            if key in self._extensions:
                raise LanguageDescriptorError(
                    f"duplicate language extension {extension!r}"
                )
            self._extensions[key] = descriptor.language
        for filename in descriptor.filenames:
            key = filename.lower()
            if key in self._filenames:
                raise LanguageDescriptorError(
                    f"duplicate language filename {filename!r}"
                )
            self._filenames[key] = descriptor.language
        self._by_language[descriptor.language] = descriptor

    def descriptors(self) -> tuple[LanguageDescriptor, ...]:
        return tuple(self._by_language[name] for name in sorted(self._by_language))

    def get(self, language: str) -> LanguageDescriptor:
        key = self._by_alias.get(language, language)
        try:
            return self._by_language[key]
        except KeyError as exc:
            raise LanguageUnsupportedError(
                f"language {language!r} has no descriptor; supported: "
                f"{sorted(self._by_language)}",
                details={"language": language, "supported": sorted(self._by_language)},
            ) from exc

    def maybe_get(self, language: str) -> LanguageDescriptor | None:
        key = self._by_alias.get(language, language)
        return self._by_language.get(key)

    def by_path(self, path: str) -> LanguageDescriptor | None:
        lowered = path.replace("\\", "/").lower()
        name = lowered.rsplit("/", 1)[-1]
        for descriptor in self._by_language.values():
            if any(lowered.endswith(ext.lower()) for ext in descriptor.extensions):
                return descriptor
            if any(name == filename.lower() for filename in descriptor.filenames):
                return descriptor
        return None

    def supported_languages(self) -> list[str]:
        return sorted(self._by_language)


def _read_registry(path: Path) -> Any:
    import hermes_yaml

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LanguageDescriptorError(
            f"{path}: cannot read registry: {exc}", details={"source": str(path)}
        ) from exc
    try:
        return hermes_yaml.safe_load(text)
    except Exception as exc:  # backend-specific YAML errors
        raise LanguageDescriptorError(
            f"{path}: invalid YAML: {exc}", details={"source": str(path)}
        ) from exc


def load_registry_file(
    path: str | Path, *, require_all_capabilities: bool = False
) -> RegistryFile:
    """Load and validate one registry file; any bad entry rejects the whole file."""
    source = Path(path)
    document = _read_registry(source)
    if not isinstance(document, dict):
        raise LanguageDescriptorError(
            f"{source}: top level must be a mapping", details={"source": str(source)}
        )
    extra = sorted(str(key) for key in set(document) - TOP_LEVEL_KEYS)
    missing = sorted(TOP_LEVEL_KEYS - set(document))
    if extra:
        raise LanguageDescriptorError(
            f"{source}: unknown top-level keys: {', '.join(extra)}",
            details={"source": str(source)},
        )
    if missing:
        raise LanguageDescriptorError(
            f"{source}: missing top-level keys: {', '.join(missing)}",
            details={"source": str(source)},
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise LanguageDescriptorError(
            f"{source}: schema_version must be {SCHEMA_VERSION}",
            details={"source": str(source)},
        )
    entries = document.get("languages")
    if not isinstance(entries, list) or not entries:
        raise LanguageDescriptorError(
            f"{source}: languages must be a non-empty list",
            details={"source": str(source)},
        )

    descriptors: list[LanguageDescriptor] = []
    languages: set[str] = set()
    aliases: dict[str, str] = {}
    extensions: dict[str, str] = {}
    filenames: dict[str, str] = {}
    for index, entry in enumerate(entries):
        label = f"{source}:languages[{index}]"
        descriptor = LanguageDescriptor.from_mapping(
            entry,
            source=label,
            registry_root=source.parent,
            require_all_capabilities=require_all_capabilities,
        )
        if descriptor.language in languages:
            raise LanguageDescriptorError(
                f"{label}: duplicate language {descriptor.language!r}",
                details={"source": str(source)},
            )
        for alias in descriptor.aliases:
            if alias in aliases:
                raise LanguageDescriptorError(
                    f"{label}: duplicate alias {alias!r}", details={"source": str(source)}
                )
            if alias in languages:
                raise LanguageDescriptorError(
                    f"{label}: alias {alias!r} collides with a language id",
                    details={"source": str(source)},
                )
            aliases[alias] = descriptor.language
        for extension in descriptor.extensions:
            key = extension.lower()
            if key in extensions:
                raise LanguageDescriptorError(
                    f"{label}: duplicate extension {extension!r}",
                    details={"source": str(source)},
                )
            extensions[key] = descriptor.language
        for filename in descriptor.filenames:
            key = filename.lower()
            if key in filenames:
                raise LanguageDescriptorError(
                    f"{label}: duplicate filename {filename!r}",
                    details={"source": str(source)},
                )
            filenames[key] = descriptor.language
        languages.add(descriptor.language)
        descriptors.append(descriptor)
    shared = sorted(set(languages) & set(aliases))
    if shared:
        raise LanguageDescriptorError(
            f"{source}: language id(s) also used as aliases: {', '.join(shared)}",
            details={"source": str(source)},
        )
    return RegistryFile(path=source, descriptors=tuple(descriptors))


def profile_registry_path() -> Path:
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home())
    for part in PROFILE_REGISTRY_PARTS:
        path = path / part
    return path


def install_target_gap(target: str | None) -> str | None:
    """Known wheel gaps for the [codegraph] extra, from ``pm.store.current_target()``.

    musl aarch64 cannot be expressed with a PEP 508 marker (no libc variable),
    so it must fail before uv's resolver error. The same applies to Android's
    bionic targets, which have no core tree-sitter wheel at all.
    """
    if not target:
        return None
    normalized = target.lower()
    if normalized.endswith("-musl") and (
        "arm64" in normalized or "aarch64" in normalized
    ):
        return (
            f"{target} has no tree-sitter core wheel; the [codegraph] extra "
            "cannot be installed on this target"
        )
    if "-bionic" in normalized:
        return (
            f"{target} has no tree-sitter core wheel; the [codegraph] extra "
            "cannot be installed on this target"
        )
    return None


def _merge_profile(
    builtin: list[LanguageDescriptor],
    profile: list[LanguageDescriptor],
) -> tuple[list[LanguageDescriptor], int, list[dict[str, Any]]]:
    merged = list(builtin)
    languages = {item.language for item in builtin}
    aliases = {alias: item.language for item in builtin for alias in item.aliases}
    extensions = {ext.lower() for item in builtin for ext in item.extensions}
    filenames = {name.lower() for item in builtin for name in item.filenames}
    skipped = 0
    conflicts: list[dict[str, Any]] = []
    for descriptor in profile:
        if descriptor.language in languages:
            skipped += 1
            continue
        reasons: list[str] = []
        if descriptor.language in aliases:
            reasons.append(f"language:{descriptor.language}")
        for alias in descriptor.aliases:
            if alias in languages or alias in aliases:
                reasons.append(f"alias:{alias}")
        for extension in descriptor.extensions:
            if extension.lower() in extensions:
                reasons.append(f"extension:{extension}")
        for filename in descriptor.filenames:
            if filename.lower() in filenames:
                reasons.append(f"filename:{filename}")
        if reasons:
            conflicts.append(
                {
                    "language": descriptor.language,
                    "reasons": reasons,
                    "source": "profile",
                }
            )
            continue
        merged.append(descriptor)
        languages.add(descriptor.language)
        aliases.update({alias: descriptor.language for alias in descriptor.aliases})
        extensions.update(ext.lower() for ext in descriptor.extensions)
        filenames.update(name.lower() for name in descriptor.filenames)
    return merged, skipped, conflicts


def default_registry() -> LanguageRegistry:
    """Load the packaged registry, then merge the optional profile registry."""
    builtin_file = load_registry_file(
        BUILTIN_REGISTRY, require_all_capabilities=True
    )
    builtin = list(builtin_file.descriptors)
    profile_path = profile_registry_path()
    profile_languages = 0
    skipped = 0
    conflicts: list[dict[str, Any]] = []
    profile_error: str | None = None
    descriptors = builtin
    if profile_path.exists():
        try:
            profile_file = load_registry_file(profile_path)
        except LanguageDescriptorError as exc:
            profile_error = exc.message
        else:
            profile = list(profile_file.descriptors)
            profile_languages = len(profile)
            descriptors, skipped, conflicts = _merge_profile(builtin, profile)
    report = RegistryReport(
        builtin_path=str(builtin_file.path),
        profile_path=str(profile_path) if profile_path.exists() else None,
        builtin_languages=len(builtin),
        profile_languages=profile_languages,
        profile_skipped=skipped,
        profile_conflicts=len(conflicts),
        profile_error=profile_error,
        conflicts=tuple(conflicts),
    )
    return LanguageRegistry(tuple(descriptors), report=report)
