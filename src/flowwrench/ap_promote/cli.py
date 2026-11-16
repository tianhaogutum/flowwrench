"""Command line entry point. All commands operate on local files only."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core import Flow, check_references, connection_catalog, mappings, pieces, remap, scrub
from .jsondoc import Document, InvalidInput, Refused, atomic_write
from .promotion import apply_plan, create_plan
from .roundtrip import verify_roundtrip


class _StoreOnce(argparse.Action):
    """Require one nonempty path before a command can read or write files."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise argparse.ArgumentError(self, 'may only be supplied once')
        if values == '':
            raise argparse.ArgumentError(self, 'expected nonempty path')
        setattr(namespace, self.dest, values)


def _nonempty_path(value):
    if value == '':
        raise argparse.ArgumentTypeError('expected nonempty path')
    return value


def _nonempty_mapping(value):
    if value == '':
        raise argparse.ArgumentTypeError('expected nonempty mapping')
    return value


def _nonempty_pairing(value):
    if value == '':
        raise argparse.ArgumentTypeError('expected nonempty pairing')
    return value


def parser():
    p = argparse.ArgumentParser(prog='flowwrench ap-promote', description=__doc__)
    p.add_argument('--version', action='version', version=__version__)
    commands = p.add_subparsers(dest='command', required=True)
    inv = commands.add_parser('inventory', help='list all connection references')
    inv.add_argument('files', nargs='+', type=_nonempty_path)
    inv.add_argument('--json', action='store_true')
    verify = commands.add_parser('verify-roundtrip', help='verify re-exported references against source identities')
    verify.add_argument('source', type=_nonempty_path)
    verify.add_argument('destination', type=_nonempty_path)
    verify.add_argument('--map', action='append', type=_nonempty_mapping, default=[], metavar='OLD=NEW')
    verify.add_argument('--map-file', action='append', type=_nonempty_path, default=[],
                        help='mapping file with a nonempty literal path; repeat to merge in order')
    verify.add_argument('--flow-pair', action='append', type=_nonempty_pairing,
                        metavar='SOURCE_INDEX=DESTINATION_INDEX',
                        help='nonempty explicit zero-based pairing; repeat to cover every flow in both exports')
    verify.add_argument('--json', action='store_true')
    rem = commands.add_parser('remap', help='rewrite literal connection references with minimal diffs')
    rem.add_argument('file', type=_nonempty_path)
    rem.add_argument('--map', action='append', type=_nonempty_mapping, default=[], metavar='OLD=NEW')
    rem.add_argument('--map-file', action='append', type=_nonempty_path, default=[],
                     help='mapping file with a nonempty literal path; repeat to merge in order')
    rem.add_argument('-o', '--output', action=_StoreOnce, required=True)
    rem.add_argument('--strict', action='store_true')
    rem.add_argument('--dry-run', action='store_true')
    rem.add_argument('--target-connections', action=_StoreOnce,
                     help='one catalog to verify destination existence, status and piece')
    rem.add_argument('--json', action='store_true')
    for name, help_text in (('plan', 'save a hashed promotion plan with exact connection-version checks'),
                            ('apply', 'apply a reviewed plan after revalidating current inputs')):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('file', action=_StoreOnce if name == 'apply' else 'store',
                             type=_nonempty_path if name == 'plan' else None,
                             help='source flow' if name == 'plan' else 'nonempty reviewed plan path')
        if name == 'apply':
            command.add_argument('--source', action=_StoreOnce, required=True,
                                 help='one current source flow; paths are never read from plans')
            command.add_argument('--dry-run', action='store_true')
        command.add_argument('--map', action='append', type=_nonempty_mapping, default=[], metavar='OLD=NEW')
        command.add_argument('--map-file', action='append', type=_nonempty_path, default=[],
                             help='mapping file with a nonempty literal path; repeat to merge in order')
        command.add_argument('--target-connections', action=_StoreOnce, required=True,
                             help='one complete catalog with exact pieceVersion metadata')
        command.add_argument('--target-pieces', action=_StoreOnce,
                             help='one catalog of exact installed versions for every step/tool piece usage')
        command.add_argument('-o', '--output', action=_StoreOnce, required=True)
        command.add_argument('--json', action='store_true')
    check = commands.add_parser('check', help='validate references against one target environment')
    check.add_argument('file', type=_nonempty_path)
    check.add_argument('--target-connections', action=_StoreOnce, required=True)
    check.add_argument('--json', action='store_true')
    pc = commands.add_parser('pieces', help='report exact version pins and target gaps')
    pc.add_argument('file', type=_nonempty_path)
    pc.add_argument('--target-pieces', action=_StoreOnce, help='one complete installed piece catalog')
    pc.add_argument('--json', action='store_true')
    sc = commands.add_parser('scrub', help='remove step settings.sampleData test-run metadata')
    sc.add_argument('file', type=_nonempty_path)
    output = sc.add_mutually_exclusive_group()
    output.add_argument('-o', '--output', action=_StoreOnce)
    output.add_argument('--in-place', action='store_true')
    sc.add_argument('--dry-run', action='store_true')
    return p


def dump(value, stream=sys.stdout):
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def table(records, fields):
    print('\t'.join(fields))
    for record in records:
        print('\t'.join(json.dumps(record.get(f), ensure_ascii=False) for f in fields))


def run(args):
    if args.command == 'verify-roundtrip':
        if not args.map and not args.map_file:
            raise InvalidInput('verify-roundtrip requires --map or --map-file (an empty object is allowed)')
        source, destination = Flow.load(args.source), Flow.load(args.destination)
        mapping = mappings(args.map, [Document.load(f) for f in args.map_file])
        report = verify_roundtrip(source, destination, mapping, args.flow_pair)
        if args.json:
            dump(report)
        else:
            if args.flow_pair is not None:
                table(report['flowPairs'], ('sourceFlowIndex', 'destinationFlowIndex', 'sourceFlowPointer', 'destinationFlowPointer'))
            table(report['errors'], ('problem', 'step', 'occurrence', 'sourcePointer', 'destinationPointer'))
            print(f"{report['verified']}/{report['sourceReferences']} source references verified; "
                  f"{len(report['errors'])} errors")
        return int(not report['ok'])
    if args.command == 'inventory':
        records = []
        # Load every file before printing, so a bad second file cannot look like success.
        for file in args.files:
            records.extend(dict(file=file, **r.record()) for r in Flow.load(file).references)
        if args.json:
            dump(records)
        else:
            table(records, ('file', 'step', 'pieceName', 'pieceVersion', 'pointer', 'externalId', 'kind'))
        return 0
    if args.command in {'plan', 'apply'}:
        if not args.map and not args.map_file:
            raise InvalidInput('plan/apply requires --map or --map-file (an empty object is allowed)')
        flow = Flow.load(args.source if args.command == 'apply' else args.file)
        mapping = mappings(args.map, [Document.load(f) for f in args.map_file])
        target = Document.load(args.target_connections)
        target_pieces = Document.load(args.target_pieces) if args.target_pieces else None
        protected = (args.file, *args.map_file, args.target_connections,
                     *((args.target_pieces,) if args.target_pieces else ()))
        if args.command == 'plan':
            _, report = create_plan(flow, mapping, target, target_pieces)
            plan_text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
            # A large flow can produce a larger artifact. Never save a plan
            # that apply cannot read within the same byte/node/depth limits.
            Document(plan_text)
            atomic_write(args.output, plan_text, protected)
        else:
            output, report = apply_plan(Document.load(args.file), flow, mapping, target, target_pieces)
            report['dryRun'] = args.dry_run
            if not args.dry_run:
                atomic_write(args.output, output, (*protected, args.source))
        if args.json:
            dump(report)
        elif args.command == 'plan':
            print(f"Plan saved: {report['report']['rewritten']} references to rewrite; exact connection versions verified.")
        else:
            print(f"{report['rewritten']} references rewritten; reviewed plan verified; dry-run={args.dry_run}")
        return 0
    flow = Flow.load(args.file)
    if args.command == 'remap':
        if not args.map and not args.map_file:
            raise InvalidInput('remap requires --map or --map-file (an empty object is allowed)')
        mapping = mappings(args.map, [Document.load(f) for f in args.map_file])
        target = connection_catalog(Document.load(args.target_connections)) if args.target_connections else None
        text, report = remap(flow, mapping, args.strict, target)
        report['dryRun'] = args.dry_run
        if not args.dry_run:
            protected = (args.file, *args.map_file, *((args.target_connections,) if args.target_connections else ()))
            atomic_write(args.output, text, protected)
        if args.json:
            dump(report)
        else:
            print(f"{report['rewritten']} references rewritten; {len(report['unmapped'])} unmapped; "
                  f"{report['alreadyMapped']} already mapped; dry-run={args.dry_run}")
            if target is None:
                print('No target catalog: piece consistency checked locally; destination existence/piece unverified.', file=sys.stderr)
        return 0
    if args.command == 'check':
        errors = check_references(flow, connection_catalog(Document.load(args.target_connections)))
        report = dict(references=len(flow.references), errors=errors, ok=not errors)
        if args.json:
            dump(report)
        else:
            table(errors, ('step', 'pointer', 'externalId', 'problem', 'pieceName', 'targetPiece'))
            print(f"{len(flow.references)} references checked; {len(errors)} errors")
        return int(bool(errors))
    if args.command == 'pieces':
        records = pieces(flow, Document.load(args.target_pieces) if args.target_pieces else None)
        if args.json:
            dump(records)
        else:
            table(records, ('step', 'kind', 'pieceName', 'pieceVersion', 'status', 'pointer'))
        return int(any(r['status'] in {'unpinned', 'missing-version'} for r in records))
    if args.command == 'scrub':
        text, removed = scrub(flow)
        if not args.dry_run:
            if args.in_place:
                atomic_write(args.file, text)
            elif args.output:
                atomic_write(args.output, text, (args.file,))
            else:
                sys.stdout.write(text)
        dump(dict(removed=removed, count=len(removed), dryRun=args.dry_run), sys.stderr)
        return 0
    raise InvalidInput('unknown command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except Refused as e:
        print(f'refused: {e}', file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 2
    except (InvalidInput, OSError, UnicodeError, RecursionError) as e:
        message = str(e) if isinstance(e, InvalidInput) else type(e).__name__
        print(f'error: {message}', file=sys.stderr)
        return 2
