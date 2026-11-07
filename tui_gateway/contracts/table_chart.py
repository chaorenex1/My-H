"""Local auto-graph chart-planning RPC contract (ADR-12 D6, ADR-17 P5).

``table_chart.plan`` is the local viewer's proxy onto :mod:`agent.auto_graph`.
The client has already read the table into omp's ``TableAnalysis`` shape and
built a local ``ChartPlan`` guess; the planner keeps that guess wherever a judge
answer is missing or unsure. Local authority only: table content travels in this
one request and is never stored.
"""

from __future__ import annotations

from typing import Literal

from .base import JsonValue, Result
from .common import ProfileParams
from .registry import method


class TableChartPlanParams(ProfileParams):
    markdown: str
    table: JsonValue
    guess: JsonValue
    # ``auto`` only judges through a native judge; ``smart`` may use the text route.
    mode: Literal["auto", "smart"] = "smart"


class TableChartPlanResult(Result):
    plan: JsonValue = None
    source: Literal["judge", "local"]


method("table_chart.plan", params=TableChartPlanParams, result=TableChartPlanResult,
       doc="Plan one table's chart through the judge, falling back to the client's local guess; "
           "local authority only.")
