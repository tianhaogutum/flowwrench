"""Seeded redistributable n8n-like SQLite data; no upstream data/code."""
from datetime import datetime, timedelta, timezone
import json
import random
import sqlite3

from .storage import DietError, output_target, staged_output

# Independent schema subset based on public table/column interface facts.
SCHEMA = '''
PRAGMA page_size=4096;
PRAGMA auto_vacuum=NONE;
PRAGMA foreign_keys=ON;
CREATE TABLE workflow_entity (id VARCHAR(36) PRIMARY KEY NOT NULL, name VARCHAR(128) NOT NULL);
CREATE TABLE execution_entity (
 id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
 workflowId VARCHAR(36) NOT NULL REFERENCES workflow_entity(id) ON DELETE CASCADE,
 finished BOOLEAN NOT NULL, mode VARCHAR NOT NULL, startedAt DATETIME, stoppedAt DATETIME,
 status VARCHAR NOT NULL, deletedAt DATETIME(3), createdAt DATETIME(3) NOT NULL,
 storedAt VARCHAR(2) NOT NULL DEFAULT 'db', jsonSizeBytes BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE execution_data (
 executionId INT PRIMARY KEY NOT NULL REFERENCES execution_entity(id) ON DELETE CASCADE,
 workflowData TEXT NOT NULL, data TEXT NOT NULL, workflowVersionId VARCHAR(36)
);
CREATE INDEX execution_workflow ON execution_entity(workflowId);
CREATE INDEX execution_stopped_status ON execution_entity(stoppedAt,status);
'''


def generate(out, *, seed=42, executions=2000, deleted_percent=60, payload_bytes=8192, overwrite=False):
    if not 0 <= executions <= 200000:
        raise DietError('--executions must be between 0 and 200000')
    if not 0 <= deleted_percent <= 100:
        raise DietError('--deleted-percent must be between 0 and 100')
    if not 32 <= payload_bytes <= 1048576:
        raise DietError('--payload-bytes must be between 32 and 1048576')
    target = output_target(out, overwrite=overwrite)
    rng = random.Random(seed)
    anchor = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    with staged_output(target, overwrite=overwrite) as staging:
        conn = sqlite3.connect(staging)
        try:
            conn.executescript(SCHEMA)
            for i in range(5):
                conn.execute('INSERT INTO workflow_entity VALUES (?,?)', (f'wf-{i+1}', f'Synthetic workflow {i+1}'))
            for i in range(1, executions + 1):
                workflow = f'wf-{1 + rng.randrange(5)}'
                status = rng.choices(['success', 'error', 'crashed', 'canceled', 'running', 'waiting'], [65, 20, 3, 2, 5, 5])[0]
                start = anchor - timedelta(days=rng.randrange(90), seconds=i)
                stopped = (start + timedelta(seconds=1)).isoformat() if status not in ('running', 'waiting') else None
                size = rng.randint(payload_bytes // 2, payload_bytes * 2)
                token = f'synthetic-{rng.getrandbits(64):016x}-'
                data = json.dumps({'result': (token * (size // len(token) + 1))[:size]}, separators=(',', ':'))
                workflow_data = json.dumps({'id': workflow, 'nodes': [], 'connections': {}}, separators=(',', ':'))
                conn.execute('INSERT INTO execution_entity(id,workflowId,finished,mode,startedAt,stoppedAt,status,createdAt,jsonSizeBytes) VALUES (?,?,?, ?,?,?,?,?,?)',
                             (i, workflow, stopped is not None, 'manual', start.isoformat(), stopped, status, start.isoformat(), len(data.encode()) + len(workflow_data.encode())))
                conn.execute('INSERT INTO execution_data VALUES (?,?,?,NULL)', (i, workflow_data, data))
            conn.commit()
            deleted = executions * deleted_percent // 100
            conn.executemany('DELETE FROM execution_entity WHERE id=?', ((i,) for i in sorted(rng.sample(range(1, executions + 1), deleted))))
            conn.commit()
            free = conn.execute('PRAGMA freelist_count').fetchone()[0]
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise DietError('generated fixture failed integrity check')
        finally:
            conn.close()
    return {'output': str(target), 'seed': seed, 'generated_executions': executions,
            'deleted_executions': deleted, 'remaining_executions': executions - deleted,
            'freelist_pages': free, 'file_bytes': target.stat().st_size}
