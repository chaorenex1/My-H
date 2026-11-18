#!/usr/bin/env bash
# Evaluator-authored independent verification for sprint 1 (title-protocol).
#
# Re-derives AS-1..AS-5 from the shipped implementation through its public
# entry points (manifest, renderer, parser, generate_title, retitle-skills AST).
# This is NOT a wrapper around the Generator's pytest suites; the only shared
# input is the repository source itself.
#
# Usage: .harness/scripts/eval-title-protocol.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2
SPRINT_DIR=".harness/agent-title-system/sprints/sprint-1-title-protocol"
OUT="$SPRINT_DIR/evidence/iter-1"
mkdir -p "$OUT"
LOG="$OUT/eval-title-protocol.log"
: > "$LOG"

scripts/run-in-hermes-env python - "$OUT" <<'PY' 2>&1 | tee -a "$LOG"
import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

repo = Path.cwd()
out = Path(sys.argv[1])
failures: list[str] = []
checks = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if cond:
        print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        failures.append(label)
        print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def expect_raises(fn, exc, label: str, needle: str | None = None) -> None:
    try:
        fn()
    except exc as e:
        check(needle is None or needle in str(e), label, f"{type(e).__name__}: {e}"[:160])
    except BaseException as e:  # noqa: BLE001 - report the real type instead of masking it
        check(False, label, f"raised {type(e).__name__}: {e}"[:160])
    else:
        check(False, label, "did not raise")


# ---------------------------------------------------------------- AS-1
print("=== AS-1: request-scope registration + StrictUndefined ===")
from agent.prompts import prompt_sections as ps
from agent.prompts import renderer
from agent.prompts.prompt_sections import PromptSectionError

by_key = {raw["key"]: raw for raw in ps.load_manifest()["sections"]}
title = by_key.get("title_system")
task = by_key.get("task_label")
check(title is not None, "title_system registered")
check(title is not None and title.get("scope") == "request", "title_system scope=request")
check(title is not None and title.get("path") == "title-system.md", "title_system path=title-system.md")
check(title is not None and title.get("context_keys") == ["title_language"], "title_system context_keys=[title_language]")
check(title is not None and title.get("context_types") == {"title_language": "str"}, "title_system context type is str")
check(task is not None, "task_label registered")
check(task is not None and task.get("scope") == "request", "task_label scope=request")
check(task is not None and task.get("context_keys") == [], "task_label declares no variables")
check(task is not None and task.get("context_types") == {}, "task_label context_types empty")

empty = renderer.render_request("title_system", {"title_language": ""})
french = renderer.render_request("title_system", {"title_language": "French"})
check(bool(empty.strip()), "title_system renders with empty title_language")
check(bool(french.strip()), "title_system renders with title_language=French")
check("- Write the title in French." in french, "French render carries the language line")
task_label = renderer.render_request("task_label", {})
check(bool(task_label.strip()), "task_label renders non-empty")
check("{{" not in task_label and "{%" not in task_label, "task_label has no template residue")
expect_raises(lambda: renderer.render_request("title_system", {}), PromptSectionError, "missing title_language rejected")
expect_raises(lambda: renderer.render_request("title_system", {"bogus": "x"}), PromptSectionError, "undeclared title_system key rejected")
expect_raises(lambda: renderer.render_request("task_label", {"assignment": "x"}), PromptSectionError, "undeclared task_label key rejected")
expect_raises(lambda: renderer.render_request("does-not-exist", {}), PromptSectionError, "unknown section rejected", "unknown prompt section")
check("title-marker-instruction" not in by_key, "title-marker-instruction absent from manifest")

# ---------------------------------------------------------------- AS-2
print("=== AS-2: language line byte identity ===")
head_bytes = subprocess.run(
    ["git", "show", "HEAD:agent/prompts/system/title-system.md"], capture_output=True, check=True
).stdout
head_text = head_bytes.decode("utf-8")
current_text = (repo / "agent/prompts/system/title-system.md").read_text(encoding="utf-8")
env = ps.environment()
head_empty = env.from_string(head_text).render(title_language="")
cur_empty = env.from_string(current_text).render(title_language="")
check(head_empty == cur_empty, "empty-language render is byte-identical to the pre-change template", f"{len(head_empty)} chars")
check(head_empty.endswith("\n") and cur_empty.endswith("\n"), "empty-language render keeps the trailing newline")
check("Write the title in" not in cur_empty, "empty language emits no language line")
cur_fr = env.get_template("title-system.md").render(title_language="French")
check(cur_fr == head_empty + "- Write the title in French.", "French render is the pre-change bytes plus the language line")
check(renderer.render_request("title_system", {"title_language": "French"}).endswith("- Write the title in French."), "render_request keeps the language line")

# ---------------------------------------------------------------- AS-3
print("=== AS-3: marker parsing ===")
from agent.title_parser import TitleParse, clean_title, parse_title_output

check(parse_title_output("<title>Fix retry queue</title>") == TitleParse("Fix retry queue"), "closed tag yields the inner title")
check(parse_title_output("<title/>") == TitleParse(None, terminal=True), "self-closing tag terminates")
check(parse_title_output("<title>none</title>") == TitleParse(None, terminal=True), "none inner terminates")
check(parse_title_output("Fix retry queue") == TitleParse("Fix retry queue"), "unlabeled prose is the title")
multiline = parse_title_output("\n  \n<title>Fix retry queue</title>\nextra")
check(multiline.title == "Fix retry queue", "multiline takes the first non-empty line", repr(multiline.title))
check("extra" not in (multiline.title or ""), "trailing extra line is discarded")
check(parse_title_output("") == TitleParse(None), "empty reply is unusable, not terminal")
check(parse_title_output("<title></title>") == TitleParse(None), "empty marker is unusable, not terminal")

# ---------------------------------------------------------------- AS-4
print("=== AS-4: shared guard ===")
check(clean_title('"Fix retry queue"') == "Fix retry queue", "quotes stripped")
check(clean_title("Title: Fix retry queue") == "Fix retry queue", "Title: prefix stripped")
check(clean_title("Fix retry queue.") == "Fix retry queue", "trailing period stripped")
check(clean_title("Fix retry queue!") == "Fix retry queue", "trailing bang stripped")
check(clean_title("x" * 81) is None, "81 characters rejected")
check(parse_title_output("<title>" + "x" * 81 + "</title>").title is None, "81-char marker rejected, not truncated")
check(clean_title(" ".join(["word"] * 13)) is None, "13 words rejected")
check(bool(clean_title(" ".join(["word"] * 12))), "12 words accepted")
check(clean_title("   ") is None, "blank rejected")
check(clean_title("!!! ???") is None, "pure punctuation rejected")
think = "<think" + ">the user reports a retry bug</think" + ">\n<title>Fix retry queue</title>"
parsed = parse_title_output(think)
check(parsed.title == "Fix retry queue", "think block stripped before parse", repr(parsed.title))
check("retry bug" not in (parsed.title or ""), "thinking text never reaches the title")
check(parse_title_output("<think" + ">only reasoning here</think" + ">") == TitleParse(None), "think-only reply is unusable")

# ---------------------------------------------------------------- AS-5
print("=== AS-5: JSON schema path retired ===")
retired = ["_TITLE_RESPONSE_FORMAT", "_extract_json_title", "_title_from_reasoning", "_is_prompt_example_echo"]
skip_parts = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "build", "dist",
    "site-packages", ".hermes", ".worktrees", "environments", ".pytest_cache", ".ruff_cache",
    ".omp", ".harness", ".specs",
}
defs = {s: [] for s in retired}
refs = {s: [] for s in retired}
files = 0
for path in repo.rglob("*.py"):
    rel = path.relative_to(repo)
    if any(part in skip_parts for part in rel.parts):
        continue
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, UnicodeDecodeError):
        continue
    files += 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in defs:
            defs[node.name].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in defs:
                    defs[target.id].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in defs:
            defs[node.target.id].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in refs:
            refs[node.id].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in refs:
            refs[node.attr].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if name in refs:
                    refs[name].append(f"{rel}:{node.lineno}")

scan_lines = [f"scanned {files} Python files (repo-rooted AST walk)", ""]
check(files > 500, "AST scan covered the repository", f"{files} files")
for symbol in retired:
    check(not defs[symbol], f"{symbol} has no definition", ", ".join(defs[symbol][:3]))
    check(not refs[symbol], f"{symbol} has no call site", ", ".join(refs[symbol][:3]))
    scan_lines.append(f"{symbol}: definitions={defs[symbol] or 'none'} references={refs[symbol] or 'none'}")
(out / "ast-scan.txt").write_text("\n".join(scan_lines) + "\n", encoding="utf-8")

import agent.title_generator as tg

captured: dict = {}


def spy(**kwargs):
    captured.update(kwargs)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="<title>Fix retry queue</title>"))]
    )


tg._auto_title_enabled = lambda: True
tg._title_config = lambda: {"enabled": True, "language": "French"}
tg.call_llm = spy
title_out = tg.generate_title("the retry queue drops jobs when redis restarts")
check(title_out == "Fix retry queue", "generate_title parses the marker reply", repr(title_out))
check(captured.get("task") == "title_generation", "naming request keeps its auxiliary task")
check("extra_body" not in captured, "call_llm received no extra_body")
check("response_format" not in captured, "call_llm received no response_format")
expected_prompt = renderer.render_request("title_system", {"title_language": "French"})
check(captured.get("messages", [{}])[0].get("content") == expected_prompt, "system prompt is the registered title_system render")
check(expected_prompt.endswith("- Write the title in French."), "registered prompt carries the configured language")

gen_src = (repo / "agent/title_generator.py").read_text(encoding="utf-8")
gen_tree = ast.parse(gen_src)
gen_fn = next((n for n in ast.walk(gen_tree) if isinstance(n, ast.FunctionDef) and n.name == "generate_title"), None)
check(gen_fn is not None, "generate_title found")
if gen_fn is not None:
    seg = ast.get_source_segment(gen_src, gen_fn) or ""
    check("response_format" not in seg and "extra_body" not in seg, "generate_title builds no structured-output field")
    check(
        any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "parse_title_output"
            for n in ast.walk(gen_fn)
        ),
        "generate_title uses the shared marker parser",
    )

sessions_src = (repo / "hermes_cli/sessions_cmd.py").read_text(encoding="utf-8")
sessions_tree = ast.parse(sessions_src)
retitle = next(
    (n for n in ast.walk(sessions_tree) if isinstance(n, ast.FunctionDef) and n.name == "_cmd_retitle_skills"),
    None,
)
check(retitle is not None, "sessions retitle-skills handler found")
if retitle is not None:
    seg = ast.get_source_segment(sessions_src, retitle) or ""
    calls = [n for n in ast.walk(retitle) if isinstance(n, ast.Call)]
    check(any(isinstance(c.func, ast.Name) and c.func.id == "generate_title" for c in calls), "retitle-skills calls generate_title")
    check(
        any(
            isinstance(n, ast.ImportFrom)
            and n.module == "agent.title_generator"
            and any(a.name == "generate_title" for a in n.names)
            for n in ast.walk(retitle)
        ),
        "retitle-skills imports generate_title from the shared module",
    )
    check("response_format" not in seg and "_TITLE_RESPONSE_FORMAT" not in seg, "retitle-skills builds no JSON request")

marker_rel = "agent/prompts/system/title-marker-instruction.md"
check((repo / marker_rel).exists(), "title-marker-instruction.md archived")
check("title-marker-instruction" not in by_key, "title-marker-instruction.md unregistered")
tracked = subprocess.run(["git", "ls-files", "--error-unmatch", marker_rel], capture_output=True)
check(tracked.returncode == 0, "title-marker-instruction.md is an archived tracked file")
dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", marker_rel], capture_output=True)
check(dirty.returncode == 0, "title-marker-instruction.md not rewritten")

passed = checks - len(failures)
print(f"\n{passed}/{checks} independent checks passed")
if failures:
    print("FAILED CHECKS:")
    for name in failures:
        print(" -", name)
    sys.exit(1)
print("ALL EVALUATOR CHECKS PASSED")
PY
status=${PIPESTATUS[0]}
printf '\neval-title-protocol exit=%s\n' "$status" | tee -a "$LOG"
exit "$status"
