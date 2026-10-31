#!/usr/bin/env python3
"""Refresh ``omp-ui/src`` from the pinned oh-my-pi checkout (layout/patches: omp-ui/src/VENDORED.md).

Run from anywhere: ``python omp-ui/scripts/sync-upstream.py`` (``OMP_SRC`` overrides the
checkout root). What it does, in order:

1. copy every ``packages/coding-agent/src/**`` file to ``omp-ui/src/**`` byte-for-byte
   (existing files are overwritten, nothing outside ``src/`` is touched);
2. vendor the non-installed ``@oh-my-pi/*`` packages by import closure into ``src/vendor/<pkg>/``;
3. rewrite every import specifier (``from``, ``import()``, ``require()``, side-effect
   ``import``) to the vendored file's path; ``@oh-my-pi/pi-tui`` and
   ``@oh-my-pi/pi-natives`` stay installed;
4. copy the package-level assets the shell imports (``LICENSE``, changelog, notices,
   manifest) into ``src/`` and ``src/assets/``;
5. apply the local entry patch (``main.ts`` boots ``InteractiveMode`` directly instead
   of dispatching through ``src/cli.ts``);
6. point the few repo-root imports at ``src/assets/`` so R2 holds.

Verify afterwards with ``bun run check:imports`` and ``bun run check`` inside ``omp-ui``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys

UP = os.path.join(os.environ.get("OMP_SRC", "C:/Users/zarag/projects/oh-my-pi"), "packages")
UI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(UI, "src")

# Guard the resolved root: a replayed/exec'd copy must never write into the repo root.
_manifest = os.path.join(UI, "package.json")
if not os.path.isfile(_manifest) or json.load(open(_manifest, encoding="utf-8")).get("name") != "hermes-omp-ui":
    raise SystemExit(f"sync-upstream: {UI} is not the omp-ui workspace (expected package name hermes-omp-ui)")
PINNED_COMMIT = "6a86df8539fccf3ad7818081266e554022fc21bc"

PKGS = {
    "@oh-my-pi/pi-utils": "utils",
    "@oh-my-pi/pi-ai": "ai",
    "@oh-my-pi/pi-agent-core": "agent",
    "@oh-my-pi/pi-catalog": "catalog",
    "@oh-my-pi/pi-wire": "wire",
    "@oh-my-pi/omptype": "omptype",
    "@oh-my-pi/omp-stats": "stats",
    "@oh-my-pi/snapcompact": "snapcompact",
    "@oh-my-pi/pi-mnemopi": "mnemopi",
}
EXPORTS = {
    name: json.load(open(os.path.join(UP, directory, "package.json"), encoding="utf-8"))
    for name, directory in PKGS.items()
}

# Packages imported by name from node_modules instead of being vendored. pi-tui is the
# renderer; pi-natives ships the platform `.node` addon through its optionalDependencies,
# which the vendored loader must not try to reproduce.
INSTALLED = ("@oh-my-pi/pi-tui", "@oh-my-pi/pi-natives")


def stays_installed(spec: str) -> bool:
    return any(spec == name or spec.startswith(name + "/") for name in INSTALLED)

# pi-tui's own peer imports: seeding them keeps one module identity for shell and renderer.
PI_TUI_SEEDS = """@oh-my-pi/omptype
@oh-my-pi/pi-agent-core
@oh-my-pi/pi-agent-core/compaction
@oh-my-pi/pi-agent-core/compaction/messages
@oh-my-pi/pi-agent-core/thinking
@oh-my-pi/pi-ai
@oh-my-pi/pi-ai/dialect
@oh-my-pi/pi-ai/error
@oh-my-pi/pi-ai/oauth
@oh-my-pi/pi-ai/oauth/types
@oh-my-pi/pi-ai/usage
@oh-my-pi/pi-ai/usage/google-antigravity
@oh-my-pi/pi-ai/utils/narration
@oh-my-pi/pi-ai/utils/schema
@oh-my-pi/pi-catalog/build
@oh-my-pi/pi-catalog/compat/providers
@oh-my-pi/pi-catalog/effort
@oh-my-pi/pi-catalog/identity
@oh-my-pi/pi-catalog/model-thinking
@oh-my-pi/pi-catalog/models
@oh-my-pi/pi-catalog/types
@oh-my-pi/pi-utils
@oh-my-pi/pi-utils/ar
@oh-my-pi/pi-utils/chalk
@oh-my-pi/pi-utils/color
@oh-my-pi/pi-utils/dirs
@oh-my-pi/pi-utils/env
@oh-my-pi/pi-utils/fs-error
@oh-my-pi/pi-utils/logger
@oh-my-pi/pi-utils/loop-phase
@oh-my-pi/pi-utils/lru
@oh-my-pi/pi-utils/marked
@oh-my-pi/pi-utils/marked-list
@oh-my-pi/pi-utils/math-delimiters
@oh-my-pi/pi-utils/mermaid-ascii
@oh-my-pi/pi-utils/postmortem
@oh-my-pi/pi-utils/sqlite
@oh-my-pi/pi-utils/stderr-guard
@oh-my-pi/pi-utils/type-guards
@oh-my-pi/pi-utils/vterm
@oh-my-pi/pi-utils/yaml-config
@oh-my-pi/pi-wire
@oh-my-pi/snapcompact""".split("\n")

SPEC_RE = re.compile(
    r'(?P<pre>from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)(?P<q>["\'])(?P<spec>[^"\']+)(?P=q)'
)

copied: set[str] = set()
queue: list[tuple[str, str, str]] = []
seen: set[tuple[str, str]] = set()


def pick_target(entry):
    if isinstance(entry, str):
        return entry, None
    target = entry.get("import") or entry.get("default") or entry.get("types")
    return target, entry.get("types")


def resolve_vendor(spec):
    for name, directory in PKGS.items():
        if spec == name or spec.startswith(name + "/"):
            sub = spec[len(name):].lstrip("/")
            exports = EXPORTS[name].get("exports", {})
            key = "." if not sub else "./" + sub
            if key in exports:
                return directory, pick_target(exports[key])
            for pattern in sorted(exports, key=len, reverse=True):
                if "*" not in pattern:
                    continue
                pre, post = pattern.split("*", 1)
                if key.startswith(pre) and key.endswith(post):
                    star = key[len(pre):len(key) - len(post) if post else None]
                    entry = exports[pattern]
                    if isinstance(entry, str):
                        return directory, (entry.replace("*", star), None)
                    target = entry.get("import") or entry.get("default") or entry.get("types")
                    types = entry.get("types")
                    return directory, (
                        target.replace("*", star),
                        types.replace("*", star) if types else None,
                    )
            raise SystemExit(f"no exports match for {spec}")
    raise SystemExit(f"not a vendored package: {spec}")


def rel_candidates(target):
    out = []
    if target.endswith(".js"):
        out.extend([target[:-3] + ".ts", target[:-3] + ".tsx"])
    out.extend(
        [
            target,
            target + ".ts",
            target + ".tsx",
            target + ".d.ts",
            os.path.join(target, "index.ts"),
            os.path.join(target, "index.tsx"),
        ]
    )
    return out


def package_of(source):
    for directory in PKGS.values():
        rel = os.path.relpath(source, os.path.join(UP, directory)).replace("\\", "/")
        if not rel.startswith(".."):
            return directory, rel
    return None


def vendor_rel(directory, rel):
    return "vendor/" + directory + "/" + rel


def locate(directory, target):
    for candidate in rel_candidates(target):
        path = os.path.normpath(os.path.join(UP, directory, candidate))
        if os.path.isfile(path):
            return os.path.relpath(path, os.path.join(UP, directory)).replace("\\", "/")
    return None


def enqueue(spec):
    directory, (target, types) = resolve_vendor(spec)
    rel = locate(directory, target)
    if rel is None:
        raise SystemExit(f"cannot locate {spec}")
    queue.append((directory, directory, rel))
    if types:
        companion = locate(directory, types)
        if companion:
            queue.append((directory, directory, companion))


def copy_vendor(directory, rel):
    source = os.path.normpath(os.path.join(UP, directory, rel))
    dest = os.path.join(SRC, vendor_rel(directory, rel))
    if source in copied:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source, dest)
    copied.add(source)
    if rel.endswith(".json"):
        companion = source[:-5] + ".d.json.ts"
        if os.path.isfile(companion):
            crel = os.path.relpath(companion, os.path.join(UP, directory)).replace("\\", "/")
            cdest = os.path.join(SRC, vendor_rel(directory, crel))
            if not os.path.exists(cdest):
                shutil.copyfile(companion, cdest)
    if rel.endswith((".ts", ".tsx")):
        scan_vendor(dest, source, directory)


def scan_vendor(path, source_path, directory):
    text = open(path, encoding="utf-8").read()
    upstream_dir = os.path.dirname(source_path)
    dest_dir = os.path.dirname(path)
    changed = [False]

    def rewrite(spec):
        if spec.startswith("@oh-my-pi/"):
            if stays_installed(spec):
                return None
            if not any(spec == name or spec.startswith(name + "/") for name in PKGS):
                return None
            target_directory, (target, _) = resolve_vendor(spec)
            rel = locate(target_directory, target)
            if rel is None:
                raise SystemExit(f"cannot locate {spec}")
            copy_vendor(target_directory, rel)
            relp = os.path.relpath(os.path.join(SRC, vendor_rel(target_directory, rel)), dest_dir).replace("\\", "/")
            return relp if relp.startswith(".") else "./" + relp
        if spec.startswith("."):
            for candidate in rel_candidates(spec):
                found = os.path.normpath(os.path.join(upstream_dir, candidate))
                if not os.path.isfile(found):
                    continue
                owner = package_of(found)
                if owner is None:
                    return None
                copy_vendor(owner[0], owner[1])
                relp = os.path.relpath(os.path.join(SRC, vendor_rel(owner[0], owner[1])), dest_dir).replace("\\", "/")
                return relp if relp.startswith(".") else "./" + relp
        return None

    def sub(match):
        rewritten = rewrite(match.group("spec"))
        if rewritten is None:
            return match.group(0)
        changed[0] = True
        return match.group("pre") + match.group("q") + rewritten + match.group("q")

    updated = SPEC_RE.sub(sub, text)
    if changed[0]:
        open(path, "w", encoding="utf-8", newline="").write(updated)


# ---- 1. shell snapshot ----------------------------------------------------------
shell_root = os.path.join(UP, "coding-agent", "src")
shell_files = []
shell_total = 0
for directory, _, files in os.walk(shell_root):
    for name in files:
        source = os.path.join(directory, name)
        rel = os.path.relpath(source, shell_root)
        dest = os.path.join(SRC, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(source, dest)
        shell_total += 1
        if name.endswith(".ts"):
            shell_files.append(dest)

# ---- 2/3. vendor closure + import rewrite --------------------------------------
shutil.rmtree(os.path.join(SRC, "vendor"), ignore_errors=True)

for path in shell_files:
    for match in SPEC_RE.finditer(open(path, encoding="utf-8").read()):
        spec = match.group("spec")
        if spec.startswith("@oh-my-pi/") and not stays_installed(spec):
            if any(spec == name or spec.startswith(name + "/") for name in PKGS):
                enqueue(spec)
for spec in PI_TUI_SEEDS:
    enqueue(spec)

while queue:
    directory, _, rel = queue.pop()
    key = (directory, rel)
    if key in seen:
        continue
    seen.add(key)
    copy_vendor(directory, rel)

for path in shell_files:
    text = open(path, encoding="utf-8").read()
    dest_dir = os.path.dirname(path)

    def rewrite_shell(match):
        spec = match.group("spec")
        if not spec.startswith("@oh-my-pi/"):
            return match.group(0)
        if stays_installed(spec):
            return match.group(0)
        if not any(spec == name or spec.startswith(name + "/") for name in PKGS):
            return match.group(0)
        directory, (target, _) = resolve_vendor(spec)
        rel = locate(directory, target)
        if rel is None:
            raise SystemExit(f"cannot locate {spec}")
        rewritten = os.path.relpath(os.path.join(SRC, vendor_rel(directory, rel)), dest_dir).replace("\\", "/")
        if not rewritten.startswith("."):
            rewritten = "./" + rewritten
        return match.group("pre") + match.group("q") + rewritten + match.group("q")

    updated = SPEC_RE.sub(rewrite_shell, text)
    if updated != text:
        open(path, "w", encoding="utf-8", newline="").write(updated)

# ---- 4. package assets -----------------------------------------------------------
assets = os.path.join(SRC, "assets")
os.makedirs(assets, exist_ok=True)
pkg_dir = os.path.join(UP, "coding-agent")
shutil.copyfile(os.path.join(pkg_dir, "package.json"), os.path.join(assets, "coding-agent-package.json"))
for name in ("CHANGELOG.md", "THIRD-PARTY-NOTICES.txt"):
    if os.path.isfile(os.path.join(pkg_dir, name)):
        shutil.copyfile(os.path.join(pkg_dir, name), os.path.join(assets, name))
shutil.copyfile(os.path.join(os.path.dirname(UP), "LICENSE"), os.path.join(SRC, "LICENSE"))

# ---- 5. local entry patch -------------------------------------------------------
PATCHES = {
    os.path.join(SRC, "main.ts"): [
        (
            "export async function main(args: string[]): Promise<void> {\n"
            "\tregisterLocalInferenceApi();\n"
            '\tconst { runCli } = await import("./cli");\n'
            '\tawait runCli(args.length === 0 ? ["launch"] : args);\n'
            "}",
            "/**\n"
            " * Hermes entry (`hermes omp` runs this file).\n"
            " *\n"
            " * The gateway client composition lives in `modes/rpc/hermes-entry.ts` (outside the vendored\n"
            " * snapshot) so a refresh of `src/` only has to restore this delegator. See that module for the\n"
            " * startup order and the plan §2.3 main-screen assembly.\n"
            " */\n"
            "export async function main(args: string[]): Promise<void> {\n"
            '\tconst { runHermesOmp } = await import("./modes/rpc/hermes-entry");\n'
            "\tawait runHermesOmp(args);\n"
            "}\n"
            "\n"
            "if (import.meta.main) {\n"
            "\tmain(process.argv.slice(2)).catch(async error => {\n"
            '\t\tconst { fatal } = await import("./vendor/utils/src/postmortem.ts");\n'
            "\t\tfatal(error);\n"
            "\t});\n"
            "}",
        )
    ],
}
for path, pairs in PATCHES.items():
    text = open(path, encoding="utf-8").read()
    for old, new in pairs:
        text = text.replace(old, new)
    open(path, "w", encoding="utf-8", newline="").write(text)

# ---- 6. keep relative imports inside src/ (R2) ----------------------------------
RELOCATIONS = {
    os.path.join(SRC, "cli", "license.ts"): [
        ('"../../LICENSE"', '"../LICENSE"'),
        ('"../../THIRD-PARTY-NOTICES.txt"', '"../assets/THIRD-PARTY-NOTICES.txt"'),
    ],
    os.path.join(SRC, "stt", "asr-worker.ts"): [
        ('"../../package.json"', '"../assets/coding-agent-package.json"')
    ],
    os.path.join(SRC, "subprocess", "worker-runtime.ts"): [
        ('"../../package.json"', '"../assets/coding-agent-package.json"')
    ],
    os.path.join(SRC, "tiny", "title-client.ts"): [
        ('"../../package.json"', '"../assets/coding-agent-package.json"')
    ],
    os.path.join(SRC, "utils", "changelog.ts"): [
        ('"../../CHANGELOG.md"', '"../assets/CHANGELOG.md"')
    ],
    os.path.join(SRC, "utils", "markit-cache.ts"): [
        ('"../../package.json"', '"../assets/coding-agent-package.json"')
    ],
}
for path, pairs in RELOCATIONS.items():
    text = open(path, encoding="utf-8").read()
    for old, new in pairs:
        text = text.replace(old, new)
    open(path, "w", encoding="utf-8", newline="").write(text)

print(
    f"sync-upstream: {shell_total} shell files ({len(shell_files)} TypeScript), "
    f"{len(copied)} vendored files ({PINNED_COMMIT[:8]}); run check:imports and check"
)
