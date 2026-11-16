"""Huginn schema v1 <-> GUID graph; no network calls or agent execution."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import re

from .formats import (AgentValidationError, KitError, MAX_DEPTH, MAX_FILE_BYTES, MAX_KIT_BYTES, MAX_NODES,
                      ReferenceValidationError, check_path, json_value, load_data, yaml_text)

SCHEDULES = frozenset(
    "every_1m every_2m every_5m every_10m every_30m every_1h every_2h every_5h "
    "every_12h every_1d every_2d every_7d midnight noon never".split()
    + [f"{hour}{suffix}" for suffix in ("am", "pm") for hour in range(1, 12)]
)
EDGE_FIELDS = {"links": ("source", "receiver"),
               "control_links": ("controller", "control_target")}
GUID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
VARIABLE_NAME = r"[A-Za-z_][A-Za-z0-9_.-]*"
PLACEHOLDER = re.compile(r"\$\{HSK:(" + VARIABLE_NAME + r")\}")
CREDENTIAL_TAG = re.compile(r"{%-?\s*credential\s+(.*?)\s*-?%}", re.DOTALL)
CREDENTIAL_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


def mapping(value, context):
    if type(value) is not dict:
        raise KitError(f"{context} must be a mapping")
    return value


def nonempty(value, context):
    if type(value) is not str or not value.strip():
        raise KitError(f"{context} must be a nonempty string")


def guid_value(value, context):
    if type(value) is not str or not GUID.fullmatch(value):
        raise KitError(f"{context} must be 1-128 ASCII letters, digits, '_' or '-', starting alphanumeric")


def unknown_schedules(doc):
    return [a["guid"] for a in doc["agents"]
            if a.get("schedule") is not None and a["schedule"] not in SCHEDULES]


def reference_links(doc, kind, fields, source):
    """Check the shared edge shape in input order, before any normalization."""
    links = doc.get(kind)
    if type(links) is not list or len(links) > 10_000:
        raise ReferenceValidationError(f"/{kind}", f"{kind} must be a list of at most 10000 links",
                                       source=source)
    for index, link in enumerate(links):
        if type(link) is not dict or set(link) != set(fields):
            raise ReferenceValidationError(f"/{kind}/{index}",
                                           f"link must contain exactly {', '.join(fields)}", source=source)
    return links


def pointer_path(parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def agent_location(agent, index, agent_origins, source=None):
    # Origins are a separate, GUID-keyed sidecar, never fields in scenario data.
    # Agent identity is concrete and validated before any field substitution.
    return (agent_origins or {}).get(agent["guid"], (source, f"/agents/{index}"))


def validate(doc, *, check_schedules=True, allow_templates=False, source=None, agent_origins=None,
             identity_origins=None):
    """Validate the internal GUID-based scenario representation.

    Loaders supply occurrence-indexed identity_origins before sorting. These are
    separate from GUID-keyed field origins: a duplicate belongs to its repeated
    occurrence, and an invalid GUID need not even be hashable. Internal/final
    graphs without original occurrence provenance retain generic identity errors.
    """
    json_value(doc)
    mapping(doc, "scenario")
    if type(doc.get("schema_version")) is not int or doc["schema_version"] != 1:
        raise KitError("unsupported schema_version (only integer 1 is supported)")
    nonempty(doc.get("name"), "scenario.name")
    guid_value(doc.get("guid"), "scenario.guid")
    agents = doc.get("agents")
    if type(agents) is not list or len(agents) > 1000:
        raise KitError("agents must be a list of at most 1000 agents")
    seen = set()
    for index, agent in enumerate(agents):
        context = f"agents[{index}]"
        mapping(agent, context)
        identity_site = identity_origins[index] if identity_origins is not None else None
        try:
            guid_value(agent.get("guid"), "agent guid" if identity_site else f"{context}.guid")
        except KitError as exc:
            if identity_site is None:
                raise
            raise AgentValidationError(identity_site[1] + "/guid", str(exc),
                                       source=identity_site[0]) from None
        if agent["guid"] in seen:
            if identity_site is not None:
                raise AgentValidationError(identity_site[1] + "/guid", "duplicate agent guid",
                                           source=identity_site[0])
            raise KitError(f"duplicate agent guid: {agent['guid']}")
        seen.add(agent["guid"])
        agent_source, pointer = agent_location(agent, index, agent_origins, source)

        def require(condition, field, reason):
            if not condition:
                raise AgentValidationError(pointer + pointer_path([field]), reason, source=agent_source)

        for field in ("name", "type"):
            require(type(agent.get(field)) is str and bool(agent[field].strip()),
                    field, "must be a nonempty string")
        is_template = lambda value: allow_templates and type(value) is str and PLACEHOLDER.fullmatch(value)
        if not is_template(agent.get("options")):
            require(type(agent.get("options")) is dict, "options", "must be a mapping")
        if type(agent.get("disabled")) is not bool and not is_template(agent.get("disabled")):
            require(False, "disabled", "must be a boolean")
        if "propagate_immediately" in agent and type(agent["propagate_immediately"]) is not bool and not is_template(agent["propagate_immediately"]):
            require(False, "propagate_immediately", "must be a boolean")
        if "keep_events_for" in agent and not is_template(agent["keep_events_for"]) and (type(agent["keep_events_for"]) is not int or agent["keep_events_for"] < 0):
            require(False, "keep_events_for", "must be a nonnegative integer (seconds)")
        if agent.get("schedule") is not None and type(agent["schedule"]) is not str:
            require(False, "schedule", "must be a string or null")
        if check_schedules and agent.get("schedule") is not None:
            require(agent["schedule"] in SCHEDULES, "schedule", "unknown schedule")
    for kind, fields in EDGE_FIELDS.items():
        links = reference_links(doc, kind, fields, source)
        unique = set()
        for index, link in enumerate(links):
            for field in fields:
                if type(link[field]) is not str or link[field] not in seen:
                    raise ReferenceValidationError(f"/{kind}/{index}/{field}", "dangling guid",
                                                   source=source)
            pair = tuple(link[f] for f in fields)
            if pair in unique:
                raise ReferenceValidationError(f"/{kind}/{index}", "duplicate edge", source=source)
            unique.add(pair)
    return doc


def canonical(doc):
    result = deepcopy(doc)
    result.pop("exported_at", None)
    result["agents"].sort(key=lambda a: a["guid"])
    for kind, fields in EDGE_FIELDS.items():
        result[kind].sort(key=lambda edge: tuple(edge[f] for f in fields))
    return result


def from_export(raw, *, check_schedules=True, allow_templates=False, source=None, agent_origins=None):
    mapping(raw, "scenario")
    doc = deepcopy(raw)
    agents = doc.get("agents")
    if type(agents) is not list or len(agents) > 1000:
        raise KitError("agents must be a list of at most 1000 agents")
    origins = {}
    for index, agent in enumerate(agents):
        if type(agent) is dict and type(agent.get("guid")) is str:
            origins.setdefault(agent["guid"], (Path(source).absolute() if source is not None else None,
                                                f"/agents/{index}"))
    for kind, fields in EDGE_FIELDS.items():
        doc.setdefault(kind, [])
        links = reference_links(doc, kind, fields, source)
        for index, link in enumerate(links):
            for field in fields:
                value = link[field]
                if type(value) is not int or not 0 <= value < len(agents):
                    raise ReferenceValidationError(f"/{kind}/{index}/{field}",
                                                   "dangling or non-integer link index", source=source)
                agent = mapping(agents[value], f"agents[{value}]")
                link[field] = agent.get("guid")
    validate(doc, check_schedules=check_schedules, allow_templates=allow_templates, source=source,
             agent_origins=origins,
             identity_origins=[(source, f"/agents/{index}") for index in range(len(agents))])
    if agent_origins is not None:
        agent_origins.clear()
        agent_origins.update(origins)
    return canonical(doc)


def to_export(doc, *, check_schedules=True, agent_origins=None):
    validate(doc, check_schedules=check_schedules, agent_origins=agent_origins)
    result = canonical(doc)
    indices = {agent["guid"]: i for i, agent in enumerate(result["agents"])}
    for kind, fields in EDGE_FIELDS.items():
        result[kind] = [{field: indices[edge[field]] for field in fields}
                        for edge in result[kind]]
    return result


def load_scenario(path: Path, *, check_schedules=True, allow_templates=False, agent_origins=None):
    """Load canonical data; optionally fill a GUID -> (source, pointer) sidecar.

    Pass the same sidecar to render/to_export/validate to retain input locations
    after canonical sorting or copying. It is scoped to this loaded scenario.
    """
    check_path(path)
    if not path.is_dir():
        return from_export(load_data(path, json_only=True), check_schedules=check_schedules,
                           allow_templates=allow_templates, source=path, agent_origins=agent_origins)
    manifest = mapping(load_data(path / "scenario.yaml"), "scenario.yaml")
    if set(manifest) != {"kit_version", "scenario", "links", "control_links"}:
        raise KitError("scenario.yaml must contain exactly kit_version, scenario, links, control_links")
    if type(manifest["kit_version"]) is not int or manifest["kit_version"] != 1:
        raise KitError("unsupported kit_version (only integer 1 is supported)")
    doc = deepcopy(mapping(manifest["scenario"], "scenario.yaml scenario"))
    if set(doc) & {"agents", "links", "control_links", "exported_at"}:
        raise KitError("scenario metadata contains reserved fields")
    agents_path = path / "agents"
    check_path(agents_path)
    if not agents_path.is_dir():
        raise KitError("kit must contain an agents directory")
    entries = sorted(agents_path.iterdir())
    if len(entries) > 1000:
        raise KitError("kit has more than 1000 agent files")
    total = (path / "scenario.yaml").stat().st_size
    for entry in entries:
        check_path(entry)
        if not entry.is_file() or entry.suffix != ".yaml":
            raise KitError("agents directory may contain only regular .yaml files")
        total += entry.stat().st_size
    if total > MAX_KIT_BYTES:
        raise KitError("kit exceeds 20 MiB total limit")
    doc["agents"] = [load_data(entry) for entry in entries]
    origins = {}
    for entry, agent in zip(entries, doc["agents"]):
        if type(agent) is dict and type(agent.get("guid")) is str:
            origins.setdefault(agent["guid"], (entry.absolute(), ""))
    for kind in EDGE_FIELDS:
        doc[kind] = manifest[kind]
    validate(doc, check_schedules=check_schedules, allow_templates=allow_templates,
             source=path / "scenario.yaml", agent_origins=origins,
             identity_origins=[(entry, "") for entry in entries])
    if agent_origins is not None:
        agent_origins.clear()
        agent_origins.update(origins)
    return canonical(doc)


def split_files(doc):
    validate(doc)
    doc = canonical(doc)
    metadata = {k: v for k, v in doc.items() if k not in {"agents", *EDGE_FIELDS}}
    manifest = {"kit_version": 1, "scenario": metadata,
                **{kind: doc[kind] for kind in EDGE_FIELDS}}
    files = {"scenario.yaml": yaml_text(manifest)}
    slugs = {a["guid"]: (re.sub(r"[^a-z0-9]+", "-", a["name"].lower()).strip("-")[:64] or "agent")
             for a in doc["agents"]}
    # Extend the GUID prefix only when names and first eight GUID chars collide.
    for agent in doc["agents"]:
        guid = agent["guid"]
        length = min(8, len(guid))
        while any(other != guid and slugs[other] == slugs[guid]
                  and other[:length] == guid[:length] for other in slugs):
            length += 1
            if length > len(guid):
                # One GUID is a prefix of another: use full GUID (the longer one's
                # next character distinguishes it, so no collision remains).
                break
        filename = f"agents/{slugs[guid]}--{guid[:length]}.yaml"
        if filename in files:
            raise KitError("agent filename collision")
        files[filename] = yaml_text(agent)
    sizes = [len(content.encode("utf-8")) for content in files.values()]
    if any(size > MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_KIT_BYTES:
        raise KitError("generated YAML exceeds 5 MiB per file / 20 MiB kit limits")
    return files


def string_sites(value, path=(), *, key_error=None):
    if type(value) is str:
        yield path, value
    elif type(value) is list:
        for index, item in enumerate(value):
            yield from string_sites(item, path + (index,), key_error=key_error)
    elif type(value) is dict:
        for key, item in value.items():
            if "${HSK:" in key:
                reason = "placeholders in mapping keys are not supported"
                if key_error is not None:
                    # Locate the containing mapping, never expose the key text.
                    key_error(path, reason)
                raise KitError(reason)
            yield from string_sites(item, path + (key,), key_error=key_error)


def strings(value):
    for _, item in string_sites(value):
        yield item


def required_variables(doc, *, malformed_error=None, key_error=None):
    variables = set()
    for path, value in string_sites(doc, key_error=key_error):
        variables.update(PLACEHOLDER.findall(value))
        if "${HSK:" in PLACEHOLDER.sub("", value):
            reason = "malformed placeholder; use ${HSK:NAME}"
            if malformed_error is not None:
                malformed_error(path, reason)
            raise KitError(reason)
    return sorted(variables)


def _template_error(doc, path, reason, *, agent_origins=None, generic_reason=None):
    if len(path) >= 2 and path[0] == "agents" and type(path[1]) is int:
        agent = doc["agents"][path[1]]
        if type(agent) is dict and type(agent.get("guid")) is str:
            source, pointer = agent_location(agent, path[1], agent_origins)
            raise AgentValidationError(pointer + pointer_path(path[2:]), reason, source=source)
    raise KitError(generic_reason or reason)


def extract(doc, *, agent_origins=None):
    """Inventory static names; carry loader origins to locate agent syntax errors."""
    def fail(path, reason):
        _template_error(doc, path, reason, agent_origins=agent_origins)

    variables = required_variables(doc, malformed_error=fail, key_error=fail)
    credentials = set()
    for value in strings(doc):
        for match in CREDENTIAL_TAG.finditer(value):
            name = match.group(1).strip()
            if len(name) >= 2 and name[0] in "\"'" and name[-1] == name[0]:
                name = name[1:-1]
            if not CREDENTIAL_NAME.fullmatch(name):
                raise KitError("dynamic or complex credential names are unsupported; use a static name")
            credentials.add(name)
    return {"variables": sorted(variables), "credentials": sorted(credentials)}


def substitute(doc, variables, *, agent_origins=None, reject_unused_vars=False, template_error=None):
    """Bounded single-pass substitution; an optional error callback locates templates."""
    json_value(doc)
    mapping(variables, "vars")
    json_value(variables)

    def fail(path, reason, *, generic_reason=None):
        if template_error is not None:
            template_error(path, reason)
        _template_error(doc, path, reason, agent_origins=agent_origins, generic_reason=generic_reason)

    needed = required_variables(doc, malformed_error=fail, key_error=fail)
    missing = sorted(set(needed) - variables.keys())
    if missing:
        raise KitError("missing variables: " + ", ".join(missing))
    if reject_unused_vars:
        # Only original template references count. Never scan supplied values or
        # echo invalid keys, which may contain arbitrary text or sensitive data.
        if any(not re.fullmatch(VARIABLE_NAME, name) for name in variables):
            raise KitError("variable names must match " + VARIABLE_NAME)
        unused = sorted(variables.keys() - set(needed))
        if unused:
            raise KitError("unused variables: " + ", ".join(unused))

    def scalar_text(item, name, path):
        if type(item) in (list, dict):
            reason = "collection variable must occupy the whole value"
            fail(path, reason, generic_reason=f"{reason}: {name}")
        if item is None:
            return "null"
        if type(item) is bool:
            return "true" if item else "false"
        return str(item)

    # Budget the expanded compact JSON before allocating replacements. A small
    # template may repeat a multi-MiB variable many times; validating afterwards
    # would be too late. Variable contents are inspected as data, never expanded.
    remaining = [MAX_FILE_BYTES, MAX_NODES]

    def charge_bytes(size):
        remaining[0] -= size
        if remaining[0] < 0:
            raise KitError("rendered data exceeds 5 MiB expansion limit")

    def scalar_size(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def preview(value, depth=0, substitute=True, path=()):
        remaining[1] -= 1
        if depth > MAX_DEPTH or remaining[1] < 0:
            raise KitError("rendered data exceeds depth/node limits (64 / 100000)")
        if type(value) is dict:
            charge_bytes(2 + max(0, len(value) - 1))
            for key, item in value.items():
                charge_bytes(scalar_size(key) + 1)
                preview(item, depth + 1, substitute, path + (key,))
        elif type(value) is list:
            charge_bytes(2 + max(0, len(value) - 1))
            for index, item in enumerate(value):
                preview(item, depth + 1, substitute, path + (index,))
        elif type(value) is str and substitute:
            whole = PLACEHOLDER.fullmatch(value)
            if whole:
                remaining[1] += 1  # The replacement occupies this same node.
                preview(variables[whole.group(1)], depth, False, path)
                return
            charge_bytes(2)  # JSON string quotes, with escaped fragment sizes.
            start = 0
            for match in PLACEHOLDER.finditer(value):
                charge_bytes(scalar_size(value[start:match.start()]) - 2)
                charge_bytes(scalar_size(scalar_text(variables[match.group(1)], match.group(1), path)) - 2)
                start = match.end()
            charge_bytes(scalar_size(value[start:]) - 2)
        else:
            charge_bytes(scalar_size(value))

    preview(doc)

    def check_replacement(value, path):
        # Inspect replacement data without expanding it. Any nested error belongs
        # to this original string site, never to a node introduced by vars.
        try:
            introduced = required_variables(value)
        except KitError as exc:
            fail(path, str(exc))
        if introduced:
            fail(path, "variable values may not introduce placeholders (rendering is single-pass)")
        return value

    def replace(value, path=()):
        if type(value) is dict:
            return {key: replace(item, path + (key,)) for key, item in value.items()}
        if type(value) is list:
            return [replace(item, path + (index,)) for index, item in enumerate(value)]
        if type(value) is not str:
            return value
        whole = PLACEHOLDER.fullmatch(value)
        if whole:
            return deepcopy(check_replacement(variables[whole.group(1)], path))

        def embedded(match):
            return scalar_text(variables[match.group(1)], match.group(1), path)
        return check_replacement(PLACEHOLDER.sub(embedded, value), path)

    return replace(doc)


def render(doc, variables, *, agent_origins=None, reject_unused_vars=False):
    """Render once; optionally reject bindings absent from original references."""
    result = substitute(doc, variables, agent_origins=agent_origins,
                        reject_unused_vars=reject_unused_vars)
    validate(result, agent_origins=agent_origins)
    return canonical(result)


def changed_paths(before, after, path=""):
    if type(before) is dict and type(after) is dict:
        changes = []
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before:
                changes.append({"path": child, "change": "added"})
            elif key not in after:
                changes.append({"path": child, "change": "removed"})
            else:
                changes.extend(changed_paths(before[key], after[key], child))
        return changes
    if type(before) is list and type(after) is list:
        changes = []
        for index in range(max(len(before), len(after))):
            child = path + f"/{index}"
            if index >= len(before):
                changes.append({"path": child, "change": "added"})
            elif index >= len(after):
                changes.append({"path": child, "change": "removed"})
            else:
                changes.extend(changed_paths(before[index], after[index], child))
        return changes
    return [] if type(before) is type(after) and before == after else [{"path": path, "change": "changed"}]


def semantic_diff(before, after):
    before, after = canonical(before), canonical(after)
    old = {a["guid"]: a for a in before["agents"]}
    new = {a["guid"]: a for a in after["agents"]}
    changed = {}
    for guid in sorted(old.keys() & new.keys()):
        paths = changed_paths(old[guid], new[guid])
        if paths:
            changed[guid] = paths
    metadata = lambda doc: {k: v for k, v in doc.items() if k not in {"agents", *EDGE_FIELDS}}
    result = {"agents": {"added": sorted(new.keys() - old.keys()),
                          "removed": sorted(old.keys() - new.keys()), "changed": changed},
              "scenario": changed_paths(metadata(before), metadata(after))}
    for kind, fields in EDGE_FIELDS.items():
        left = {tuple(e[f] for f in fields) for e in before[kind]}
        right = {tuple(e[f] for f in fields) for e in after[kind]}
        result[kind] = {"added": [dict(zip(fields, edge)) for edge in sorted(right - left)],
                        "removed": [dict(zip(fields, edge)) for edge in sorted(left - right)]}
    result["different"] = bool(changed or result["agents"]["added"] or result["agents"]["removed"]
                               or result["scenario"] or any(result[k][c] for k in EDGE_FIELDS for c in ("added", "removed")))
    return result


def cycle_components(vertices, edges):
    """Iterative Kosaraju: report actual SCCs, excluding downstream noncycles."""
    graph = {v: set() for v in vertices}
    reverse = {v: set() for v in vertices}
    for a, b in edges:
        graph[a].add(b)
        reverse[b].add(a)
    seen, order = set(), []
    for root in sorted(vertices):
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            vertex, finished = stack.pop()
            if finished:
                order.append(vertex)
            elif vertex not in seen:
                seen.add(vertex)
                stack.append((vertex, True))
                stack.extend((n, False) for n in sorted(graph[vertex], reverse=True) if n not in seen)
    seen, components = set(), []
    for root in reversed(order):
        if root in seen:
            continue
        stack, component = [root], []
        while stack:
            vertex = stack.pop()
            if vertex not in seen:
                seen.add(vertex)
                component.append(vertex)
                stack.extend(reverse[vertex] - seen)
        if len(component) > 1 or root in graph[root]:
            components.append(sorted(component))
    return sorted(components)


def lint(doc):
    validate(doc, check_schedules=False)
    vertices = {a["guid"] for a in doc["agents"]}
    connected, issues = set(), []
    for guid in sorted(unknown_schedules(doc)):
        issues.append({"code": "unknown-schedule", "severity": "error", "guid": guid})
    for kind, fields in EDGE_FIELDS.items():
        edges = [tuple(edge[f] for f in fields) for edge in doc[kind]]
        connected.update(v for edge in edges for v in edge)
        for component in cycle_components(vertices, edges):
            issues.append({"code": "link-cycle", "severity": "warning", "kind": kind, "guids": component})
    for guid in sorted(vertices - connected):
        issues.append({"code": "orphan-agent", "severity": "warning", "guid": guid})
    return {"issues": issues, "ok": not issues}
