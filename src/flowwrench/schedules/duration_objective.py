"""Optional concurrency scoring for the existing bounded jitter optimizer."""
from collections import Counter
from dataclasses import replace

from .model import InputError
from .overlap import (OverlapBudget, concurrency_score, execution_events, overlap,
                      validate_durations)


class DurationObjective:
    def __init__(self, records, start, end, durations, work):
        validate_durations(durations, {r.id for r in records})
        if any(durations.get(r.id) is None for r in records if r.enabled and r.supported):
            raise InputError('concurrency objective requires known durations for every enabled supported schedule')
        self.records, self.start, self.end, self.durations = records, start, end, durations
        self.budget = OverlapBudget(work)
        # Reuse overlap horizon, identity, record and report validation too.
        self.before = overlap(records, start, end, durations, budget=self.budget)
        self.events, self.originals = Counter(), {}
        for record in records:
            if record.enabled and record.supported:
                own, firings, _ = self.enumerate(record)
                self.originals[record.id] = own, firings
                self.events.update(own)
        self.before_score = self.score(self.events)
        self.base = None

    def enumerate(self, record):
        return execution_events(record, self.start, self.end, self.durations[record.id], self.budget)

    def score(self, events):
        # Charge both copying/scanning and sorting; bounded without a seconds grid.
        self.budget.work.score(len(events) * (1 + len(events).bit_length()))
        return concurrency_score(events)

    def begin(self, record):
        self.base = self.events.copy()
        self.base.subtract(self.originals[record.id][0])
        self.base = Counter({t: delta for t, delta in self.base.items() if delta})
        return self.score(self.events)

    def candidate(self, record, cron):
        own, firings, _ = self.enumerate(replace(record, expr=cron.expr))
        # Also preserve exact-second horizon starts, including historical offsets.
        if firings != self.originals[record.id][1]:
            return None
        changed = self.base.copy()
        changed.update(own)
        changed = Counter({t: delta for t, delta in changed.items() if delta})
        return self.score(changed), changed

    def finish(self, plans):
        lookup = {p['id']: p for p in plans}
        proposed = [replace(r, expr=lookup[r.id]['proposed_expr'],
                            source_expr=lookup[r.id]['proposed_source_expr']) if r.id in lookup else r
                    for r in self.records]
        after = overlap(proposed, self.start, self.end, self.durations, budget=self.budget)
        return {'before': self.before, 'after': after,
                'before_squared_job_seconds': self.before_score[1],
                'after_squared_job_seconds': self.score(self.events)[1]}
