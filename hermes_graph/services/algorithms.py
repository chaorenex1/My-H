"""Read-only graph algorithms over scope snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..exceptions import AlgorithmError, AlgorithmUnavailableError, ValidationError

if TYPE_CHECKING:
    from ..context import GraphContext


class AlgorithmService:
    def __init__(self, ctx: GraphContext):
        self.ctx = ctx

    def degree(
        self, scope: str, *, filters: dict[str, Any] | None = None
    ) -> dict[str, float]:
        nx = self._nx()
        graph = self._simple_directed(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        values = nx.degree_centrality(graph)
        return {node: float(values[node]) for node in sorted(values)}

    def centrality(
        self, method: str = "pagerank", *, scope: str, weight: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        if weight is not None and not isinstance(weight, str):
            raise ValidationError("weight must be an edge property name")
        nx = self._nx()
        graph = self._simple_directed(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        if method == "pagerank":
            values = self._pagerank(nx, graph, weight)
        elif method == "betweenness":
            values = nx.betweenness_centrality(graph, weight=weight)
        elif method == "closeness":
            values = nx.closeness_centrality(graph, distance=weight)
        else:
            raise AlgorithmError(f"unsupported centrality method {method!r}")
        return {node: float(values[node]) for node in sorted(values)}

    def community(
        self, scope: str, method: str = "louvain",
        *, filters: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        if method != "louvain":
            raise AlgorithmError(f"unsupported community method {method!r}")
        nx = self._nx()
        graph = self._simple_undirected(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        if not graph:
            return {}
        communities = list(nx.community.louvain_communities(graph, seed=0))
        communities.sort(key=lambda members: min(members))
        mapping: dict[str, int] = {}
        for index, members in enumerate(communities):
            for node in members:
                mapping[str(node)] = index
        return {node: mapping[node] for node in sorted(mapping)}

    def connected_components(
        self, scope: str, *, filters: dict[str, Any] | None = None
    ) -> list[list[str]]:
        nx = self._nx()
        graph = self._simple_directed(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        if not graph:
            return []
        components = [sorted(str(node) for node in part)
                      for part in nx.weakly_connected_components(graph)]
        components.sort(key=lambda part: part[0] if part else "")
        return components

    def topological_sort(
        self, scope: str, *, filters: dict[str, Any] | None = None
    ) -> list[str]:
        nx = self._nx()
        graph = self._simple_directed(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        if not graph:
            return []
        if not nx.is_directed_acyclic_graph(graph):
            raise AlgorithmError("graph contains a cycle; topological sort is undefined")
        return [str(node) for node in nx.topological_sort(graph)]

    def cycles(
        self, scope: str, *, filters: dict[str, Any] | None = None
    ) -> list[list[str]]:
        """Elementary cycles in the directed graph, each sorted and stably ordered."""
        nx = self._nx()
        graph = self._simple_directed(
            nx, self.ctx.index.nx_graph(scope, filters=filters)
        )
        if not graph:
            return []
        found = [sorted(str(node) for node in cycle) for cycle in nx.simple_cycles(graph)]
        found.sort(key=lambda cycle: (len(cycle), cycle))
        return found

    # ── internals ────────────────────────────────────────────────────────
    @classmethod
    def _pagerank(cls, nx: Any, graph: Any, weight: str | None) -> dict[str, float]:
        """NetworkX pagerank, with a pure-Python fallback when scipy is absent."""
        try:
            return nx.pagerank(graph, weight=weight)
        except (ImportError, ModuleNotFoundError):
            return cls._pagerank_python(graph, weight)

    @staticmethod
    def _pagerank_python(graph: Any, weight: str | None) -> dict[str, float]:
        nodes = sorted(graph.nodes)
        count = len(nodes)
        if count == 0:
            return {}
        index = {node: position for position, node in enumerate(nodes)}
        out_weights = [0.0] * count
        links: list[list[tuple[int, float]]] = [[] for _ in range(count)]
        for source, target, data in graph.edges(data=True):
            value = data.get(weight, 1.0) if weight else 1.0
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                value = 1.0
            value = float(value)
            if value <= 0:
                continue
            source_index = index[source]
            links[source_index].append((index[target], value))
            out_weights[source_index] += value
        alpha = 0.85
        scores = [1.0 / count] * count
        for _ in range(100):
            dangling = sum(
                scores[position]
                for position in range(count)
                if out_weights[position] == 0
            )
            updated = [0.0] * count
            for source in range(count):
                if out_weights[source] == 0:
                    continue
                share = alpha * scores[source] / out_weights[source]
                for target, value in links[source]:
                    updated[target] += share * value
            base = (1.0 - alpha) / count + alpha * dangling / count
            updated = [value + base for value in updated]
            if sum(abs(updated[i] - scores[i]) for i in range(count)) < count * 1e-6:
                scores = updated
                break
            scores = updated
        remainder = 1.0 - sum(scores)
        if abs(remainder) > 0 and count:
            scores = [value + remainder / count for value in scores]
        return {nodes[position]: scores[position] for position in range(count)}

    @staticmethod
    def _nx() -> Any:
        try:
            import networkx as nx  # optional extra, imported lazily
        except ImportError as exc:
            raise AlgorithmUnavailableError(
                "graph algorithms need networkx; run "
                "`hermes pm install --extra graph`",
                details={"module": "networkx"},
            ) from exc
        return nx

    @classmethod
    def _simple_directed(cls, nx: Any, graph: Any) -> Any:
        simple = nx.DiGraph()
        simple.add_nodes_from((node, graph.nodes[node]) for node in sorted(graph.nodes))
        for source, target, data in sorted(
            graph.edges(data=True), key=lambda edge: (edge[0], edge[1], edge[2].get("edge_id", ""))
        ):
            weight = data.get("weight", 1.0)
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                weight = 1.0
            if simple.has_edge(source, target):
                simple[source][target]["weight"] += float(weight)
            else:
                simple.add_edge(source, target, **data)
                simple[source][target]["weight"] = float(weight)
        return simple

    @classmethod
    def _simple_undirected(cls, nx: Any, graph: Any) -> Any:
        simple = nx.Graph()
        simple.add_nodes_from((node, graph.nodes[node]) for node in sorted(graph.nodes))
        for source, target, data in sorted(
            graph.edges(data=True), key=lambda edge: (edge[0], edge[1], edge[2].get("edge_id", ""))
        ):
            weight = data.get("weight", 1.0)
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                weight = 1.0
            if simple.has_edge(source, target):
                simple[source][target]["weight"] += float(weight)
            else:
                simple.add_edge(source, target, **data)
                simple[source][target]["weight"] = float(weight)
        return simple

