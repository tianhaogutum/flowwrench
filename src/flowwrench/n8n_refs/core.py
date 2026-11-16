"""Workflow validation, graph analysis and non-executing refactoring."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .scanner import ScanError, replace_references, scan

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 2000
MAX_EDGES = 20000
MAX_DEPTH = 100
MAX_REFERENCES = 20000


class WorkflowError(ValueError):
    def __init__(self, message, *, category=None):
        super().__init__(message)
        self.category = category


def pointer(*parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    pointer: str
    message: str
    node: str | None = None
    reference: str | None = None
    offset: int | None = None

    def to_dict(self):
        return asdict(self)


RULES = {
    "R001": ("error", "Unknown referenced node"),
    "R002": ("error", "Referenced node is not upstream"),
    "R003": ("warning", "Referenced node does not dominate consumer"),
    "R004": ("warning", "Paired-item lookup crosses a pairing hazard"),
    "R005": ("error", "Disabled node is referenced"),
    "R006": ("error", "Duplicate name or dangling connection"),
    "R007": ("error", "Unsupported or ambiguous static analysis input"),
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowError("duplicate JSON object key (ambiguous workflow)", category="invalid-json")
        result[key] = value
    return result


def _bad_constant(value):
    raise WorkflowError("non-finite JSON numbers are not supported", category="invalid-json")


def load_workflow(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        category = ("file-not-found" if isinstance(exc, FileNotFoundError) else
                    "permission-denied" if isinstance(exc, PermissionError) else "file-read")
        # Preserve the CLI's historical sanitized message, never the filename
        # or raw exception repr (which may contain private source).
        raise WorkflowError(exc.strerror or "filesystem error", category=category) from exc
    if len(raw) > MAX_BYTES:
        raise WorkflowError("workflow exceeds 16 MiB input limit", category="input-limit")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise WorkflowError("invalid UTF-8 JSON or excessive nesting", category="invalid-utf8") from exc
    try:
        data = json.loads(decoded, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    except json.JSONDecodeError as exc:
        # Do not echo JSON source lines: these can contain credentials.
        raise WorkflowError("invalid UTF-8 JSON or excessive nesting", category="invalid-json") from exc
    except RecursionError as exc:
        raise WorkflowError("invalid UTF-8 JSON or excessive nesting", category="input-limit") from exc
    validate(data)
    return data


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list) or not isinstance(data.get("connections"), dict):
        raise WorkflowError("expected one workflow object with nodes array and connections object", category="invalid-workflow")
    if not data["nodes"] or len(data["nodes"]) > MAX_NODES:
        raise WorkflowError("workflow must have 1..2000 nodes", category="input-limit" if data["nodes"] else "invalid-workflow")
    stack = [(data, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise WorkflowError("JSON nesting exceeds 100", category="input-limit")
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    for i, node in enumerate(data["nodes"]):
        if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node["name"].strip():
            raise WorkflowError(f"/nodes/{i}: expected node with nonempty string name", category="invalid-workflow")
        if not isinstance(node.get("type"), str) or not node["type"]:
            raise WorkflowError(f"/nodes/{i}/type: expected nonempty string", category="invalid-workflow")
        if not isinstance(node.get("parameters", {}), dict):
            raise WorkflowError(f"/nodes/{i}/parameters: expected object", category="invalid-workflow")
        if "disabled" in node and not isinstance(node["disabled"], bool):
            raise WorkflowError(f"/nodes/{i}/disabled: expected boolean", category="invalid-workflow")
    if "pinData" in data and not isinstance(data["pinData"], dict):
        raise WorkflowError("/pinData: expected object", category="invalid-workflow")
    edge_count = 0
    for source, channels in data["connections"].items():
        if not isinstance(channels, dict):
            raise WorkflowError("connection source must map to an object of output channels", category="invalid-workflow")
        for channel, outputs in channels.items():
            if not isinstance(outputs, list):
                raise WorkflowError("connection channel must be an array of output arrays", category="invalid-workflow")
            for output in outputs:
                if not isinstance(output, list):
                    raise WorkflowError("connection output must be an array", category="invalid-workflow")
                for edge in output:
                    edge_count += 1
                    if edge_count > MAX_EDGES:
                        raise WorkflowError("workflow exceeds 20000 edges", category="input-limit")
                    if not isinstance(edge, dict) or not isinstance(edge.get("node"), str) or not edge["node"]:
                        raise WorkflowError("connection must contain a nonempty node string", category="invalid-workflow")
                    if not isinstance(edge.get("type"), str) or not edge["type"]:
                        raise WorkflowError("connection must contain a type string", category="invalid-workflow")
                    if type(edge.get("index")) is not int or edge["index"] < 0:
                        raise WorkflowError("connection index must be a nonnegative integer", category="invalid-workflow")


def edges(data):
    for source, channels in data["connections"].items():
        for channel, outputs in channels.items():
            for output_index, output in enumerate(outputs):
                for i, edge in enumerate(output):
                    yield source, channel, edge, pointer("connections", source, channel, output_index, i, "node")


def fields(node, index):
    """Yield only executable source, never pinned data, credentials or metadata."""
    stack = [(node.get("parameters", {}), ("nodes", index, "parameters"))]
    code_type = node["type"] in {"n8n-nodes-base.code", "n8n-nodes-base.function", "n8n-nodes-base.functionItem"}
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            stack.extend((child, (*path, key)) for key, child in reversed(list(value.items())))
        elif isinstance(value, list):
            stack.extend((child, (*path, i)) for i, child in reversed(list(enumerate(value))))
        elif isinstance(value, str):
            if code_type and len(path) == 4 and path[-1] in {"jsCode", "pythonCode", "functionCode"}:
                yield path, value, "python" if path[-1] == "pythonCode" else "js", False
            elif value.startswith("="):
                yield path, value, "js", True


class Graph:
    """Bitset transitive closure and dominators, with a virtual entry node.

    Every source SCC is reachable from entry. For a rootless cyclic SCC, each
    member is an entry: we cannot infer the first iteration from an export.
    Known trigger nodes get an entry edge even if feedback edges target them.
    """
    def __init__(self, names, edge_pairs, triggers=()):
        self.names = list(names)
        self.index = {name: i for i, name in enumerate(self.names)}
        n = len(self.names)
        self.pred = [set() for _ in range(n)]
        self.succ = [set() for _ in range(n)]
        for source, target in edge_pairs:
            a, b = self.index[source], self.index[target]
            self.pred[b].add(a)
            self.succ[a].add(b)
        # Iterative Kosaraju avoids recursion limits on long exported chains.
        seen, order = set(), []
        for root in range(n):
            if root in seen:
                continue
            todo = [(root, False)]
            while todo:
                v, done = todo.pop()
                if done:
                    order.append(v)
                elif v not in seen:
                    seen.add(v)
                    todo.append((v, True))
                    todo.extend((w, False) for w in sorted(self.succ[v], reverse=True) if w not in seen)
        component = {}
        groups = []
        for root in reversed(order):
            if root in component:
                continue
            group = []
            todo = [root]
            component[root] = len(groups)
            while todo:
                v = todo.pop()
                group.append(v)
                for w in self.pred[v]:
                    if w not in component:
                        component[w] = len(groups)
                        todo.append(w)
            groups.append(group)
        incoming = {component[v] for v in range(n) for w in self.pred[v] if component[w] != component[v]}
        trigger_ids = {self.index[name] for name in triggers}
        self.roots = set(trigger_ids)
        for c, group in enumerate(groups):
            if c not in incoming:
                explicit = set(group) & trigger_ids
                self.roots.update(explicit or group)
        self.ancestors = [0] * n
        changed = True
        while changed:
            changed = False
            for v in range(n):
                mask = 0
                for p in self.pred[v]:
                    mask |= self.ancestors[p] | (1 << p)
                mask &= ~(1 << v)
                if mask != self.ancestors[v]:
                    self.ancestors[v] = mask
                    changed = True
        universe = (1 << n) - 1
        self.dominators = [(1 << v) if v in self.roots else universe for v in range(n)]
        changed = True
        while changed:
            changed = False
            for v in range(n):
                mask = 0 if v in self.roots else universe
                for p in self.pred[v]:
                    mask &= self.dominators[p]
                mask |= 1 << v
                if mask != self.dominators[v]:
                    self.dominators[v] = mask
                    changed = True

    def is_upstream(self, source, target):
        return bool(self.ancestors[self.index[target]] & (1 << self.index[source]))

    def dominates(self, source, target):
        return bool(self.dominators[self.index[target]] & (1 << self.index[source]))

    def between(self, source, target, candidates):
        return sorted(name for name in candidates if name not in {source, target}
                      and self.is_upstream(source, name) and self.is_upstream(name, target))


def pairing_breaker(node):
    kind = node["type"]
    if kind in {"n8n-nodes-base.merge", "n8n-nodes-base.aggregate", "n8n-nodes-base.summarize", "n8n-nodes-base.itemLists", "n8n-nodes-base.function"}:
        return True
    return kind == "n8n-nodes-base.code" and node.get("parameters", {}).get("mode", "runOnceForAllItems") != "runOnceForEachItem"


def analyze(data):
    validate(data)
    findings, by_name, pairs = [], {}, []

    def add(rule, loc, message, node=None, reference=None, offset=None):
        findings.append(Finding(rule, RULES[rule][0], loc, message, node, reference, offset))

    for i, node in enumerate(data["nodes"]):
        name = node["name"]
        if name in by_name:
            add("R006", pointer("nodes", i, "name"), "duplicate node name", name)
        else:
            by_name[name] = node
    for source, channels in data["connections"].items():
        if source not in by_name:
            add("R006", pointer("connections", source), "connection source does not exist", source)
        for channel in channels:
            if channel != "main":
                add("R007", pointer("connections", source, channel), "only main execution connections are supported", source)
    for source, channel, edge, loc in edges(data):
        target = edge["node"]
        if target not in by_name:
            add("R006", loc, "connection target does not exist", source, target)
        if edge["type"] != channel:
            add("R007", loc.rsplit("/", 1)[0] + "/type", "connection type differs from its channel", source)
        if channel == "main" and source in by_name and target in by_name:
            pairs.append((source, target))
    triggers = [name for name, node in by_name.items() if node["type"].lower().endswith("trigger") or node["type"] == "n8n-nodes-base.webhook"]
    graph = Graph(by_name, pairs, triggers)
    breakers = [name for name, node in by_name.items() if pairing_breaker(node)]
    reference_count = 0
    for i, node in enumerate(data["nodes"]):
        consumer = node["name"]
        for path, value, language, expression in fields(node, i):
            loc = pointer(*path)
            try:
                refs = scan(value, language, expression)
            except ScanError as exc:
                add("R007", loc, str(exc), consumer)
                continue
            reference_count += len(refs)
            if reference_count > MAX_REFERENCES:
                add("R007", loc, "workflow exceeds 20000 static references; analysis stopped", consumer)
                return findings
            for ref in refs:
                name = ref.name
                args = (consumer, name, ref.start)
                if name not in by_name:
                    add("R001", loc, "referenced node does not exist", *args)
                    continue
                if by_name[name].get("disabled", False):
                    add("R005", loc, "referenced node is disabled", *args)
                if not graph.is_upstream(name, consumer):
                    add("R002", loc, "referenced node is not upstream of consumer", *args)
                elif not graph.dominates(name, consumer):
                    add("R003", loc, "referenced node is not executed on every structural path to consumer", *args)
                if ref.paired and graph.is_upstream(name, consumer):
                    hazards = graph.between(name, consumer, breakers)
                    if hazards:
                        add("R004", loc, "paired-item lookup crosses possible pairing breakers: " + ", ".join(hazards), *args)
    return findings


def rename(data, old, new):
    """Return a copy; refuse ambiguous/unsupported input before any write."""
    validate(data)
    if not old.strip() or not new.strip() or any(ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF for ch in new):
        raise WorkflowError("node names must be nonempty and contain no controls or surrogates")
    names = [node["name"] for node in data["nodes"]]
    if old not in names:
        raise WorkflowError("rename source does not exist")
    if old == new or new in names:
        raise WorkflowError("rename target already exists or equals source")
    blocking = [f for f in analyze(data) if f.rule in {"R006", "R007"}]
    if blocking:
        raise WorkflowError("rename refused: resolve R006/R007 findings first")
    if new in data.get("pinData", {}):
        raise WorkflowError("rename target collides with an existing pinData key")
    if new in data["connections"]:
        raise WorkflowError("rename target collides with a connection key")
    result = deepcopy(data)
    for i, node in enumerate(result["nodes"]):
        for path, value, language, expression in list(fields(node, i)):
            changed = replace_references(value, scan(value, language, expression), old, new)
            parent = result
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = changed
        if node["name"] == old:
            node["name"] = new
    result["connections"] = {new if key == old else key: value for key, value in result["connections"].items()}
    for _, _, edge, _ in edges(result):
        if edge["node"] == old:
            edge["node"] = new
    if old in result.get("pinData", {}):
        result["pinData"] = {new if key == old else key: value for key, value in result["pinData"].items()}
    return result
