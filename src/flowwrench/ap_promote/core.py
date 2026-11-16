"""Offline Activepieces connection and piece analysis."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .jsondoc import Document, InvalidInput, Node, Refused, pointer, walk

TEMPLATE = re.compile(r'''\{\{\s*connections\s*\[\s*(?P<quote>['"])(?P<id>[^'"\\\r\n]+)(?P=quote)\s*\]\s*\}\}''')
BLOCK = re.compile(r'\{\{.*?(?:\}\}|$)', re.DOTALL)
_NUM = r'(?:0|[1-9][0-9]*)'
_PRE = r'(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
PIN = re.compile(rf'{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z')
MAX_REFERENCES = 50_000
STEP_TYPES = {'PIECE', 'PIECE_TRIGGER', 'EMPTY', 'CODE', 'LOOP_ON_ITEMS',
              'PROCESS_IN_BATCHES', 'ROUTER', 'AI_ROUTER'}


def valid_id(value: str):
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(c in "'\"\\[]{}" or c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise InvalidInput("connection externalId must be 1..512 characters without whitespace, quotes, brackets, braces or backslashes")
    return value


def obj(n: Node | None, where: tuple) -> Node:
    if n is None or not isinstance(n.value, dict):
        raise InvalidInput(f"expected object at {pointer(where) or '/'}")
    return n


@dataclass(frozen=True)
class Context:
    path: tuple
    flow: str
    step: str
    piece: str | None
    version: str | None
    kind: str

    def record(self):
        return dict(flow=self.flow, step=self.step, pieceName=self.piece,
                    pieceVersion=self.version, kind=self.kind, pointer=pointer(self.path))


@dataclass
class Reference:
    path: tuple
    node: Node
    external_id: str
    start: int
    end: int
    context: Context | None
    kind: str
    occurrence: int

    def record(self):
        c = self.context
        return dict(step=c.step if c else None, flow=c.flow if c else None,
                    pieceName=c.piece if c else None, pieceVersion=c.version if c else None,
                    pointer=pointer(self.path), externalId=self.external_id,
                    kind=self.kind, occurrence=self.occurrence)


class Flow:
    def __init__(self, document: Document):
        self.document = document
        self.contexts: dict[tuple, Context] = {}
        self.pieces: list[Context] = []
        self.steps: list[tuple[tuple, Node]] = []
        self.auth_paths: set[tuple] = set()
        root = obj(document.root, ())
        forms = [k for k in ('trigger', 'flows', 'template', 'version') if root.get(k) is not None]
        if len(forms) != 1:
            raise InvalidInput("unrecognised or ambiguous export shape; expected trigger, flows, template or version")
        form = forms[0]
        if form == 'trigger':
            versions = [((), root)]
        elif form == 'flows':
            array = root.get(form)
            if not isinstance(array.value, list) or not array.value:
                raise InvalidInput("flows must be a nonempty array")
            versions = [(('flows', i), n) for i, n in enumerate(array.value)]
        else:
            versions = [((form,), root.get(form))]
        for path, version in versions:
            obj(version, path)
            agent_ids = version.get('agentIds')
            if agent_ids is not None and agent_ids.value != []:
                raise InvalidInput('saved agentIds are not embedded tools and cannot be audited offline')
            names: set[str] = set()
            self._step(version.get('trigger'), path + ('trigger',), pointer(path) or '/', names, True)
        self.references = self._references()

    @classmethod
    def load(cls, path):
        return cls(Document.load(path))

    def _context(self, path, metadata, step, flow, kind):
        obj(metadata, path)
        piece = metadata.text('pieceName')
        version = metadata.text('pieceVersion', required=False)
        c = Context(path, flow, step, piece, version, kind)
        self.contexts[path] = c
        self.pieces.append(c)
        return c

    def _step(self, n, path, flow, names, trigger=False):
        obj(n, path)
        name, typ = n.text('name'), n.text('type')
        if name in names:
            raise InvalidInput(f"duplicate step name at {pointer(path)}")
        names.add(name)
        if typ not in STEP_TYPES or (trigger and typ not in {'PIECE_TRIGGER', 'EMPTY'}) or (not trigger and typ in {'PIECE_TRIGGER', 'EMPTY'}):
            raise InvalidInput(f"unsupported step type at {pointer(path)}")
        settings = obj(n.get('settings'), path + ('settings',))
        self.steps.append((path, n))
        context = Context(path, flow, name, None, None, 'step')
        if typ in {'PIECE', 'PIECE_TRIGGER'}:
            # The entire step has its owning piece context, not arbitrary input objects.
            context = self._context(path, settings, name, flow, 'step')
            inputs = obj(settings.get('input'), path + ('settings', 'input'))
            self.auth_paths.add(path + ('settings', 'input', 'auth'))
            agent_id = inputs.get('agentId')
            if (context.piece == '@activepieces/piece-ai' and settings.text('actionName', required=False) == 'run_agent'
                    and agent_id is not None and agent_id.value not in (None, '')):
                raise InvalidInput('saved run_agent agentId cannot be audited offline; only embedded tools are supported')
        self.contexts[path] = context
        # Agent tools can be embedded within piece inputs. Their own piece wins.
        inputs = settings.get('input')
        if inputs is not None:
            for rel, child in walk(inputs):
                if not isinstance(child.value, dict) or child.get('agentTools') is None:
                    continue
                tools_path = path + ('settings', 'input') + rel + ('agentTools',)
                tools = child.get('agentTools')
                if not isinstance(tools.value, list):
                    raise InvalidInput(f"agentTools must be an array at {pointer(tools_path)}")
                for i, tool in enumerate(tools.value):
                    tp = tools_path + (i,)
                    obj(tool, tp)
                    tool_type = tool.text('type')
                    if tool_type not in {'PIECE', 'FLOW', 'MCP', 'KNOWLEDGE_BASE'}:
                        raise InvalidInput(f'unsupported agent tool type at {pointer(tp)}')
                    meta = tool.get('pieceMetadata')
                    if tool_type == 'PIECE' and meta is not None:
                        self._context(tp, meta, name, flow, 'agent-tool')
                        self.auth_paths.add(tp + ('pieceMetadata', 'predefinedInput', 'auth'))
                    elif tool_type == 'PIECE':
                        raise InvalidInput(f"piece tool lacks pieceMetadata at {pointer(tp)}")
                    else:
                        if meta is not None and meta.value is not None:
                            raise InvalidInput(f'unexpected pieceMetadata on non-piece tool at {pointer(tp)}')
                        self.contexts[tp] = Context(tp, flow, name, None, None, 'agent-tool')
        for field in ('nextAction', 'firstLoopAction'):
            child = n.get(field)
            if child is not None and child.value is not None:
                if field == 'firstLoopAction' and typ not in {'LOOP_ON_ITEMS', 'PROCESS_IN_BATCHES'}:
                    raise InvalidInput(f"unexpected firstLoopAction at {pointer(path)}")
                self._step(child, path + (field,), flow, names)
        children = n.get('children')
        if children is not None:
            if typ not in {'ROUTER', 'AI_ROUTER'} or not isinstance(children.value, list):
                raise InvalidInput(f"invalid router children at {pointer(path)}")
            for i, child in enumerate(children.value):
                if child.value is not None:
                    self._step(child, path + ('children', i), flow, names)
        elif typ in {'ROUTER', 'AI_ROUTER'}:
            raise InvalidInput(f"router lacks children at {pointer(path)}")
        branches = n.get('continueOnFailureBranches')
        if branches is not None:
            obj(branches, path + ('continueOnFailureBranches',))
            if set(branches.value) - {'onSuccess', 'onFailure'}:
                raise InvalidInput("unknown continueOnFailureBranches member")
            for k, child in branches.value.items():
                if child.value is not None:
                    self._step(child, path + ('continueOnFailureBranches', k), flow, names)

    def _owner(self, path):
        for length in range(len(path), -1, -1):
            if path[:length] in self.contexts:
                return self.contexts[path[:length]]
        return None

    def _references(self):
        refs = []
        for path, n in walk(self.document.root):
            if isinstance(n.value, dict):
                if any('{{' in key and re.search(r'\bconnections\b', key) for key in n.value):
                    raise InvalidInput(f"connection references in object keys unsupported at {pointer(path)}")
            if not isinstance(n.value, str):
                if path in self.auth_paths and n.value is not None:
                    raise InvalidInput(f"inline/non-string auth unsupported at {pointer(path)}; use an externalId")
                continue
            matches = []
            for block in BLOCK.finditer(n.value):
                if re.search(r'\bconnections\b', block.group()):
                    match = TEMPLATE.fullmatch(block.group())
                    if not match:
                        raise InvalidInput(f"unsupported connection expression at {pointer(path)}")
                    if len(refs) + len(matches) >= MAX_REFERENCES:
                        raise InvalidInput('flow exceeds connection reference limit')
                    matches.append((block.start() + match.start('id'),
                                    block.start() + match.end('id'), valid_id(match.group('id'))))
            if path in self.auth_paths and matches and not TEMPLATE.fullmatch(n.value):
                raise InvalidInput(f"auth must be one connection reference at {pointer(path)}")
            if path in self.auth_paths and n.value and not matches:
                if '{{' in n.value or '}}' in n.value:
                    raise InvalidInput(f"dynamic auth unsupported at {pointer(path)}")
                matches.append((0, len(n.value), valid_id(n.value)))
            for occurrence, (start, end, external_id) in enumerate(matches):
                if len(refs) >= MAX_REFERENCES:
                    raise InvalidInput('flow exceeds connection reference limit')
                refs.append(Reference(path, n, external_id, start, end, self._owner(path),
                                      'template' if '{{' in n.value else 'literal', occurrence))
        return refs


def entries(document: Document, kind: str) -> list[Node]:
    root = document.root
    if isinstance(root.value, list):
        return root.value
    if isinstance(root.value, dict) and root.get('data') is not None:
        data = root.get('data')
        if not isinstance(data.value, list):
            raise InvalidInput(f"{kind} data must be an array")
        # A partial API page is not evidence of absence in the target.
        for k in ('next', 'nextCursor'):
            n = root.get(k)
            if n is not None and n.value not in (None, ''):
                raise InvalidInput(f"paginated {kind} catalog incomplete; combine all pages into an array")
        return data.value
    raise InvalidInput(f"{kind} catalog must be an array or an unpaginated data array")


def connection_catalog(document: Document) -> dict[str, dict]:
    result = {}
    for n in entries(document, 'connection'):
        obj(n, ())
        external_id = valid_id(n.text('externalId'))
        piece = n.text('pieceName')
        status = n.text('status', required=False) or 'ACTIVE'
        if status not in {'ACTIVE', 'MISSING', 'ERROR'}:
            raise InvalidInput("unknown connection status")
        if external_id in result:
            raise InvalidInput("duplicate connection externalId; provide one target environment")
        # Deliberately discard value, tokens, metadata and all other sensitive fields.
        result[external_id] = {'pieceName': piece, 'status': status}
    return result


def check_references(flow: Flow, catalog: dict) -> list[dict]:
    errors = []
    for ref in flow.references:
        target = catalog.get(ref.external_id)
        piece = ref.context.piece if ref.context else None
        code = None
        if target is None:
            code = 'missing'
        elif not piece:
            code = 'unknown-piece'
        elif piece != target['pieceName']:
            code = 'wrong-piece'
        elif target['status'] != 'ACTIVE':
            code = 'inactive'
        if code:
            errors.append(dict(ref.record(), problem=code,
                               targetPiece=target['pieceName'] if target else None))
    return errors


@dataclass(frozen=True)
class Mapping:
    external_id: str
    piece: str | None = None


def mappings(cli: list[str], documents: list[Document]) -> dict[str, Mapping]:
    result = {}

    def add(old, new):
        valid_id(old)
        valid_id(new.external_id)
        if old in result and result[old] != new:
            raise InvalidInput("conflicting duplicate mapping entries")
        result[old] = new

    for doc in documents:
        obj(doc.root, ())
        for old, value in doc.root.value.items():
            if isinstance(value.value, str):
                new = Mapping(value.value)
            else:
                obj(value, ())
                if set(value.value) - {'externalId', 'pieceName'}:
                    raise InvalidInput("unknown mapping fields; expected externalId and pieceName")
                new = Mapping(value.text('externalId'), value.text('pieceName'))
            add(old, new)
    for entry in cli:
        if '=' not in entry:
            raise InvalidInput("--map requires old=new")
        old, new = entry.split('=', 1)
        add(old, Mapping(new))
    for old, new in result.items():
        if new.external_id != old and new.external_id in result and result[new.external_id].external_id != new.external_id:
            raise InvalidInput("mapping chains/cycles are ambiguous; map directly to final destinations")
    return result


def remap(flow: Flow, mapping: dict[str, Mapping], strict=False, target=None):
    owners: dict[str, set[str]] = {}
    for ref in flow.references:
        if ref.context and ref.context.piece:
            owners.setdefault(ref.external_id, set()).add(ref.context.piece)
    destinations = {m.external_id for m in mapping.values()}
    # Include every destination's existing use and every source's owning piece.
    inferred = {k: set(v) for k, v in owners.items()}
    for old, new in mapping.items():
        inferred.setdefault(new.external_id, set()).update(owners.get(old, set()))
        if new.piece:
            inferred[new.external_id].add(new.piece)
    for old, new in mapping.items():
        if owners.get(old) and len(inferred[new.external_id]) > 1:
            raise Refused("piece guard: destination would mix different pieces")
    edits = []
    string_edits: dict[tuple, list[tuple[Reference, str]]] = {}
    rewritten = already = 0
    unmapped = []
    used = set()
    for ref in flow.references:
        new = mapping.get(ref.external_id)
        if new is None and ref.external_id in destinations:
            new = Mapping(ref.external_id)
            already += 1
        elif new is None:
            unmapped.append(ref.record())
            continue
        else:
            used.add(ref.external_id)
        piece = ref.context.piece if ref.context else None
        if not piece:
            raise Refused(f"piece guard: unknown owning piece at {pointer(ref.path)}")
        if new.piece is not None and new.piece != piece:
            raise Refused(f"piece guard: mapping piece differs at {pointer(ref.path)}")
        if len(inferred.get(new.external_id, set())) > 1 or (inferred.get(new.external_id) and piece not in inferred[new.external_id]):
            raise Refused(f"piece guard: incompatible destination at {pointer(ref.path)}")
        if target is not None:
            record = target.get(new.external_id)
            if record is None or record['pieceName'] != piece or record['status'] != 'ACTIVE':
                raise Refused(f"piece guard: destination missing, inactive or wrong-piece at {pointer(ref.path)}")
        if new.external_id != ref.external_id:
            string_edits.setdefault(ref.path, []).append((ref, new.external_id))
            rewritten += 1
    report = dict(references=len(flow.references), rewritten=rewritten, alreadyMapped=already,
                  unmapped=unmapped, unusedMappings=sorted(set(mapping) - used),
                  catalogVerified=target is not None)
    if strict and unmapped:
        raise Refused(f"{len(unmapped)} unmapped reference(s); strict mode wrote no output")
    for changes in string_edits.values():
        positions = {p for ref, _ in changes for p in (ref.start, ref.end)}
        bounds = flow.document.raw_positions(changes[0][0].node, positions)
        for ref, external_id in changes:
            escaped = json.dumps(external_id, ensure_ascii=False)[1:-1]
            edits.append((bounds[ref.start], bounds[ref.end], escaped))
    output = flow.document.edited(edits)
    # With a supplied catalog, validate ALL resulting references, including unmapped ones.
    if target is not None:
        errors = check_references(Flow(Document(output)), target)
        if errors:
            raise Refused(f"target check failed for {len(errors)} reference(s); wrote no output")
    return output, report


def _piece_catalog_field(node: Node, short: str, alias: str) -> str:
    # Validate both supplied fields before choosing either. In particular an
    # invalid/contradictory short alias cannot hide behind a valid long alias.
    keys = [key for key in (short, alias) if node.get(key) is not None]
    values = [node.text(key) for key in keys] if keys else [node.text(short)]
    if len(values) == 2 and values[0] != values[1]:
        raise InvalidInput(f"conflicting piece catalog aliases: {short} and {alias}")
    return values[0]


def piece_catalog(document: Document) -> set[tuple[str, str]]:
    """Consistent aliases and allowlisted pins; duplicate pairs have set semantics."""
    available = set()
    for n in entries(document, 'piece'):
        obj(n, ())
        name = _piece_catalog_field(n, 'name', 'pieceName')
        version = _piece_catalog_field(n, 'version', 'pieceVersion')
        if not PIN.fullmatch(version):
            raise InvalidInput("target piece versions must be exact x.y.z pins")
        available.add((name, version))
    return available


def piece_records(flow: Flow, available: set[tuple[str, str]] | None = None):
    records = []
    for ctx in flow.pieces:
        status = 'pinned'
        if ctx.version is None or not PIN.fullmatch(ctx.version):
            status = 'unpinned'
        elif available is not None:
            status = 'available' if (ctx.piece, ctx.version) in available else 'missing-version'
        records.append(dict(ctx.record(), status=status))
    return records


def pieces(flow: Flow, target: Document | None = None):
    return piece_records(flow, piece_catalog(target) if target is not None else None)


def scrub(flow: Flow):
    edits = []
    removed = []
    for path, step in flow.steps:
        settings = step.get('settings')
        sample = settings.get('sampleData')
        if sample is None:
            continue
        if sample.value is not None:
            obj(sample, path + ('settings', 'sampleData'))
            if set(sample.value) - {'sampleDataFileId', 'sampleDataInputFileId', 'lastTestDate'}:
                raise InvalidInput(f"unknown sampleData fields at {pointer(path)}; refusing destructive scrub")
        keys = list(settings.value)
        i = keys.index('sampleData')
        start, end = settings.keys['sampleData'], sample.end
        if i + 1 < len(keys):
            end = settings.keys[keys[i + 1]]
        elif i > 0:
            start = settings.value[keys[i - 1]].end
        edits.append((start, end, ''))
        removed.append(pointer(path + ('settings', 'sampleData')))
    return flow.document.edited(edits), removed
