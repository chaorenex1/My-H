"""P2: Go/Rust extraction, cross-file graphs, query envelope limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.hermes_codegraph.helpers import commit, make_repo


@pytest.fixture
def multi_repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "b.py").write_text(
        "def helper(x):\n"
        "    return x\n"
        "\n"
        "class Base:\n"
        "    def run(self):\n"
        "        return helper(1)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "a.py").write_text(
        "from .b import helper, Base\n"
        "\n"
        "class Child(Base):\n"
        "    def run(self):\n"
        "        return helper(2)\n"
        "\n"
        "def use():\n"
        "    return Child().run()\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text(
        "from pkg.a import use\n"
        "\n"
        "def test_use():\n"
        "    assert use() == 2\n",
        encoding="utf-8",
    )
    (root / "main.go").write_text(
        "package main\n"
        'import (\n'
        '    "fmt"\n'
        '    "example.com/repo/pkg/util"\n'
        ')\n'
        "type Shape interface { Area() int }\n"
        "type Circle struct { R int }\n"
        "func (c Circle) Area() int { return c.R }\n"
        "func main() { c := Circle{R: 2}; fmt.Println(c.Area()); util.Foo() }\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util").mkdir()
    (root / "pkg" / "util" / "util.go").write_text(
        "package util\nfunc Foo() int { return 1 }\n",
        encoding="utf-8",
    )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "util.rs").write_text(
        "pub fn foo() -> i32 { 1 }\n", encoding="utf-8"
    )
    (root / "src" / "main.rs").write_text(
        "use crate::util::foo;\nfn main() { foo(); }\n", encoding="utf-8"
    )
    (root / "lib.rs").write_text(
        "pub trait Shape { fn area(&self) -> i32; }\n"
        "pub struct P { x: i32 }\n"
        "impl Shape for P { fn area(&self) -> i32 { self.x } }\n"
        "fn helper() -> P { P { x: 1 } }\n"
        "#[test]\n"
        "fn test_p() { let p = helper(); p.area(); }\n",
        encoding="utf-8",
    )
    commit(root)
    return root


def _open(root: Path):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    return CodeIntelligence.open(root=root, limits=CodeGraphLimits())


def test_go_and_rust_symbols_are_indexed(code_home, multi_repo):
    with _open(multi_repo) as ci:
        result = ci.index()
        assert result["status"] == "complete"
        symbols = {
            item["qualified_name"]: item
            for item in ci.find_symbols(limit=200)["results"]
        }
        assert symbols["main.Circle"]["kind"] == "struct"
        assert symbols["main.Circle.Area"]["kind"] == "method"
        assert symbols["lib.Shape"]["kind"] == "trait"
        assert symbols["lib.P"]["kind"] == "struct"
        assert symbols["lib.P.area"]["kind"] == "method"


def test_cross_file_calls_and_dependencies(code_home, multi_repo):
    with _open(multi_repo) as ci:
        ci.index()
        use = ci.find_symbols(name="use")["results"][0]
        callers = ci.get_callers(use["symbol_id"], policy="exploratory")["results"]
        assert [item["qualified_name"] for item in callers] == ["tests.test_a.test_use"]

        dependencies = ci.get_dependencies("pkg.a", depth=2)["results"]
        assert [item["qualified_name"] for item in dependencies] == ["pkg.b"]

        callees = ci.get_callees(use["symbol_id"], policy="exploratory")["results"]
        assert {item["qualified_name"] for item in callees} >= {
            "pkg.a.Child",
            "pkg.a.Child.run",
        }

        impact = ci.get_impact(
            use["symbol_id"], include=("dependencies",), policy="exploratory"
        )["results"]
        assert [item["qualified_name"] for item in impact] == ["pkg.b"]


def test_go_and_rust_cross_module_calls_are_resolved(code_home, multi_repo):
    with _open(multi_repo) as ci:
        ci.index()
        go_main = next(
            item for item in ci.find_symbols(name="main", limit=50)["results"]
            if item["qualified_name"] == "main.main"
        )
        go_callees = ci.get_callees(
            go_main["symbol_id"], policy="strict"
        )["results"]
        assert any(
            item["qualified_name"] == "pkg/util.Foo"
            and item["edge"]["resolution_status"] == "resolved"
            for item in go_callees
        )

        rust_main = next(
            item for item in ci.find_symbols(name="main", limit=50)["results"]
            if item["qualified_name"] == "src::main.main"
        )
        rust_callees = ci.get_callees(
            rust_main["symbol_id"], policy="strict"
        )["results"]
        assert any(
            item["qualified_name"] == "src::util.foo"
            and item["edge"]["resolution_status"] == "resolved"
            for item in rust_callees
        )


def test_type_hierarchy_and_static_tests(code_home, multi_repo):
    with _open(multi_repo) as ci:
        ci.index()
        child = ci.find_symbols(name="Child")["results"][0]
        types = ci.get_type_hierarchy(child["symbol_id"], policy="exploratory")["results"]
        assert [item["qualified_name"] for item in types] == ["pkg.b.Base"]

        use = ci.find_symbols(name="use")["results"][0]
        tests = ci.get_tests_for_symbol(use["symbol_id"])["results"]
        assert [item["qualified_name"] for item in tests] == ["tests.test_a.test_use"]

        rust_shape = next(
            item for item in ci.find_symbols(name="Shape", limit=50)["results"]
            if item["qualified_name"] == "lib.Shape"
        )
        implementations = ci.get_implementations(
            rust_shape["symbol_id"], policy="exploratory"
        )["results"]
        assert {item["qualified_name"] for item in implementations} >= {"lib.P"}


def test_query_limits_mark_truncated(code_home, multi_repo):
    with _open(multi_repo) as ci:
        ci.index()
        use = ci.find_symbols(name="use")["results"][0]
        result = ci.get_callees(use["symbol_id"], policy="exploratory", limit=1)
        assert result["metadata"]["truncated"] is True
        assert len(result["results"]) == 1
        assert result["metadata"]["limits"]["limit"] == 1


def test_repositories_do_not_mix(code_home, tmp_path):
    first = make_repo(tmp_path / "one")
    (first / "a.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    commit(first)
    second = make_repo(tmp_path / "two")
    (second / "b.py").write_text("def shared():\n    return 2\n", encoding="utf-8")
    commit(second)
    with _open(first) as one, _open(second) as two:
        one.index()
        two.index()
        one_symbols = one.find_symbols(name="shared", limit=20)["results"]
        two_symbols = two.find_symbols(name="shared", limit=20)["results"]
        assert {item["qualified_name"] for item in one_symbols} == {"a.shared"}
        assert {item["qualified_name"] for item in two_symbols} == {"b.shared"}


def test_diagnostics_query_excludes_stored_semgrep_rows(code_home, tmp_path):
    """ADR-15 D1: the query surface never returns stored Semgrep findings, and a page short after
    filtering re-fetches a larger one until it fills or the graph runs out."""
    from hermes_graph.serialization import new_node_id

    root = make_repo(tmp_path / "diag")
    (root / "mod.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    commit(root)
    with _open(root) as ci:
        ci.index()
        repo_id = ci.repository.repository_id

        def _diag(tool: str, line: int, code: str) -> None:
            ci._client.nodes.upsert(
                "CodeDiagnostic",
                f"diag_{new_node_id()}",
                {
                    "repository_id": repo_id,
                    "file_path": "mod.py",
                    "line_number": line,
                    "column_number": 1,
                    "code": code,
                    "tool": tool,
                    "severity": "warning",
                    "message": f"{code} finding",
                },
            )

        for index in range(25):
            _diag("semgrep", index + 1, f"semgrep.rule.{index}")
        _diag("tree_sitter", 1, "parse-ambiguous")
        _diag("tree_sitter", 2, "parse-timeout")

        payload = ci.get_diagnostics(limit=2)

    assert [item["code"] for item in payload["results"]] == ["parse-ambiguous", "parse-timeout"]
    assert {item["tool"] for item in payload["results"]} == {"tree_sitter"}


def test_search_without_tool_refetches_and_flags_truncation():
    """ADR-15 D1: a page dominated by excluded rows re-fetches; a cut page is truncated even when
    the graph itself reported no more rows."""
    from types import SimpleNamespace

    from hermes_codegraph.query import _search_without_tool

    class _Nodes:
        def __init__(self, tools: list[str]) -> None:
            self.tools = tools
            self.calls: list[int] = []

        def search(self, node_type, filters, *, limit):
            self.calls.append(limit)
            items = [
                SimpleNamespace(properties={"tool": tool}) for tool in self.tools[:limit]
            ]
            return SimpleNamespace(items=items, has_more=len(self.tools) > limit)

    dominated = _Nodes(["semgrep"] * 30 + ["tree_sitter"] * 3)
    items, truncated = _search_without_tool(
        SimpleNamespace(nodes=dominated), "CodeDiagnostic", {},
        field="tool", value="semgrep", limit=2,
    )
    assert dominated.calls == [20, 40]  # the short first page triggered a refetch
    assert [item.properties["tool"] for item in items] == ["tree_sitter"] * 2
    assert truncated is True  # the [:limit] cut dropped the third kept row

    starved = _Nodes(["semgrep"] * 3000 + ["tree_sitter"])
    items, truncated = _search_without_tool(
        SimpleNamespace(nodes=starved), "CodeDiagnostic", {},
        field="tool", value="semgrep", limit=2,
    )
    assert starved.calls[-1] == 1000  # stops at the graph's max page
    assert items == [] and truncated is True
