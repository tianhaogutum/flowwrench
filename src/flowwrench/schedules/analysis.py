"""UTC minute buckets, explicit DST hazards, and conservative forward jitter."""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from hashlib import sha256
from heapq import nsmallest
from .model import InputError, timezone

UTC = dt_timezone.utc
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
MAX_CANDIDATES = 8_000_000
MAX_SCORE_WORK = 20_000_000
MAX_FIRINGS = 1_000_000
MAX_HAZARDS = 10000


class Budget:
    def __init__(self):
        self.remaining = MAX_CANDIDATES
        self.score_remaining = MAX_SCORE_WORK

    def consume(self, n=1):
        self.remaining -= n
        if self.remaining < 0:
            raise InputError('forecast work limit exceeded; reduce --days or split schedules')

    def score(self, n):
        self.score_remaining -= n
        if self.score_remaining < 0:
            raise InputError('optimizer work limit exceeded; reduce --days/--max-shift or split schedules')


def horizon(from_date, days):
    try:
        if not isinstance(from_date, str) or len(from_date) != 10 or from_date[4] != '-' or from_date[7] != '-':
            raise ValueError
        day = date.fromisoformat(from_date)
    except (TypeError, ValueError):
        raise InputError('--from must be an ISO date (YYYY-MM-DD)') from None
    if not 1970 <= day.year <= 2098:
        raise InputError('--from year must be in 1970..2098')
    if not 1 <= days <= 730:
        raise InputError('--days must be in 1..730')
    start = datetime.combine(day, time(), UTC)
    return start, start + days * DAY


def local_instants(local, tz):
    """0 instants = nonexistent, 1 = ordinary, 2 = ambiguous (PEP 495)."""
    instants = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=tz, fold=fold).astimezone(UTC)
        if candidate.astimezone(tz).replace(tzinfo=None) == local:
            instants.add(candidate)
    return tuple(sorted(instants))


def date_span(start, end, tz):
    day = start.astimezone(tz).date()
    last = (end - MINUTE).astimezone(tz).date()
    while day <= last:
        yield day
        day += DAY


def hazard(record, local, instants):
    return {'id': record.id, 'platform': record.platform, 'workflow': record.workflow,
            'node': record.node, 'timezone': record.timezone,
            'local_time': local.isoformat(timespec='minutes'),
            'hazard': 'skipped' if not instants else 'ambiguous',
            'utc_instants': [iso(t) for t in instants]}


def hazard_in_horizon(local, tz, instants, start, end):
    # A fold is relevant if either possible fire is inside the horizon. Keep
    # both instants in its provenance even if the other is outside the horizon.
    references = instants or (local.replace(tzinfo=tz).astimezone(UTC),)
    return any(start <= instant < end for instant in references)


def iso(instant):
    return instant.isoformat(timespec='minutes').replace('+00:00', 'Z')


def forecast(record, start, end, budget, cron=None, collect_hazards=False):
    cron = cron or record.cron()
    tz = timezone(record.timezone)
    fires, hazards = [], []
    for day in date_span(start, end, tz):
        if not cron.matches_date(day):
            continue
        budget.consume(len(cron.hours) * len(cron.minutes))
        for hour in sorted(cron.hours):
            for minute in sorted(cron.minutes):
                local = datetime.combine(day, time(hour, minute))
                instants = local_instants(local, tz)
                for instant in instants:
                    if start <= instant < end:
                        fires.append(int((instant - start).total_seconds() // 60))
                if collect_hazards and len(instants) != 1:
                    if hazard_in_horizon(local, tz, instants, start, end):
                        hazards.append(hazard(record, local, instants))
                        if len(hazards) > MAX_HAZARDS:
                            raise InputError('DST hazard limit exceeded; reduce --days')
    return sorted(fires), hazards


def active_records(records):
    active = [r for r in records if r.enabled and r.supported]
    if not active:
        raise InputError('no enabled, supported schedules to analyze (use extract for inventory)')
    return active


def coverage(records):
    excluded = [{'id': r.id, 'platform': r.platform, 'node': r.node,
                 'reason': r.error or 'disabled'} for r in records if not r.enabled or not r.supported]
    return {'records': len(records), 'analyzed': sum(r.enabled and r.supported for r in records),
            'disabled': sum(not r.enabled for r in records),
            'unsupported': sum(not r.supported for r in records),
            'complete': not any(r.enabled and not r.supported for r in records), 'excluded': excluded}


def rolling(histogram, length, window_minutes):
    if window_minutes == 1:
        return dict(histogram)
    diffs = Counter()
    for index, count in histogram.items():
        diffs[index] += count
        if index + window_minutes < length:
            diffs[index + window_minutes] -= count
    result, current = {}, 0
    for i in range(length):
        current += diffs.get(i, 0)
        if current:
            result[i] = current
    return result


def summary(histogram, start, end, window=60, include_histogram=True):
    length = int((end - start).total_seconds() // 60)
    windowed = rolling(histogram, length, window // 60)
    peak = max(windowed.values(), default=0)
    hot = nsmallest(20, windowed, key=lambda k: (-windowed[k], k))
    result = {
        'from': iso(start), 'until_exclusive': iso(end), 'window_seconds': window,
        'total_minutes': length, 'total_firings': sum(histogram.values()),
        'zero_minutes': length - len(histogram), 'peak': peak,
        'peak_at': iso(start + min((k for k, v in windowed.items() if v == peak), default=0) * MINUTE) if peak else None,
        'hot_minutes': [{'minute': iso(start + k * MINUTE), 'count': windowed[k]} for k in hot],
    }
    if include_histogram:
        result['histogram'] = {iso(start + k * MINUTE): v for k, v in sorted(histogram.items())}
        if window != 60:
            result['window_histogram'] = {iso(start + k * MINUTE): v for k, v in sorted(windowed.items())}
    return result


def analyze(records, start, end, window=60):
    # Snapshots need not retain extraction traversal order.
    records = sorted(records, key=lambda r: r.id)
    active = active_records(records)
    budget = Budget()
    histogram, per_platform, hazards = Counter(), {}, []
    for record in active:
        fires, problems = forecast(record, start, end, budget, collect_hazards=True)
        histogram.update(fires)
        per_platform.setdefault(record.platform, Counter()).update(fires)
        hazards.extend(problems)
        if sum(histogram.values()) > MAX_FIRINGS or len(hazards) > MAX_HAZARDS:
            raise InputError('result size limit exceeded; reduce --days or split schedules')
    result = summary(histogram, start, end, window)
    result.update(coverage(records))
    result['schedules'] = [r.as_dict() for r in records]
    result['per_platform'] = {p: summary(h, start, end, window, False) for p, h in sorted(per_platform.items())}
    result['dst_hazards'] = hazards
    result['model'] = 'UTC minute firing counts; both folds counted, gaps skipped; not running-job concurrency'
    return result


def dst(records, start, end):
    records = sorted(records, key=lambda r: r.id)
    budget, hazards = Budget(), []
    for record in active_records(records):
        cron, tz = record.cron(), timezone(record.timezone)
        for day in date_span(start, end, tz):
            if not cron.matches_date(day):
                continue
            midnight = datetime.combine(day, time())
            offsets = {(midnight + timedelta(hours=h)).replace(tzinfo=tz).utcoffset() for h in (0, 6, 12, 18, 24)}
            if len(offsets) == 1:
                continue
            budget.consume(len(cron.hours) * len(cron.minutes))
            for hour in sorted(cron.hours):
                for minute in sorted(cron.minutes):
                    local = datetime.combine(day, time(hour, minute))
                    instants = local_instants(local, tz)
                    if len(instants) != 1 and hazard_in_horizon(local, tz, instants, start, end):
                        hazards.append(hazard(record, local, instants))
                        if len(hazards) > MAX_HAZARDS:
                            raise InputError('DST hazard limit exceeded; reduce --days or split schedules')
    return {'from': iso(start), 'until_exclusive': iso(end), **coverage(records),
            'hazards': sorted(hazards, key=lambda h: (h['local_time'], h['id']))}


def score(histogram, length, window, budget):
    budget.score(len(histogram) if window == 60 else length)
    values = rolling(histogram, length, window // 60).values()
    peak, squares = 0, 0
    for v in values:
        peak = max(peak, v)
        squares += v * v
    return peak, squares


def with_fires(base, fires):
    result = base.copy()
    result.update(fires)
    return result


def proposed_source(record, cron):
    fields = record.source_expr.split() if record.source_expr else []
    if record.source_dialect in ('n8n', 'windmill') and len(fields) in (6, 7):
        return f"{record.second} {cron.expr}" + (' *' if len(fields) == 7 else '')
    return cron.expr


def suggest(records, start, end, max_shift=10, window=60, *, objective='fire-density', durations=None):
    if objective not in ('fire-density', 'concurrency'):
        raise InputError('suggest objective must be fire-density or concurrency')
    if (objective == 'concurrency') != (durations is not None):
        raise InputError('suggest requires both --objective concurrency and --durations')
    records = sorted(records, key=lambda r: r.id)
    active, budget = active_records(records), Budget()
    duration_model = None
    if objective == 'concurrency':
        from .duration_objective import DurationObjective
        duration_model = DurationObjective(records, start, end, durations, budget)
    length = int((end - start).total_seconds() // 60)
    histogram, originals = Counter(), {}
    for r in active:
        fires, _ = forecast(r, start, end, budget)
        originals[r.id] = fires
        histogram.update(fires)
        if sum(histogram.values()) > MAX_FIRINGS:
            raise InputError('result size limit exceeded; reduce --days or split schedules')
    before = summary(histogram, start, end, window, False)
    plans = []
    # Identity order and SHA-256 tie breaks make results independent of traversal order.
    for record in sorted(active, key=lambda r: r.id):
        original = originals[record.id]
        base = histogram.copy()
        base.subtract(original)
        base = +base
        cron = record.cron()
        best_offset, best_cron, best_hist = 0, cron, histogram
        best_events = duration_model.events if duration_model else None
        initial_score = duration_model.begin(record) if duration_model else score(histogram, length, window, budget)
        best_score = (*initial_score, 0,
                      sha256(f'{record.id}:0'.encode()).digest())
        candidates = (range(1, min(max_shift, 59 - max(cron.minutes)) + 1)
                      if original and (not duration_model or durations[record.id] != 0) else ())
        for offset in candidates:
            candidate = cron.shifted(offset)
            fires, _ = forecast(record, start, end, budget, candidate)
            # Never advertise a reduction achieved by dropping fires at DST boundaries.
            if len(fires) != len(original):
                continue
            changed = with_fires(base, fires)
            if duration_model:
                evaluated = duration_model.candidate(record, candidate)
                if evaluated is None:
                    continue
                measured, candidate_events = evaluated
            else:
                measured = score(changed, length, window, budget)
            candidate_score = (*measured, int(duration_model is not None),
                               sha256(f'{record.id}:{offset}'.encode()).digest())
            if candidate_score < best_score:
                best_score = candidate_score
                best_offset, best_cron, best_hist = offset, candidate, changed
                if duration_model:
                    best_events = candidate_events
        histogram = best_hist
        if duration_model:
            duration_model.events = best_events
        plans.append({'id': record.id, 'platform': record.platform, 'file': record.file,
                      'workflow': record.workflow, 'node': record.node, 'timezone': record.timezone,
                      'original_expr': record.expr, 'proposed_expr': best_cron.expr,
                      'proposed_source_expr': proposed_source(record, best_cron),
                      'offset_minutes': best_offset, 'firings': len(original),
                      'reason': 'no forward shift fits inside the hour' if max(cron.minutes) == 59 else
                                'no fires in this horizon' if not original else 'bounded deterministic greedy plan',
                      'application': 'manual; use a custom cron trigger where the platform only exposes presets'})
    after = summary(histogram, start, end, window, False)
    result = {**coverage(records), 'max_shift_minutes': max_shift, 'algorithm': 'sha256-tiebroken greedy (peak, sum of squared bucket counts)',
            'before': before, 'after': after, 'plan': plans,
            'limitations': ['Forward shifts stay in the same hour; no hour/day rollover.',
                            'Comparison applies only to this horizon and conservative DST model.',
                            'No files, services or workflows are modified.']}
    if duration_model:
        result.update(objective='concurrency', overlap=duration_model.finish(plans),
                      algorithm='greedy (known running peak, squared job seconds); zero-offset then sha256 ties')
        result['limitations'].append('Running objective covers known supported jobs only; incomplete peaks are lower bounds, not runtime parity.')
    return result
