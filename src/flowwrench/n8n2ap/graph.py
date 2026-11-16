"""Connection graph, trigger selection and branch joins (port of ``lib/graph.js``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .input import InputError
from .jscompat import is_array, is_integer, is_object, js_items, truthy

TRIGGERS = frozenset({"webhook", "scheduleTrigger", "cron", "manualTrigger"})
_PREFIX = "n8n-nodes-base."


def node_kind(n: dict[str, Any]) -> str:
    node_type: str = n["type"]
    return node_type[len(_PREFIX):] if node_type.startswith(_PREFIX) else "unknown"


@dataclass(frozen=True)
class Edge:
    source: str  # JavaScript ``from``
    to: str
    output: int
    input: int | float
    channel: str


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]]
    edges: dict[str, list[Edge]]
    incoming: dict[str, list[Edge]]
    trigger: dict[str, Any]
    reachable: set[str]
    side_channels: set[str]
    _post: dict[str, dict[str, None]]
    _order_index: dict[str, int]

    def main(self, name: str) -> list[Edge]:
        return [e for e in self.edges[name] if e.channel == "main"]

    def join(self, a: str | None, b: str | None) -> str | None:
        if not a or not b:
            return None
        other = self._post[b]
        common = [x for x in self._post[a] if x in other]
        if not common:
            return None
        # Highest post-order index first; indexes are unique so the order is total.
        return max(common, key=self._order_index.__getitem__)


def build_graph(workflow: dict[str, Any], override: str | None) -> Graph:
    nodes: dict[str, dict[str, Any]] = {n["name"]: n for n in workflow["nodes"]}
    edges: dict[str, list[Edge]] = {name: [] for name in nodes}
    incoming: dict[str, list[Edge]] = {name: [] for name in nodes}
    side_channels: set[str] = set()
    edge_count = 0
    for name, outputs in js_items(workflow["connections"]):
        if name not in nodes or not is_object(outputs):
            raise InputError("Connection source does not exist or has invalid outputs")
        for channel, ports in js_items(outputs):
            if not is_array(ports):
                raise InputError("Connection ports must be arrays")
            for output, port in enumerate(ports):
                if not is_array(port):
                    raise InputError("Connection output must be an array")
                for e in port:
                    edge_count += 1
                    if edge_count > 5000:
                        raise InputError("Workflow exceeds 5000 connections")
                    if (not is_object(e) or not isinstance(e.get("node"), str) or e["node"] not in nodes
                            or not isinstance(e.get("type"), str) or not is_integer(e.get("index"))
                            or e["index"] < 0):
                        raise InputError("Invalid connection target/type/index")
                    edge = Edge(source=name, to=e["node"], output=output, input=e["index"], channel=channel)
                    if any(x.to == edge.to and x.output == output and x.input == e["index"] and x.channel == channel
                           for x in edges[name]):
                        raise InputError("Duplicate connection")
                    edges[name].append(edge)
                    incoming[e["node"]].append(edge)
                    if channel != "main" or e["type"] != "main":
                        side_channels.add(name)
                        side_channels.add(e["node"])

    # Includes disconnected nodes and non-main connections: cycles are never ignored.
    visiting: set[str] = set()
    visited: set[str] = set()
    order: list[str] = []

    def visit(name: str) -> None:
        if name in visiting:
            raise InputError("Cycle in workflow connections")
        if name in visited:
            return
        visiting.add(name)
        for e in edges[name]:
            visit(e.to)
        visiting.discard(name)
        visited.add(name)
        order.append(name)

    for name in nodes:
        visit(name)

    if override is not None:
        matches = [n for n in workflow["nodes"] if n["name"] == override or n.get("id") == override]
        if len(matches) != 1:
            raise InputError("--trigger must identify exactly one node by name or id")
        trigger = matches[0]
        if node_kind(trigger) not in TRIGGERS:
            raise InputError("--trigger must be a supported trigger node")
    else:
        candidates = [n for n in workflow["nodes"] if node_kind(n) in TRIGGERS and not truthy(n.get("disabled"))]
        if len(candidates) != 1:
            raise InputError("Expected exactly one enabled trigger; select one with --trigger")
        trigger = candidates[0]
    if incoming[trigger["name"]]:
        raise InputError("Selected trigger has incoming connections")
    if truthy(trigger.get("disabled")):
        raise InputError("Selected trigger is disabled")

    reachable: set[str] = set()

    def reach(name: str) -> None:
        if name in reachable:
            return
        reachable.add(name)
        for e in edges[name]:
            reach(e.to)

    reach(trigger["name"])

    def main(name: str) -> list[Edge]:
        return [e for e in edges[name] if e.channel == "main"]

    # Ordered sets (dict keys) of nodes every main path from a node passes through.
    post: dict[str, dict[str, None]] = {}
    for name in order:
        following = [post[e.to] for e in main(name)]
        if following:
            common = {x: None for x in following[0] if all(x in s for s in following)}
        else:
            common = {}
        common[name] = None
        post[name] = common
    order_index = {name: index for index, name in enumerate(order)}
    return Graph(nodes=nodes, edges=edges, incoming=incoming, trigger=trigger, reachable=reachable,
                 side_channels=side_channels, _post=post, _order_index=order_index)
