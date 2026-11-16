"""Command-line interface; payload contents are never printed."""
import argparse
import json
import sqlite3
import sys

from . import __version__
from .artifacts import DEFAULT_ROW_HASH_MEMORY_BYTES, validate_max_content_bytes, validate_max_content_rows, validate_max_content_row_bytes, validate_max_content_value_bytes
from .core import analyze, compare, plan, reclaim, reclaim_plan, validate_max_members, validate_max_workflows, verify_copy
from .fixture import generate
from .storage import DietError


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be non-negative')
    return number


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


def row_hash_budget(value):
    number = int(value)
    if number < 32:
        raise argparse.ArgumentTypeError('row-hash memory bytes must be at least 32')
    return number


def content_value_limit(value):
    number = int(value)
    try:
        validate_max_content_value_bytes(number)
    except DietError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return number


def content_row_limit(value):
    try:
        number = int(value)
        validate_max_content_row_bytes(number)
    except (ValueError, DietError) as exc:
        raise argparse.ArgumentTypeError('max-content-row-bytes must be an integer from 0 to 9223372036854775807') from exc
    return number


def content_count_limit(value):
    try:
        number = int(value)
        validate_max_content_rows(number)
    except (ValueError, DietError) as exc:
        raise argparse.ArgumentTypeError('max-content-rows must be a non-negative integer') from exc
    return number


def content_byte_limit(value):
    try:
        number = int(value)
        validate_max_content_bytes(number)
    except (ValueError, DietError) as exc:
        raise argparse.ArgumentTypeError('max-content-bytes must be a non-negative integer') from exc
    return number


def workflow_limit(value):
    try:
        number = int(value)
        validate_max_workflows(number)
    except (ValueError, DietError) as exc:
        # argparse's default integer error echoes input; keep this diagnostic value-free.
        raise argparse.ArgumentTypeError('max_workflows must be a non-negative integer') from exc
    return number


def member_limit(value):
    try:
        number = int(value)
        validate_max_members(number)
    except (ValueError, DietError) as exc:
        raise argparse.ArgumentTypeError('max_members must be a non-negative integer') from exc
    return number


def parser():
    root = argparse.ArgumentParser(prog='flowwrench n8n-diet', description='Offline n8n SQLite execution storage accounting; never prunes the source.')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command', required=True)
    for name, help_text in [('analyze', 'measure page and execution storage'), ('plan', 'simulate retention without changing any records'), ('compare', 'compare two retention policies on one snapshot'), ('reclaim', 'VACUUM INTO a verified compact copy'), ('reclaim-plan', 'save a source-bound disk reclamation artifact'), ('verify-copy', 'check a published copy against a saved reclamation plan')]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('db', help='SQLite database file (use a stopped n8n backup)')
        sub.add_argument('--json', action='store_true', help='machine-readable report')
        if name != 'verify-copy':
            sub.add_argument('--force', action='store_true', help='allow committed WAL via private snapshot; never bypass locks')
        if name == 'analyze':
            sub.add_argument('--top', type=positive, default=10, help='largest execution count (all workflows are reported)')
            sub.add_argument('--no-dbstat', action='store_true', help='use explicitly labelled value-byte estimates')
        if name == 'plan':
            sub.add_argument('--max-age-days', type=nonnegative)
            sub.add_argument('--max-count', type=nonnegative)
            sub.add_argument('--keep-failed', action='store_true', help='protect error, crashed, canceled executions')
            sub.add_argument('--as-of', help='ISO-8601 reference time; default now (UTC)')
        if name in ('analyze', 'compare'):
            sub.add_argument('--max-workflows', type=workflow_limit,
                             help='optional non-negative observed workflow count limit; not an execution/memory/byte cap')
        if name == 'compare':
            sub.add_argument('--max-members', type=member_limit,
                             help='optional non-negative emitted execution member limit; summary emits zero; not a memory/byte cap')
            sub.add_argument('--summary-only', action='store_true', help='omit execution members; retain all global and workflow rollups')
            sub.add_argument('--baseline', required=True, help='baseline policy JSON file')
            sub.add_argument('--candidate', required=True, help='candidate policy JSON file')
            sub.add_argument('--as-of', required=True, help='shared ISO-8601 reference time (required for replay)')
        if name in ('reclaim', 'reclaim-plan'):
            sub.add_argument('--out', required=True, help='new plan JSON path' if name == 'reclaim-plan' else 'new compact database path')
        if name == 'reclaim':
            sub.add_argument('--overwrite', action='store_true', help='atomically replace an existing regular output file')
            sub.add_argument('--plan', help='saved reclamation plan; refuse a changed source before staging')
        if name == 'verify-copy':
            sub.add_argument('--report-content-mismatches', action='store_true',
                             help='report changed table names and digests (exit 2); requires a content plan')
            sub.add_argument('--plan', required=True, help='saved reclamation plan used to create this copy')
        if name in ('reclaim', 'reclaim-plan', 'verify-copy'):
            sub.add_argument('--max-content-bytes', type=content_byte_limit,
                             help='refuse total stored TEXT/BLOB bytes before content payload scans; not a scan/process cap')
            sub.add_argument('--max-content-rows', type=content_count_limit,
                             help='refuse total ordinary-table rows above N before content payload scans')
            sub.add_argument('--row-hash-memory-bytes', type=row_hash_budget, default=DEFAULT_ROW_HASH_MEMORY_BYTES,
                             help='raw row-hash bytes per sort run (minimum 32, default 8388608); extra hashes spill privately')
            sub.add_argument('--max-content-value-bytes', type=content_value_limit,
                             help='optional TEXT/BLOB byte limit per value in content scans; not a row/process cap')
            sub.add_argument('--max-content-row-bytes', type=content_row_limit,
                             help='optional aggregate stored TEXT/BLOB bytes per row in content scans; not a scan/process cap')
        if name in ('reclaim-plan', 'verify-copy'):
            sub.add_argument('--content', action='store_true',
                             help=('save typed per-table row digests (extra full scan)' if name == 'reclaim-plan'
                                   else 'require content digests in the plan; always checked when present'))
    fixture = commands.add_parser('fixture', help='generate redistributable synthetic data')
    gen = fixture.add_subparsers(dest='fixture_command', required=True).add_parser('gen')
    gen.add_argument('--out', required=True)
    gen.add_argument('--seed', type=int, default=42)
    gen.add_argument('--executions', type=nonnegative, default=2000)
    gen.add_argument('--deleted-percent', type=nonnegative, default=60)
    gen.add_argument('--payload-bytes', type=positive, default=8192)
    gen.add_argument('--overwrite', action='store_true')
    gen.add_argument('--json', action='store_true')
    return root


def mib(number):
    return f'{number / 1048576:.2f} MiB ({number:,} bytes)'


def safe(value):
    # Escape terminal control characters even when workflow names are untrusted.
    return json.dumps(value, ensure_ascii=True)


def load_policy(path, label):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DietError(label + ' policy contains duplicate keys')
            result[key] = value
        return result

    with open(path, 'rb') as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise DietError(label + ' policy exceeds 64 KiB limit')
    try:
        return json.loads(content.decode('utf-8'), object_pairs_hook=unique_keys)
    except (ValueError, RecursionError) as exc:
        raise DietError(label + ' policy must be valid UTF-8 JSON') from exc


def render(command, report):
    if command == 'analyze':
        s = report['storage']
        print('Database: ' + mib(s['file_bytes']))
        print(f"Page size: {s['page_size']}; pages: {s['page_count']}; free: {s['freelist_count']}")
        print('Reclaimable free-page capacity: ' + mib(s['freelist_bytes']))
        print('Table accounting: ' + report['table_storage']['method'])
        for item in report['table_storage']['tables']:
            size = item['allocated_bytes'] if item['allocated_bytes'] is not None else item['value_bytes_estimate']
            print(f"  {safe(item['table'])}: {item['rows']} rows; {mib(size)}")
        print('Workflows (stored payload bytes, descending):')
        for w in report['workflows']:
            print(f"  {safe(w['workflow_id'])} {safe(w['workflow_name'])}: {w['executions']} executions; {mib(w['payload_bytes'])}; {safe(w['statuses'])}")
        print('Largest executions:')
        for e in report['largest_executions']:
            print(f"  id={safe(e['id'])}, workflow={safe(e['workflow_id'])}, status={safe(e['status'])}, bytes={e['payload_bytes']}")
        print('Totals: ' + safe(report['totals']))
    elif command == 'plan':
        d = report['delete']
        print(f"DRY RUN: would select {d['executions']} executions / {d['data_rows']} data rows; payload {mib(d['payload_bytes'])}")
        print('Protected: ' + safe(report['protected']))
        print('Reasons: ' + safe(d['reasons']))
        print('Projected compact size (estimate): ' + mib(report['projected_size_estimate_bytes']))
    elif command == 'compare':
        print('DRY RUN: retention comparison at ' + report['baseline']['policy']['as_of'])
        if report.get('members_included') is False:
            print('Summary only: execution members omitted (members_included=false).')
        for cell in report['transitions']:
            print(f"{cell['from']} -> {cell['to']}: {cell['executions']} executions / {cell['data_rows']} data rows; payload {mib(cell['payload_bytes'])}")
            for row in cell.get('members', ()):
                print(f"  id={safe(row['execution_id'])}, workflow={safe(row['workflow_id'])}, bytes={row['payload_bytes']}; {row['baseline_reason']} -> {row['candidate_reason']}")
        print('Drop delta (candidate - baseline): ' + safe(report['drop_delta']))
        print('Workflow transition rollups:')
        for workflow in report['workflows']:
            totals = workflow['totals']
            print(f"Workflow {safe(workflow['workflow_id'])} {safe(workflow['workflow_name'])}: {totals['executions']} executions / {totals['data_rows']} data rows; {totals['payload_bytes']} payload bytes")
            for cell in workflow['transitions']:
                print(f"  {cell['from']} -> {cell['to']}: {cell['executions']} executions / {cell['data_rows']} data rows; {cell['payload_bytes']} payload bytes")
            print('  Drop delta (candidate - baseline): ' + safe(workflow['drop_delta']))
    elif command == 'reclaim':
        print('Compact copy: ' + safe(report['output']))
        print('Before: ' + mib(report['before']['file_bytes']) + '; after: ' + mib(report['after']['file_bytes']))
        print('Saved: ' + mib(report['saved_bytes']))
        print('Integrity: ok; all table row counts equal; source unchanged.')
        if report['content_verified']:
            print('All table row content matches the reclamation plan.')
    elif command == 'reclaim-plan':
        print('Reclamation plan saved; source: ' + safe(report['source']['path']))
        print('Database SHA-256: ' + report['source']['database']['sha256'])
        print('Schema SHA-256: ' + report['schema_sha256'])
        if 'content' in report:
            print('Typed row content digests saved for all tables.')
        print('Existing free-page capacity: ' + mib(report['storage']['freelist_bytes']))
    elif command == 'verify-copy' and not report['verified']:
        print('Copy content mismatch: ' + safe(report['output']))
        print('Integrity, schema and all table row counts match the reclamation plan.')
        print('Content algorithm: ' + report['content_algorithm'])
        for mismatch in report['content_mismatches']:
            print('Table ' + safe(mismatch['table']) + ': expected ' + mismatch['expected_sha256']
                  + '; actual ' + mismatch['actual_sha256'])
    elif command == 'verify-copy':
        print('Copy verified: ' + safe(report['output']))
        print('Integrity, schema and all table row counts match the reclamation plan.')
        if report['content_verified']:
            print('All table row content matches the reclamation plan.')
        print('Output SHA-256: ' + report['output_sha256'])
    else:
        print('Fixture: ' + safe(report['output']))
        print(f"{report['generated_executions']} generated; {report['deleted_executions']} deleted; {report['remaining_executions']} remaining; {report['freelist_pages']} free pages")
    for note in report.get('notes', []):
        print('Note: ' + note)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'analyze':
            report = analyze(args.db, top=args.top, force=args.force, use_dbstat=not args.no_dbstat,
                             max_workflows=args.max_workflows)
        elif args.command == 'plan':
            report = plan(args.db, max_age_days=args.max_age_days, max_count=args.max_count,
                          keep_failed=args.keep_failed, as_of=args.as_of, force=args.force)
        elif args.command == 'compare':
            report = compare(args.db, baseline=load_policy(args.baseline, 'baseline'),
                             candidate=load_policy(args.candidate, 'candidate'), as_of=args.as_of,
                             force=args.force, summary_only=args.summary_only, max_workflows=args.max_workflows,
                             max_members=args.max_members)
        elif args.command == 'reclaim':
            report = reclaim(args.db, out=args.out, overwrite=args.overwrite, force=args.force, plan_file=args.plan, row_hash_memory_bytes=args.row_hash_memory_bytes, max_content_value_bytes=args.max_content_value_bytes, max_content_row_bytes=args.max_content_row_bytes, max_content_rows=args.max_content_rows, max_content_bytes=args.max_content_bytes)
        elif args.command == 'reclaim-plan':
            report = reclaim_plan(args.db, out=args.out, force=args.force, content=args.content, row_hash_memory_bytes=args.row_hash_memory_bytes, max_content_value_bytes=args.max_content_value_bytes, max_content_row_bytes=args.max_content_row_bytes, max_content_rows=args.max_content_rows, max_content_bytes=args.max_content_bytes)
        elif args.command == 'verify-copy':
            report = verify_copy(args.db, plan_file=args.plan, content=args.content, report_content_mismatches=args.report_content_mismatches, row_hash_memory_bytes=args.row_hash_memory_bytes, max_content_value_bytes=args.max_content_value_bytes, max_content_row_bytes=args.max_content_row_bytes, max_content_rows=args.max_content_rows, max_content_bytes=args.max_content_bytes)
        else:
            report = generate(args.out, seed=args.seed, executions=args.executions,
                              deleted_percent=args.deleted_percent, payload_bytes=args.payload_bytes, overwrite=args.overwrite)
        if args.json:
            print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
        else:
            render(args.command, report)
        return 2 if args.command == 'verify-copy' and not report['verified'] else 0
    except (DietError, sqlite3.Error, OSError, ValueError) as exc:
        # Do not include DB contents or exception arguments containing payloads.
        print('flowwrench n8n-diet: ' + str(exc).encode('unicode_escape').decode('ascii'), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('flowwrench n8n-diet: interrupted; unpublished temporary output removed', file=sys.stderr)
        return 130
