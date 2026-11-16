"""Finite-horizon inventory comparison using the shared conservative forecast."""
from collections import Counter
from .analysis import (Budget, MAX_FIRINGS, MAX_HAZARDS, MINUTE, coverage,
                       forecast, iso, local_instants, summary)
from .model import InputError, timezone
from .overlap import OverlapBudget, overlap, validate_durations

MAX_CHANGES = 100_000


def fire_event(record, index, start):
    bucket = start + index * MINUTE
    tz = timezone(record.timezone)
    local = bucket.astimezone(tz)
    # Recover the scheduled wall minute even for historical sub-minute offsets.
    wall = local.replace(tzinfo=None, second=0, microsecond=0)
    if local.second:
        wall += MINUTE
    instants = local_instants(wall, tz)
    instant = next(t for t in instants if int((t - start).total_seconds() // 60) == index)
    local = instant.astimezone(tz)
    return {'id': record.id, 'utc_minute': iso(bucket), 'source_second': record.second,
            'timezone': record.timezone, 'local_time': wall.isoformat(timespec='minutes'),
            'utc_offset_seconds': int(local.utcoffset().total_seconds()),
            'fold': local.fold, 'ambiguous': len(instants) == 2,
            'possible_utc_minutes': [iso(t) for t in instants]}


def compare(before, after, start, end, window=60, *, before_durations=None, after_durations=None):
    """Match by (stable schedule ID, UTC minute, fixed source second).

    Metadata remains on both sides even when UTC fires are unchanged. Disabled
    and unsupported records contribute no fires; unlike analyze, zero active
    schedules are useful here (e.g. disabling the last job).
    """
    if not 60 <= window <= 86400 or window % 60:
        raise InputError('--window must be a multiple of 60 seconds in 60..86400')
    if (before_durations is None) != (after_durations is None):
        raise InputError('comparison requires both before and after duration sidecars')
    duration_comparison = before_durations is not None
    if duration_comparison:
        validate_durations(before_durations, {r.id for r in before})
        validate_durations(after_durations, {r.id for r in after})
    budget = Budget()
    report, forecasts, sources = {}, {}, {}
    total_firings = total_hazards = 0
    for side, records in (('before', before), ('after', after)):
        records = sorted(records, key=lambda r: r.id)
        sources[side] = {r.id: r for r in records}
        if len(sources[side]) != len(records):
            raise InputError(f'duplicate schedule identities in {side} inventory')
        histogram, hazards, fires_by_id = Counter(), [], {}
        for record in records:
            if not record.enabled or not record.supported:
                continue
            fires, problems = forecast(record, start, end, budget, collect_hazards=True)
            total_firings += len(fires)
            total_hazards += len(problems)
            if total_firings > MAX_FIRINGS or total_hazards > MAX_HAZARDS:
                raise InputError('comparison result size limit exceeded; reduce --days or split schedules')
            histogram.update(fires)
            hazards.extend(problems)
            fires_by_id[record.id] = set(fires)
        forecasts[side] = fires_by_id
        report[side] = {**summary(histogram, start, end, window, False), **coverage(records),
                        'schedules': [r.as_dict() for r in records],
                        'dst_hazards': sorted(hazards, key=lambda h: (h['local_time'], h['id']))}
    added, removed, unchanged = [], [], 0
    for identity in sorted(forecasts['before'].keys() | forecasts['after'].keys()):
        old = forecasts['before'].get(identity, set())
        new = forecasts['after'].get(identity, set())
        old_record = sources['before'].get(identity)
        new_record = sources['after'].get(identity)
        common = old & new if old_record and new_record and old_record.second == new_record.second else set()
        unchanged += len(common)
        deleted, inserted = old - common, new - common
        if len(removed) + len(added) + len(deleted) + len(inserted) > MAX_CHANGES:
            raise InputError(f'comparison change limit exceeded ({MAX_CHANGES}); reduce --days or split schedules')
        removed.extend(fire_event(old_record, index, start) for index in sorted(deleted))
        added.extend(fire_event(new_record, index, start) for index in sorted(inserted))
    order = lambda e: (e['utc_minute'], e['id'], e['source_second'])
    report.update({'added': sorted(added, key=order), 'removed': sorted(removed, key=order),
                   'unchanged_firings': unchanged,
                   'peak_delta': report['after']['peak'] - report['before']['peak'],
                   'total_firings_delta': report['after']['total_firings'] - report['before']['total_firings'],
                   'complete': report['before']['complete'] and report['after']['complete'],
                   'matching': 'stable schedule ID + UTC minute + fixed source second; metadata retained separately',
                   'model': 'UTC minute firing counts; both folds counted, gaps skipped; not running-job concurrency'})
    if duration_comparison:
        overlap_budget = OverlapBudget(budget)
        old = overlap(before, start, end, before_durations, budget=overlap_budget)
        new = overlap(after, start, end, after_durations, budget=overlap_budget)
        complete = old['complete'] and new['complete']
        report['overlap'] = {'before': old, 'after': new, 'complete': complete,
                             'peak_delta': new['peak'] - old['peak'] if complete else None}
        report['complete'] = report['complete'] and complete
    return report
