"""Explicit constant durations and exact-second, half-open running intervals."""
from collections import Counter
from datetime import datetime, time, timedelta
import json
from pathlib import Path
import stat

from .analysis import Budget, DAY, MAX_FIRINGS, UTC, coverage, local_instants
from .extract import MAX_RECORDS, unique_object
from .model import InputError, timezone

MAX_DURATION_SECONDS = 7 * 86400
MAX_DURATION_BYTES = 1024 * 1024
MAX_PEAK_INTERVALS = 100_000


class OverlapBudget:
    """Share candidate work, execution and interval bounds across comparison sides."""
    def __init__(self, work=None):
        self.work = work if work is not None else Budget()
        self.executions = 0
        self.intervals = 0


def load_durations(path):
    """Read a versioned user sidecar; no inferred or default duration."""
    path = Path(path)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise InputError('duration input must be a regular file, not a symlink')
        with path.open('rb') as source:
            raw = source.read(MAX_DURATION_BYTES + 1)
        if len(raw) > MAX_DURATION_BYTES:
            raise InputError('duration input size limit exceeded (1 MiB)')
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError('cannot read duration input: expected valid UTF-8 JSON with unique keys') from None
    if (not isinstance(data, dict) or set(data) != {'version', 'durations_seconds'}
            or type(data['version']) is not int or data['version'] != 1):
        raise InputError('duration input must contain only version=1 and durations_seconds')
    if not isinstance(data['durations_seconds'], dict):
        raise InputError('durations_seconds must be an object mapping schedule IDs to seconds or null')
    return data['durations_seconds']


def validate_durations(durations, identities):
    if not isinstance(durations, dict) or len(durations) > MAX_RECORDS:
        raise InputError('durations_seconds must be an object with at most 2000 schedule IDs')
    for identity, duration in durations.items():
        if identity not in identities:
            raise InputError('duration input contains an unknown schedule ID; use extract IDs for this inventory')
        if duration is not None and (type(duration) is not int or not 0 <= duration <= MAX_DURATION_SECONDS):
            raise InputError('duration must be null (unknown) or an integer in 0..604800 elapsed seconds')


def timestamp(instant):
    return instant.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def running_intervals(record, start, end, duration, budget):
    """Enumerate starts far enough back to include every carry-in execution.

    Unlike the start-count forecast, seconds are part of the local candidate,
    before timezone conversion, so even historical sub-minute offsets are exact.
    Duration is elapsed UTC time, including through DST transitions.
    """
    zone, cron = timezone(record.timezone), record.cron()
    elapsed = timedelta(seconds=duration)
    earliest = start - elapsed
    day = earliest.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    while day <= last:
        if cron.matches_date(day):
            budget.consume(len(cron.hours) * len(cron.minutes))
            for hour in sorted(cron.hours):
                for minute in sorted(cron.minutes):
                    local = datetime.combine(day, time(hour, minute, record.second))
                    for instant in local_instants(local, zone):
                        finish = instant + elapsed
                        if ((earliest < instant < end and finish > start)
                                or (duration == 0 and start <= instant < end)):
                            yield max(instant, start), min(finish, end), instant < start
        day += DAY


def concurrency_score(events):
    """Exact half-open sweep: peak and integral of squared running count."""
    current = peak = squares = 0
    previous = None
    for point in sorted(t for t, delta in events.items() if delta):
        if previous is not None:
            squares += current * current * int((point - previous).total_seconds())
        current += events[point]
        peak = max(peak, current)
        previous = point
    return peak, squares


def execution_events(record, start, end, duration, budget):
    """Share exact interval enumeration and execution bounds with the optimizer."""
    events = Counter()
    firings = carry_in = 0
    for begin, finish, carried in running_intervals(record, start, end, duration, budget.work):
        firings += not carried
        carry_in += carried
        budget.executions += 1
        if budget.executions > MAX_FIRINGS:
            raise InputError('overlap firing limit exceeded; reduce --days or split schedules')
        if begin < finish:  # Known zero duration contributes starts but no running load.
            events[begin] += 1
            events[finish] -= 1
    return events, firings, carry_in


def peak_intervals(events):
    """Group all ties before counting: an ending run never overlaps its successor.

    Use two passes so only intervals at the final peak consume the output bound.
    Net-zero ties are removed to merge adjacent equal-count peak intervals.
    """
    points = sorted(t for t, delta in events.items() if delta)
    peak, _ = concurrency_score(events)
    result, current = [], 0
    for index, point in enumerate(points[:-1]):
        current += events[point]
        if current == peak and peak:
            if len(result) >= MAX_PEAK_INTERVALS:
                raise InputError('overlap peak interval limit exceeded; reduce --days or split schedules')
            result.append({'from': timestamp(point), 'until_exclusive': timestamp(points[index + 1])})
    return peak, result


def overlap(records, start, end, durations=None, *, budget=None):
    """Report known-duration concurrency, leaving the full peak unknown if partial."""
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.utcoffset() is None or end.utcoffset() is None
            or start.microsecond or end.microsecond):
        raise InputError('overlap horizon must use timezone-aware whole-second instants')
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if not 1970 <= start.year <= 2098 or not timedelta(0) < end - start <= 730 * DAY:
        raise InputError('overlap horizon must start in 1970..2098 and span at most 730 days')
    records = sorted(records, key=lambda r: r.id)
    if len(records) > MAX_RECORDS:
        raise InputError('overlap record limit exceeded (2000); split schedules')
    identities = {r.id for r in records}
    if len(identities) != len(records):
        raise InputError('duplicate schedule identities in overlap inventory')
    durations = {} if durations is None else durations
    validate_durations(durations, identities)
    budget = budget if budget is not None else OverlapBudget()
    events = Counter()
    known_firings = carry_in = 0
    unknown = []
    for record in records:
        if not record.enabled or not record.supported:
            continue
        duration = durations.get(record.id)
        if duration is None:
            unknown.append(record.id)
            continue
        own_events, firings, carried = execution_events(record, start, end, duration, budget)
        known_firings += firings
        carry_in += carried
        events.update(own_events)
    peak, intervals = peak_intervals(events)
    budget.intervals += len(intervals)
    if budget.intervals > MAX_PEAK_INTERVALS:
        raise InputError('overlap peak interval limit exceeded; reduce --days or split schedules')
    cov = coverage(records)
    complete = cov['complete'] and not unknown
    return {**cov, 'schedule_coverage_complete': cov['complete'], 'complete': complete,
            'from': timestamp(start), 'until_exclusive': timestamp(end),
            'known_peak': peak, 'peak': peak if complete else None,
            'known_peak_intervals': intervals, 'known_firings': known_firings,
            'carry_in_firings': carry_in, 'unknown_duration_ids': unknown,
            'schedules': [{**r.as_dict(), 'duration_seconds': durations.get(r.id)} for r in records],
            'model': 'Constant user-supplied elapsed durations; exact source seconds; half-open UTC intervals; '
                     'carry-in included; gaps skipped, both folds counted; known_peak is a lower bound when incomplete'}
