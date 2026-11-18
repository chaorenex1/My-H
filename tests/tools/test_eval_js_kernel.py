#!/usr/bin/env python3
"""Tests for execute_code's Bun JS kernel (ADR-4 D9/P2).

Design contract under test:

  - cells run in one persistent Bun process per owner; top-level bindings
    (const/let/var, destructuring, functions, classes) survive for later cells
  - the parser-level transform accepts every function shape, static imports,
    exports, import.meta and top-level await/return
  - result frames use the Python kernel's finishing contract (`ok` → `success`,
    redaction/truncation, `--- stderr ---`, timeout text)
  - stream frames reach the host hook; final result remains authoritative
  - process.exit()/EOF and timeouts kill the process; the next call starts fresh
  - the tool bridge is the same allowlist/budget path as the Python kernel

Skipped when PM's locked Bun is not installed (`hermes pm install bun`).
"""

import json
import os
from pathlib import Path

import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools.code_execution_tool import _bun_binary
from tools.eval_js_kernel import run_js_cell, shutdown_all_js_kernels

# Captured before the per-test HERMES_HOME/HOME sandbox: the kernel tests exercise
# Bun, not PM's store resolution (test_code_execution_pm_runtime covers that).
_BUN_PATH = _bun_binary()

pytestmark = pytest.mark.skipif(_BUN_PATH is None, reason="PM Bun is not installed")


@pytest.fixture(autouse=True)
def _fresh_js_registry(monkeypatch):
    monkeypatch.setattr("tools.code_execution_tool._bun_binary", lambda: _BUN_PATH)
    shutdown_all_js_kernels()
    yield
    shutdown_all_js_kernels()


def _run(code, *, reset=False, timeout=20, cwd="", tools=frozenset(), max_calls=10,
         on_stream=None, cell_index=0):
    return json.loads(run_js_cell(
        code, task_id="js-kernel-test", child_cwd=cwd, sandbox_tools=tools,
        timeout=timeout, max_tool_calls=max_calls, reset=reset,
        is_interrupted=lambda: False, on_stream=on_stream, cell_index=cell_index,
    ))


def test_cross_cell_bindings_and_reset():
    first = _run("const answer = 41; let label = 'js'; var items = [1, 2];")
    assert first["status"] == "success", first

    second = _run("console.log(answer + 1, label, items.length)")
    assert second["status"] == "success", second
    assert second["output"] == "42 js 2\n", second
    assert second["kernel"]["reused"] is True

    third = _run("console.log(typeof answer, typeof label, typeof items)", reset=True)
    assert third["status"] == "success", third
    assert third["output"] == "undefined undefined undefined\n", third
    assert third["kernel"]["state_reset"] is True


def test_full_function_shapes_survive_cells():
    declared = _run("""
        const {a, b: [c], ...rest} = {a: 1, b: [2], d: 3, e: 4};
        class Counter { constructor(start = 0) { this.value = start; } bump(by = 1) { return this.value += by; } }
        function classic(x, y = 1, ...more) { return x + y + more.length; }
        function* gen(n) { for (let i = 0; i < n; i += 1) yield i; }
        const arrow = async (n) => n * 2;
        const closure = ((seed) => () => seed + a)(10);
    """)
    assert declared["status"] == "success", declared

    used = _run("""
        const counter = new Counter(5);
        const values = [...gen(3)];
        const doubled = await arrow(4);
        console.log(a, c, rest.d + rest.e, counter.bump(), counter.bump(2),
                    classic(1), values.join(','), doubled, closure());
    """)
    assert used["status"] == "success", used
    assert used["output"].strip() == "1 2 7 6 8 2 0,1,2 8 11", used


def test_top_level_await_and_return():
    awaited = _run("await new Promise((resolve) => setTimeout(() => { console.log('awaited'); resolve(); }, 5));")
    assert awaited["status"] == "success", awaited
    assert "awaited" in awaited["output"]

    returned = _run("return 21 * 2;")
    assert returned["status"] == "success", returned


def test_static_imports_resolve_from_session_cwd(tmp_path):
    (tmp_path / "lib.mjs").write_text(
        "export const k = 7; export default (n) => n * 2;\n", encoding="utf-8")
    (tmp_path / "side.mjs").write_text("globalThis.sideEffect = 'yes';\n", encoding="utf-8")

    result = _run(
        "import dbl, { k } from './lib.mjs'; import './side.mjs';"
        "console.log(k, dbl(3), globalThis.sideEffect);",
        cwd=str(tmp_path),
    )
    assert result["status"] == "success", result
    assert result["output"] == "7 6 yes\n", result


def test_exports_publish_bindings(tmp_path):
    (tmp_path / "m.mjs").write_text("export const k = 7; export default 3;\n", encoding="utf-8")
    result = _run(
        "export const exported = 5; export default 9;"
        "export { k as renamed } from './m.mjs';"
        "console.log(globalThis.exported, globalThis.default, globalThis.renamed);",
        cwd=str(tmp_path),
    )
    assert result["status"] == "success", result
    assert result["output"] == "5 9 7\n", result


def test_import_meta_and_parser_distinguishes_regex(tmp_path):
    result = _run(
        "const re = /a\\/b/g; const q = 10 / 2;"
        "console.log(re.test('a/b'), q, import.meta.dir === process.cwd());",
        cwd=str(tmp_path),
    )
    assert result["status"] == "success", result
    assert result["output"] == "true 5 true\n", result


def test_import_meta_rewrite_is_not_textual(tmp_path):
    marker = "import.meta.url"
    result = _run(
        "const literal = 'import.meta.url';"
        "console.log(typeof import.meta.dir);"
        "const after = 1;"
        "console.log(literal, after, typeof import.meta.filename);",
        cwd=str(tmp_path),
    )
    assert result["status"] == "success", result
    assert result["output"] == f"string\n{marker} 1 string\n", result
    assert result["output"].count(marker) == 1


def test_stream_frames_reach_the_host_and_result_is_authoritative():
    frames = []
    result = _run("console.log('stream-hello'); process.stdout.write('write-path\\n');",
                  on_stream=frames.append, cell_index=5)
    assert result["status"] == "success", result
    assert "stream-hello" in result["output"]
    assert "write-path" in result["output"]
    assert any(frame.get("type") == "stream" and "stream-hello" in frame.get("text", "")
               and frame.get("cell_index") == 5 for frame in frames)


def test_tool_bridge_allowlist_and_budget(monkeypatch):
    import model_tools

    calls = []

    def fake_handle(name, args, task_id=None, **kwargs):
        calls.append((name, args))
        return json.dumps({"tool": name, "args": args})

    monkeypatch.setattr(model_tools, "handle_function_call", fake_handle)

    ok = _run("const r = await echo({x: 1}); console.log(r.args.x);",
              tools=frozenset({"echo"}), max_calls=2)
    assert ok["status"] == "success", ok
    assert ok["output"] == "1\n", ok
    assert ok["tool_calls_made"] == 1
    assert calls == [("echo", {"x": 1})]

    limited = _run("const first = await echo({a: 1}); const second = await echo({b: 2});"
                   "console.log(first.args.a, second.error);",
                   tools=frozenset({"echo"}), max_calls=1, reset=True)
    assert limited["status"] == "success", limited  # the script itself completed
    assert limited["tool_calls_made"] == 1
    assert "Tool call limit reached" in limited["output"]

    denied = _run("console.log(typeof not_allowed);", tools=frozenset({"echo"}))
    assert denied["status"] == "success", denied
    assert denied["output"] == "undefined\n", denied


def test_cross_cell_redeclaration_and_assignment():
    """Indirect eval gives each cell its own lexical scope; the published global
    lets a corrected re-run redeclare a name instead of hitting a duplicate binding."""
    first = _run("const again = 1; let value = 1;")
    assert first["status"] == "success", first

    redeclared = _run("const again = 2; value = 2; console.log(again, value)")
    assert redeclared["status"] == "success", redeclared
    assert redeclared["output"] == "2 2\n", redeclared

    after = _run("console.log(again, value)")
    assert after["status"] == "success", after
    assert after["output"] == "2 2\n", after


def test_staging_start_ignores_project_bunfig_preload(tmp_path):
    """Bun reads bunfig.toml at startup; the kernel starts in its staging dir, so a
    project preload must never run (chdir alone cannot retract it)."""
    (tmp_path / "bunfig.toml").write_text('preload = ["./preload.js"]\n', encoding="utf-8")
    (tmp_path / "preload.js").write_text("globalThis.preloaded = true;\n", encoding="utf-8")

    result = _run("console.log(typeof globalThis.preloaded);", cwd=str(tmp_path))
    assert result["status"] == "success", result
    assert result["output"] == "undefined\n", result


def test_exception_keeps_kernel_alive():
    boom = _run("const kept = 7; throw new Error('boom');")
    assert boom["status"] == "error", boom
    assert "boom" in boom["error"]

    after = _run("console.log(kept)")
    assert after["status"] == "success", after
    assert after["output"] == "7\n"
    assert after["kernel"]["reused"] is True


def test_timeout_kills_kernel_and_next_call_is_fresh():
    timed_out = _run("await new Promise(() => {});", timeout=2)
    assert timed_out["status"] == "timeout", timed_out
    assert "timed out" in timed_out["error"]

    fresh = _run("console.log('fresh')")
    assert fresh["status"] == "success", fresh
    assert fresh["output"] == "fresh\n"
    assert fresh["kernel"]["reused"] is False


def test_process_exit_is_an_error_and_next_call_is_fresh():
    exited = _run("process.exit(3)")
    assert exited["status"] == "error", exited
    assert "session kernel died" in exited["error"]

    fresh = _run("console.log('revived')")
    assert fresh["status"] == "success", fresh
    assert fresh["output"] == "revived\n"
    assert fresh["kernel"]["reused"] is False
