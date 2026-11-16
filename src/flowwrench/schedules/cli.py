"""Read-only CLI; stdout is a report, stderr contains diagnostics."""
import argparse
from datetime import datetime, timezone as dt_timezone
import json
import sys
from . import __version__
from .analysis import horizon, analyze, dst, suggest, coverage
from .comparison import compare
from .extract import extract
from .model import InputError
from .overlap import load_durations, overlap
from .snapshot import load_snapshot, snapshot_json


def shift(value):
    raw = value[:-1] if value.endswith('m') else value
    if len(raw) > 2 or not raw.isascii() or not raw.isdecimal() or not 0 <= int(raw) <= 59:
        raise argparse.ArgumentTypeError('--max-shift must be 0..59 minutes, e.g. 10 or 10m')
    return int(raw)


def source_path(value):
    if value == '':
        raise argparse.ArgumentTypeError('source path must not be empty; use . for the current directory')
    return value


class InventorySelector(argparse.Action):
    """Reject repeated selectors before argparse can discard an earlier input."""
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise argparse.ArgumentError(self, 'may be specified only once; select one input per selector')
        setattr(namespace, self.dest, values)


def parser():
    p = argparse.ArgumentParser(prog='flowwrench schedules', description='Offline schedule inventory, herd/DST analysis and deterministic jitter plans')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)
    today = datetime.now(dt_timezone.utc).date().isoformat()
    for name in ('extract', 'analyze', 'dst', 'suggest', 'compare', 'overlap'):
        cmd = sub.add_parser(name)
        if name == 'compare':
            for side in ('before', 'after'):
                group = cmd.add_mutually_exclusive_group(required=True)
                group.add_argument(f'--{side}', nargs='+', type=source_path, action=InventorySelector, metavar='PATH', help=f'{side} export files/directories')
                group.add_argument(f'--{side}-snapshot', action=InventorySelector, metavar='JSON', help=f'{side} versioned inventory snapshot')
                cmd.add_argument(f'--{side}-durations', action=InventorySelector, metavar='JSON',
                                 help=f'{side} v1 duration sidecar; requires both sides')
        elif name in ('analyze', 'dst', 'suggest', 'overlap'):
            cmd.add_argument('paths', nargs='*', type=source_path, metavar='PATH')
            cmd.add_argument('--snapshot', action=InventorySelector, metavar='FILE', help='versioned inventory snapshot instead of source paths')
        else:
            cmd.add_argument('paths', nargs='+', type=source_path, metavar='PATH')
        cmd.add_argument('--format', choices=('text', 'json', 'snapshot') if name == 'extract' else ('text', 'json'), default='text')
        cmd.add_argument('--default-timezone', default='UTC', metavar='IANA', help='explicit assumption for missing platform/server timezone (default UTC)')
        cmd.add_argument('--strict', action='store_true', help='exit 2 if any record is unsupported'
                         + (' or any enabled supported record has unknown duration' if name in ('overlap', 'compare') else ''))
        if name != 'extract':
            cmd.add_argument('--from', dest='from_date', default=today, metavar='DATE', help='UTC start date (default today)')
            cmd.add_argument('--days', type=int, default=370 if name == 'dst' else 7)
        if name in ('analyze', 'suggest', 'compare'):
            cmd.add_argument('--window', type=int, default=60, metavar='SECONDS', help='trailing firing window, a multiple of 60 (default 60)')
        if name in ('analyze', 'compare'):
            cmd.add_argument('--max-peak', type=int, metavar='K',
                             help='exit 1 above this start-count peak (compare: after side)')
        if name == 'compare':
            cmd.add_argument('--max-peak-increase', type=int, metavar='K',
                             help='exit 1 above this start-count peak increase (after minus before)')
            cmd.add_argument('--max-added-firings', type=int, metavar='K',
                             help='exit 1 above this number of added firing events in the horizon')
            cmd.add_argument('--max-removed-firings', type=int, metavar='K',
                             help='exit 1 above this number of removed firing events in the horizon')
        if name == 'suggest':
            cmd.add_argument('--max-shift', type=shift, default=10, metavar='MINUTES')
            cmd.add_argument('--objective', choices=('fire-density', 'concurrency'), default='fire-density')
            cmd.add_argument('--durations', action=InventorySelector, metavar='JSON',
                             help='v1 duration sidecar; requires --objective concurrency')
        if name == 'overlap':
            cmd.add_argument('--durations', action=InventorySelector, required=True, metavar='JSON', help='version 1 per-ID duration sidecar in elapsed seconds')
    return p


def safe_print(*values, **kwargs):
    # Untrusted labels cannot send terminal control sequences in text reports.
    values = tuple(''.join(f'\\x{ord(c):02x}' if ord(c) < 32 or ord(c) == 127 else c
                           for c in str(value)) for value in values)
    print(*values, **kwargs)


def print_text(command, report):
    if command == 'extract':
        safe_print(f"{len(report['schedules'])} schedules")
        for r in report['schedules']:
            state = 'disabled' if not r['enabled'] else 'supported' if r['supported'] else 'UNSUPPORTED'
            safe_print(f"{r['platform']} | {r['workflow']} | {r['node']} | {r['expr']} | {r['timezone']} | {state}")
    elif command == 'analyze':
        safe_print(f"Peak {report['peak']} at {report['peak_at']} in trailing {report['window_seconds']}s; {report['total_firings']} firings")
        safe_print(f"Coverage: {report['analyzed']}/{report['records']} analyzed; {report['disabled']} disabled; {report['unsupported']} unsupported")
        for platform, stats in report['per_platform'].items():
            safe_print(f"  {platform}: {stats['total_firings']} firings, peak {stats['peak']}")
        for minute in report['hot_minutes'][:10]:
            safe_print(f"  hot {minute['minute']}: {minute['count']}")
        for h in report['dst_hazards']:
            safe_print(f"  DST {h['hazard']}: {h['local_time']} {h['timezone']} ({h['node']})")
    elif command == 'overlap':
        state = f"overall peak {report['peak']}" if report['complete'] else 'overall peak unknown'
        safe_print(f"Known-duration peak {report['known_peak']}; {state}; "
                   f"{len(report['unknown_duration_ids'])} enabled supported schedules with unknown duration")
        safe_print(f"Coverage: {report['analyzed']}/{report['records']} supported/enabled; "
                   f"{report['disabled']} disabled; {report['unsupported']} unsupported; complete={report['complete']}")
        safe_print(f"{report['known_firings']} known-duration starts in horizon; {report['carry_in_firings']} carry-in executions")
        for interval in report['known_peak_intervals']:
            safe_print(f"  known peak [{interval['from']}, {interval['until_exclusive']})")
    elif command == 'dst':
        safe_print(f"{len(report['hazards'])} DST hazards; {report['analyzed']} schedules analyzed")
        for h in report['hazards']:
            safe_print(f"{h['hazard']}: {h['local_time']} {h['timezone']} ({h['platform']}/{h['node']})")
    elif command == 'compare':
        safe_print(f"Peak {report['before']['peak']} -> {report['after']['peak']} in trailing {report['before']['window_seconds']}s; "
                   f"{len(report['removed'])} removed; {len(report['added'])} added; {report['unchanged_firings']} unchanged")
        if 'overlap' in report:
            ov = report['overlap']
            peaks = [str(ov[side]['peak']) if ov[side]['complete'] else 'unknown'
                     for side in ('before', 'after')]
            delta = ov['peak_delta'] if ov['complete'] else 'unknown'
            safe_print(f"Overlap peak {peaks[0]} -> {peaks[1]}; delta {delta}")
            for side in ('before', 'after'):
                data = ov[side]
                safe_print(f"  {side} known peak {data['known_peak']}; "
                           f"{len(data['unknown_duration_ids'])} unknown durations; complete={data['complete']}")
        for side in ('before', 'after'):
            data = report[side]
            safe_print(f"{side}: {data['analyzed']}/{data['records']} analyzed; {data['disabled']} disabled; "
                       f"{data['unsupported']} unsupported; complete={data['complete']}")
            for h in data['dst_hazards']:
                safe_print(f"  {side} DST {h['hazard']}: {h['local_time']} {h['timezone']} ({h['node']})")
        for kind in ('removed', 'added'):
            for event in report[kind]:
                safe_print(f"{kind} {event['id']} {event['utc_minute']} (source second={event['source_second']}) | "
                           f"{event['local_time']} {event['timezone']} offset={event['utc_offset_seconds']}s "
                           f"fold={event['fold']} ambiguous={event['ambiguous']}")
    else:
        if report.get('objective') == 'concurrency':
            before, after = report['overlap']['before'], report['overlap']['after']
            safe_print(f"Objective concurrency: modeled running peak {before['peak']} -> {after['peak']}; "
                       f"known lower bound {before['known_peak']} -> {after['known_peak']}; "
                       f"complete={report['complete']}")
        safe_print(f"Peak {report['before']['peak']} -> {report['after']['peak']}; firings {report['before']['total_firings']} -> {report['after']['total_firings']}")
        for r in report['plan']:
            safe_print(f"+{r['offset_minutes']}m {r['platform']}/{r['node']}: {r['original_expr']} -> {r['proposed_expr']}")
        safe_print('Read-only plan. Review platform constraints and dependencies before applying manually.')


def inventory_metadata(inv, default_timezone, side=None, saved=False):
    prefix = f'{side}: ' if side else ''
    for warning in inv.warnings:
        safe_print(f'warning: {prefix}{warning}', file=sys.stderr)
    for r in inv.schedules:
        if not r.supported:
            safe_print(f'warning: {prefix}{r.file} [{r.node}]: {r.error}', file=sys.stderr)
    assumed_tz = sum(r.timezone_assumed for r in inv.schedules)
    assumed_enabled = sum(r.enabled_assumed for r in inv.schedules)
    if assumed_tz:
        origin = 'saved default_timezone' if saved else '--default-timezone'
        safe_print(f'warning: {prefix}{assumed_tz} records assume {origin}={default_timezone}; confirm instance timezone', file=sys.stderr)
    if assumed_enabled:
        safe_print(f'warning: {prefix}{assumed_enabled} records lack activation state and are assumed enabled', file=sys.stderr)
    return {'warnings': inv.warnings,
            'assumptions': {'default_timezone': default_timezone,
                            'timezone_assumed_records': assumed_tz,
                            'enabled_assumed_records': assumed_enabled}}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command in ('analyze', 'dst', 'suggest', 'overlap') and bool(args.paths) == (args.snapshot is not None):
        p.error(f'{args.command} requires exactly one of source PATH... or --snapshot FILE')
    try:
        if args.command == 'suggest' and ((args.objective == 'concurrency') != (args.durations is not None)):
            raise InputError('suggest requires both --objective concurrency and --durations')
        if hasattr(args, 'window') and (not 60 <= args.window <= 86400 or args.window % 60):
            raise InputError('--window must be a multiple of 60 seconds in 60..86400')
        if getattr(args, 'max_peak', None) is not None and args.max_peak < 0:
            raise InputError('--max-peak must be non-negative')
        if getattr(args, 'max_peak_increase', None) is not None and args.max_peak_increase < 0:
            raise InputError('--max-peak-increase must be non-negative')
        if getattr(args, 'max_added_firings', None) is not None and args.max_added_firings < 0:
            raise InputError('--max-added-firings must be non-negative')
        if getattr(args, 'max_removed_firings', None) is not None and args.max_removed_firings < 0:
            raise InputError('--max-removed-firings must be non-negative')
        bounds = horizon(args.from_date, args.days) if args.command != 'extract' else None
        if args.command == 'compare':
            if (args.before_durations is None) != (args.after_durations is None):
                raise InputError('comparison requires both --before-durations and --after-durations')
            def load_side(side):
                path = getattr(args, f'{side}_snapshot')
                return load_snapshot(path) if path is not None else extract(getattr(args, side), args.default_timezone)
            before, after = load_side('before'), load_side('after')
            metadata = {side: inventory_metadata(inv, inv.default_timezone, side, getattr(args, f'{side}_snapshot') is not None)
                        for side, inv in (('before', before), ('after', after))}
            report = compare(before.schedules, after.schedules, *bounds, args.window,
                             before_durations=load_durations(args.before_durations) if args.before_durations is not None else None,
                             after_durations=load_durations(args.after_durations) if args.after_durations is not None else None)
            for side in ('before', 'after'):
                report[side].update(metadata[side])
            unsupported = any(not r.supported for r in before.schedules + after.schedules)
        else:
            saved = getattr(args, 'snapshot', None) is not None
            inv = load_snapshot(args.snapshot) if saved else extract(args.paths, args.default_timezone)
            metadata = inventory_metadata(inv, inv.default_timezone, saved=saved)
            unsupported = any(not r.supported for r in inv.schedules)
            if args.command == 'extract':
                report = {'schedules': [r.as_dict() for r in inv.schedules], **coverage(inv.schedules)}
            elif args.command == 'analyze':
                report = analyze(inv.schedules, *bounds, args.window)
            elif args.command == 'dst':
                report = dst(inv.schedules, *bounds)
            elif args.command == 'overlap':
                report = overlap(inv.schedules, *bounds, load_durations(args.durations))
            else:
                report = suggest(inv.schedules, *bounds, args.max_shift, args.window,
                                 objective=args.objective,
                                 durations=load_durations(args.durations) if args.durations is not None else None)
            report.update(metadata)
        if args.format == 'snapshot':
            sys.stdout.write(snapshot_json(inv))
        elif args.format == 'json':
            json.dump(report, sys.stdout, indent=2, ensure_ascii=True, allow_nan=False)
            print()
        else:
            print_text(args.command, report)
        if args.strict and unsupported:
            return 2
        if args.command in ('overlap', 'compare') and args.strict and not report['complete']:
            return 2
        if args.command in ('analyze', 'compare') and args.max_peak is not None:
            peak = report['after']['peak'] if args.command == 'compare' else report['peak']
            if peak > args.max_peak:
                return 1
        if args.command == 'compare' and args.max_peak_increase is not None:
            if report['peak_delta'] > args.max_peak_increase:
                return 1
        if args.command == 'compare' and args.max_added_firings is not None:
            if len(report['added']) > args.max_added_firings:
                return 1
        if args.command == 'compare' and args.max_removed_firings is not None:
            if len(report['removed']) > args.max_removed_firings:
                return 1
        return 0
    except InputError as e:
        safe_print(f'error: {e}', file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
