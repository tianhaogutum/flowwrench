"""Storage accounting and explicitly defined, non-mutating retention policies."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from .artifacts import (DEFAULT_ROW_HASH_MEMORY_BYTES, FORMAT, MAX_PLAN_BYTES, content_digests,
                        load_plan, schema_sha256, validate_max_content_bytes, validate_max_content_rows, validate_max_content_row_bytes, validate_max_content_value_bytes,
                        validate_row_hash_memory_bytes, verify_structure)
from .storage import DietError, ordinary_tables, quote, row_counts, source_database, output_target, staged_output, connect

REQUIRED = {
    'execution_entity': {'id', 'workflowId', 'status', 'startedAt', 'stoppedAt'},
    'execution_data': {'executionId', 'data', 'workflowData'},
}
TERMINAL = {'success', 'error', 'crashed', 'canceled'}
FAILED = {'error', 'crashed', 'canceled'}
PAYLOAD = ('coalesce(length(CAST(d."data" AS BLOB)),0) + '
           'coalesce(length(CAST(d."workflowData" AS BLOB)),0)')
MEASURED = ('SELECT e."id", e."workflowId", e."status", e."startedAt", e."stoppedAt", '
            + PAYLOAD + ' AS payload_bytes, d."executionId" IS NOT NULL AS has_data '
            'FROM execution_entity e LEFT JOIN execution_data d ON d."executionId"=e."id"')


def detect_schema(conn):
    tables = ordinary_tables(conn)
    missing = sorted(set(REQUIRED) - set(tables))
    if missing:
        raise DietError('unsupported schema: missing tables: ' + ', '.join(missing))
    columns = {}
    for table, expected in REQUIRED.items():
        info = conn.execute('PRAGMA table_info(' + quote(table) + ')').fetchall()
        columns[table] = {row['name'] for row in info}
        absent = expected - columns[table]
        if absent:
            raise DietError('unsupported schema: ' + table + ' missing columns: ' + ', '.join(sorted(absent)))
        key = 'id' if table == 'execution_entity' else 'executionId'
        if [row['name'] for row in info if row['pk']] != [key]:
            raise DietError('unsupported schema: ' + table + ' must have single primary key ' + key)
    if 'workflow_entity' in tables:
        info = conn.execute('PRAGMA table_info(workflow_entity)').fetchall()
        if not {'id', 'name'} <= {row['name'] for row in info}:
            raise DietError('unsupported schema: workflow_entity requires id and name')
        if [row['name'] for row in info if row['pk']] != ['id']:
            raise DietError('unsupported schema: workflow_entity must have single primary key id')
    return {'format': 'split-execution-data', 'workflow_names': 'workflow_entity' in tables,
            'external_storage_column': 'storedAt' in columns['execution_entity']}


def page_stats(conn, source):
    values = {key: conn.execute('PRAGMA ' + key).fetchone()[0]
              for key in ('page_size', 'page_count', 'freelist_count', 'auto_vacuum')}
    values['file_bytes'] = source.stat().st_size
    values['database_page_bytes'] = values['page_size'] * values['page_count']
    values['freelist_bytes'] = values['page_size'] * values['freelist_count']
    values['allocated_page_bytes'] = values['database_page_bytes'] - values['freelist_bytes']
    return values


def table_stats(conn, *, use_dbstat=True):
    tables = ordinary_tables(conn)
    counts = row_counts(conn)
    physical = {}
    mode = 'dbstat'
    if use_dbstat and conn.execute("SELECT 1 FROM sqlite_schema WHERE lower(name)='dbstat'").fetchone():
        raise DietError('unsupported schema: dbstat name shadows SQLite storage accounting')
    if use_dbstat:
        try:
            owners = {row['name']: row['tbl_name'] for row in
                      conn.execute("SELECT name,tbl_name FROM sqlite_schema WHERE type IN ('table','index')")}
            for row in conn.execute('SELECT name,sum(pgsize) AS bytes FROM dbstat GROUP BY name'):
                owner = owners.get(row['name'], row['name'])
                physical[owner] = physical.get(owner, 0) + row['bytes']
        except sqlite3.OperationalError as exc:
            if 'no such table: dbstat' not in str(exc) and 'no such module: dbstat' not in str(exc):
                raise
            mode = 'value-bytes-estimate'
    else:
        mode = 'value-bytes-estimate'
    result = []
    for table in tables:
        entry = {'table': table, 'rows': counts[table], 'allocated_bytes': physical.get(table, 0) if mode == 'dbstat' else None}
        if mode != 'dbstat':
            cols = conn.execute('PRAGMA table_info(' + quote(table) + ')').fetchall()
            terms = ['coalesce(length(CAST(' + quote(col['name']) + ' AS BLOB)),0)' for col in cols]
            entry['value_bytes_estimate'] = conn.execute(
                'SELECT coalesce(sum(' + '+'.join(terms) + '),0) FROM ' + quote(table)).fetchone()[0]
        result.append(entry)
    return {'method': mode, 'includes_indexes': mode == 'dbstat', 'tables': result,
            'note': 'dbstat accounts table and index pages; fallback sums UTF-8/BLOB value bytes, excluding page/index/record overhead'}


def analyze_connection(conn, source, *, top=10, use_dbstat=True, wal_snapshot=False,
                       max_workflows=None):
    validate_max_workflows(max_workflows)
    schema = detect_schema(conn)
    query = 'WITH measured AS (' + MEASURED + ') SELECT "workflowId",status,count(*) AS n,sum(payload_bytes) AS bytes,sum(has_data) AS data_rows FROM measured GROUP BY "workflowId",status'
    if max_workflows is not None:
        # Preserve this storage-schema refusal ahead of the optional count check.
        if use_dbstat and conn.execute("SELECT 1 FROM sqlite_schema WHERE lower(name)='dbstat'").fetchone():
            raise DietError('unsupported schema: dbstat name shadows SQLite storage accounting')
        # Count exactly the keys the rollup dictionary would emit: SQLite's
        # group representatives (including collation) then Python key equality.
        # DISTINCT workflowId alone can disagree across status groups. This
        # extra aggregate scan/key set is a report-count guard, not a memory cap.
        observed = len({row['workflowId'] for row in conn.execute(query)})
        if observed > max_workflows:
            raise DietError(f'observed workflow count {observed} exceeds max_workflows {max_workflows}')
    # Select original observed keys on this locked snapshot, without catalog values
    # for unused definitions. Remove affinity/collation coercion to match dict keys;
    # IN excludes NULL, so preserve nullable catalog-key lookup explicitly.
    names = dict(conn.execute(
        'SELECT id,name FROM workflow_entity WHERE +id COLLATE BINARY IN '
        '(SELECT +"workflowId" FROM execution_entity) OR (id IS NULL AND EXISTS '
        '(SELECT 1 FROM execution_entity WHERE "workflowId" IS NULL))'
    )) if schema['workflow_names'] else {}
    grouped = {}
    for row in conn.execute(query):
        key = row['workflowId']
        item = grouped.setdefault(key, {'workflow_id': key, 'workflow_name': names.get(key),
                                       'executions': 0, 'payload_bytes': 0, 'data_rows': 0, 'statuses': {}})
        item['executions'] += row['n']
        item['data_rows'] += row['data_rows']
        item['payload_bytes'] += row['bytes']
        item['statuses'][row['status']] = row['n']
    workflows = sorted(grouped.values(), key=lambda item: (-item['payload_bytes'], str(item['workflow_id'])))
    largest = [dict(row) for row in conn.execute(
        'WITH measured AS (' + MEASURED + ') SELECT id,"workflowId" AS workflow_id,status,payload_bytes FROM measured ORDER BY payload_bytes DESC,id DESC LIMIT ?', (top,))]
    orphan = conn.execute('SELECT count(*),coalesce(sum(' + PAYLOAD + '),0) FROM execution_data d LEFT JOIN execution_entity e ON e.id=d."executionId" WHERE e.id IS NULL').fetchone()
    external = {}
    if schema['external_storage_column']:
        external = {str(row[0]): row[1] for row in conn.execute('SELECT "storedAt",count(*) FROM execution_entity GROUP BY "storedAt"')}
    stats = page_stats(conn, source)
    return {'schema': schema, 'storage': stats, 'wal_snapshot': wal_snapshot,
            'table_storage': table_stats(conn, use_dbstat=use_dbstat), 'workflows': workflows,
            'largest_executions': largest,
            'totals': {'executions': sum(w['executions'] for w in workflows),
                       'payload_bytes': sum(w['payload_bytes'] for w in workflows),
                       'missing_data_rows': sum(w['executions'] - w['data_rows'] for w in workflows),
                       'orphan_data_rows': orphan[0], 'orphan_payload_bytes': orphan[1],
                       'storage_locations': external},
            'notes': ['Payload bytes are exact stored data + workflowData byte lengths, not disk allocations or external binary files.',
                      'Freelist bytes are exact free-page capacity; final VACUUM size can also change due to B-tree repacking.',
                      'Freelist is already-deleted space. Plan never changes records; reclaim never applies a plan.']}


def analyze(path, *, top=10, force=False, use_dbstat=True, max_workflows=None):
    validate_max_workflows(max_workflows)
    with source_database(path, force=force) as (conn, source, wal):
        return analyze_connection(conn, source, top=top, use_dbstat=use_dbstat,
                                  wal_snapshot=wal, max_workflows=max_workflows)


def parse_time(value, label='timestamp'):
    if not isinstance(value, str):
        raise DietError(label + ' must be an ISO-8601 timestamp')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if 'T' not in value and ' ' not in value:
            raise ValueError('date without time')
        return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise DietError(label + ' must be an ISO-8601 timestamp (naive timestamps are UTC)') from exc


def retention_policy(values, now, label='policy'):
    """Validate explicit policy values and normalize their shared reference time."""
    if not isinstance(values, dict) or set(values) - {'max_age_days', 'max_count', 'keep_failed'}:
        raise DietError(label + ' must be an object containing only max_age_days, max_count, keep_failed')
    age, count = values.get('max_age_days'), values.get('max_count')
    failed = values.get('keep_failed', False)
    if age is None and count is None:
        raise DietError(label + ' requires max_age_days and/or max_count')
    if age is not None and (type(age) is not int or not 0 <= age <= 365000):
        raise DietError(label + ': max_age_days must be an integer from 0 to 365000')
    if count is not None and (type(count) is not int or count < 0):
        raise DietError(label + ': max_count must be a non-negative integer')
    if type(failed) is not bool:
        raise DietError(label + ': keep_failed must be a boolean')
    try:
        cutoff = now - timedelta(days=age) if age is not None else None
    except OverflowError as exc:
        raise DietError(label + ': age cutoff is outside supported datetime range') from exc
    return {'max_age_days': age, 'max_count': count, 'keep_failed': failed,
            'as_of': now.isoformat(), 'cutoff': cutoff.isoformat() if cutoff else None}


def classify_retention(rows, policy, now):
    """One (group, reason) per measured row; payloads are never loaded."""
    decisions = [None] * len(rows)
    eligible = []
    cutoff = parse_time(policy['cutoff']) if policy['cutoff'] is not None else None
    for index, row in enumerate(rows):
        if row['status'] not in TERMINAL:
            reason = 'non_terminal_or_unknown_status'
        elif policy['keep_failed'] and row['status'] in FAILED:
            reason = 'keep_failed'
        elif row['stoppedAt'] is None:
            reason = 'missing_stopped_at'
        else:
            end = parse_time(row['stoppedAt'], 'execution stoppedAt')
            if end > now:
                reason = 'future_stopped_at'
            else:
                eligible.append((end, str(row['id']), index))
                continue
        decisions[index] = ('protected', reason)
    # IDs need not be numeric; preserve plan's descending lexicographic ties.
    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for rank, (end, _, index) in enumerate(eligible):
        aged = cutoff is not None and end < cutoff
        counted = policy['max_count'] is not None and rank >= policy['max_count']
        decisions[index] = (('drop', 'both' if aged and counted else 'age' if aged else 'count')
                            if aged or counted else ('keep', 'within_limits'))
    return decisions


def plan(path, *, max_age_days=None, max_count=None, keep_failed=False, as_of=None, force=False):
    now = parse_time(as_of, '--as-of') if as_of is not None else datetime.now(timezone.utc)
    policy = retention_policy({'max_age_days': max_age_days, 'max_count': max_count,
                               'keep_failed': keep_failed}, now)
    with source_database(path, force=force) as (conn, source, wal):
        detect_schema(conn)
        rows = list(conn.execute(MEASURED))
        decisions = classify_retention(rows, policy, now)
        protected = Counter(reason for group, reason in decisions if group == 'protected')
        by_workflow = defaultdict(lambda: {'executions': 0, 'data_rows': 0, 'payload_bytes': 0})
        reasons = Counter()
        selected = data_rows = payload = 0
        for row, (group, reason) in zip(rows, decisions):
            if group != 'drop':
                continue
            selected += 1
            data_rows += row['has_data']
            payload += row['payload_bytes']
            reasons[reason] += 1
            item = by_workflow[row['workflowId']]
            item['executions'] += 1
            item['data_rows'] += row['has_data']
            item['payload_bytes'] += row['payload_bytes']
        stats = page_stats(conn, source)
        return {'dry_run': True, 'wal_snapshot': wal,
                'policy': policy,
                'executions_before': len(rows), 'eligible_executions': len(rows) - sum(protected.values()),
                'protected': dict(protected),
                'delete': {'executions': selected, 'data_rows': data_rows, 'payload_bytes': payload, 'reasons': dict(reasons)},
                'executions_after': len(rows) - selected,
                'workflows': [{'workflow_id': key, **value} for key, value in sorted(by_workflow.items(), key=lambda item: str(item[0]))],
                'storage': stats,
                'projected_size_estimate_bytes': max(stats['page_size'], stats['allocated_page_bytes'] - payload),
                'projection_is_estimate': True,
                'notes': ['Union of age and global count limits over eligible terminal executions; keep-failed excludes error/crashed/canceled from both limits.',
                          'new/running/waiting/unknown statuses, missing/future stoppedAt are protected; age is strictly before cutoff.',
                          'Count keeps newest stoppedAt, then descending lexicographic execution ID. Protected rows do not consume the count budget.',
                          'Projection = max(one page, allocated page bytes - selected payload bytes). It excludes record/index/related-table changes and B-tree packing.',
                          'Only execution_entity and execution_data are simulated. Orphan data, auxiliary tables and external files are not selected. No SQL or deletion is performed.']}


GROUPS = ('keep', 'drop', 'protected')
METRICS = ('executions', 'data_rows', 'payload_bytes')


def comparison_totals():
    """Independent zero counters for a global or workflow transition partition."""
    return {'totals': dict.fromkeys(METRICS, 0),
            'baseline': {'groups': {g: dict.fromkeys(METRICS, 0) for g in GROUPS}},
            'candidate': {'groups': {g: dict.fromkeys(METRICS, 0) for g in GROUPS}},
            'transitions': [{'from': a, 'to': b, **dict.fromkeys(METRICS, 0)}
                            for a in GROUPS for b in GROUPS]}


def validate_max_workflows(value):
    if value is not None and (type(value) is not int or value < 0):
        raise DietError('max_workflows must be a non-negative integer or None')


def validate_max_members(value):
    if value is not None and (type(value) is not int or value < 0):
        raise DietError('max_members must be a non-negative integer or None')


def compare(path, *, baseline, candidate, as_of, force=False, summary_only=False, max_workflows=None,
            max_members=None):
    """Compare two explicit policies on one locked snapshot and reference time."""
    if not isinstance(summary_only, bool):
        raise DietError('summary_only must be a boolean')
    validate_max_workflows(max_workflows)
    validate_max_members(max_members)
    now = parse_time(as_of, '--as-of')
    before = retention_policy(baseline, now, 'baseline policy')
    after = retention_policy(candidate, now, 'candidate policy')
    with source_database(path, force=force) as (conn, source, wal):
        schema = detect_schema(conn)
        rows = sorted(conn.execute(MEASURED), key=lambda row: str(row['id']))
        left = classify_retention(rows, before, now)
        right = classify_retention(rows, after, now)
        # Use the rollups' original keys, including None, only from measured executions.
        # Keep both policies' timestamp validation ahead of this report-size refusal.
        if max_workflows is not None:
            observed = len({row['workflowId'] for row in rows})
            if observed > max_workflows:
                raise DietError(f'observed workflow count {observed} exceeds max_workflows {max_workflows}')
        # Each measured execution emits one global member in full mode; summary emits none.
        # Count the same classified snapshot, including protected and missing-data rows.
        emitted = 0 if summary_only else len(rows)
        if max_members is not None and emitted > max_members:
            raise DietError(f'emitted execution member count {emitted} exceeds max_members {max_members}')
        # Filter on this same snapshot without fetching unused definitions or binding
        # one parameter per workflow. Unary + removes affinity and BINARY prevents
        # declared collations from broadening the original Python dict key match.
        # IN excludes NULL, so retain the nullable catalog-key lookup explicitly.
        names = dict(conn.execute(
            'SELECT id,name FROM workflow_entity WHERE +id COLLATE BINARY IN '
            '(SELECT +"workflowId" FROM execution_entity) OR (id IS NULL AND EXISTS '
            '(SELECT 1 FROM execution_entity WHERE "workflowId" IS NULL))'
        )) if schema['workflow_names'] else {}
        summary = comparison_totals()
        if not summary_only:
            for cell in summary['transitions']:
                cell['members'] = []
        indexes = {(a, b): i for i, (a, b) in enumerate((a, b) for a in GROUPS for b in GROUPS)}
        workflows = {}
        for row, (a, reason_a), (b, reason_b) in zip(rows, left, right):
            workflow_id = row['workflowId']
            if workflow_id not in workflows:
                workflows[workflow_id] = {'workflow_id': workflow_id, 'workflow_name': names.get(workflow_id),
                                          **comparison_totals()}
            metrics = {'executions': 1, 'data_rows': row['has_data'], 'payload_bytes': row['payload_bytes']}
            index = indexes[a, b]
            # Roll up the global decisions; count limits are never rerun per workflow.
            for aggregate in (summary, workflows[workflow_id]):
                for target in (aggregate['totals'], aggregate['transitions'][index],
                               aggregate['baseline']['groups'][a], aggregate['candidate']['groups'][b]):
                    for key, value in metrics.items():
                        target[key] += value
            if not summary_only:
                cell = summary['transitions'][index]
                cell['members'].append({'execution_id': row['id'], 'workflow_id': row['workflowId'],
                                        'data_rows': row['has_data'], 'payload_bytes': row['payload_bytes'],
                                        'baseline_reason': reason_a, 'candidate_reason': reason_b})
        for aggregate in [summary, *workflows.values()]:
            aggregate['drop_delta'] = {k: aggregate['candidate']['groups']['drop'][k]
                                     - aggregate['baseline']['groups']['drop'][k] for k in METRICS}
        summary['baseline']['policy'] = before
        summary['candidate']['policy'] = after
        if summary_only:
            summary['members_included'] = False
        return {'dry_run': True, 'wal_snapshot': wal, **summary,
                'workflows': sorted(workflows.values(), key=lambda w: (w['workflow_id'] is not None, str(w['workflow_id']))),
                'notes': ['Both policies use the same locked snapshot and explicit as-of time; plan eligibility and age/count rules apply.',
                          ('Transitions include all nine keep/drop/protected pairs; execution members are omitted.' if summary_only else
                           'Transitions include all nine keep/drop/protected pairs; members use ascending lexicographic execution IDs.'),
                          'Workflow rollups partition the global transitions without repeating members; null workflow ID sorts first, then string IDs ascending. Missing workflow names are null.',
                          'Drop delta is candidate minus baseline. Payload bytes are exact stored data + workflowData lengths, not disk savings.',
                          'Malformed stoppedAt needed by either policy aborts the entire comparison; both-side status/keep-failed protections skip timestamp parsing.',
                          'Missing data rows contribute zero bytes. Orphan data, auxiliary tables and external files are excluded. No deletion is performed.']}


def reclaim_plan(path, *, out, force=False, content=False,
                 row_hash_memory_bytes=DEFAULT_ROW_HASH_MEMORY_BYTES, max_content_value_bytes=None, max_content_row_bytes=None, max_content_rows=None, max_content_bytes=None):
    validate_row_hash_memory_bytes(row_hash_memory_bytes)
    validate_max_content_value_bytes(max_content_value_bytes)
    validate_max_content_row_bytes(max_content_row_bytes)
    validate_max_content_rows(max_content_rows)
    validate_max_content_bytes(max_content_bytes)
    fingerprint = {}
    with source_database(path, force=force, fingerprint=fingerprint) as (conn, source, wal):
        detect_schema(conn)
        target = output_target(out, source)
        if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise DietError('source integrity_check failed; no plan published')
        artifact = {'format': FORMAT, 'version': 1, 'operation': 'vacuum-into',
                    'source': fingerprint, 'schema_sha256': schema_sha256(conn),
                    'row_counts': row_counts(conn), 'storage': page_stats(conn, source)}
        if content:
            artifact.update(version=2, content=content_digests(conn, row_hash_memory_bytes=row_hash_memory_bytes,
                                                            max_content_value_bytes=max_content_value_bytes,
                                                            max_content_row_bytes=max_content_row_bytes, max_content_rows=max_content_rows, max_content_bytes=max_content_bytes))
        encoded = (json.dumps(artifact, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode('ascii')
        if len(encoded) > MAX_PLAN_BYTES:
            raise DietError('reclamation plan exceeds 1 MiB limit; no plan published')
        with staged_output(target) as staging:
            staging.write_bytes(encoded)
        return artifact


def verify_copy(path, *, plan_file, content=False, report_content_mismatches=False,
                row_hash_memory_bytes=DEFAULT_ROW_HASH_MEMORY_BYTES, max_content_value_bytes=None, max_content_row_bytes=None, max_content_rows=None, max_content_bytes=None):
    validate_row_hash_memory_bytes(row_hash_memory_bytes)
    validate_max_content_value_bytes(max_content_value_bytes)
    validate_max_content_row_bytes(max_content_row_bytes)
    validate_max_content_rows(max_content_rows)
    validate_max_content_bytes(max_content_bytes)
    if type(report_content_mismatches) is not bool:
        raise DietError('report-content-mismatches must be a boolean')
    try:
        artifact, _ = load_plan(plan_file)
        expected_content = artifact.get('content')
        if (content or report_content_mismatches) and expected_content is None:
            raise DietError('reclamation plan has no content digests; create a new plan with reclaim-plan --content')
        fingerprint = {}
        with source_database(path, fingerprint=fingerprint) as (conn, source, wal):
            detect_schema(conn)
            mismatches = verify_structure(conn, expected_schema=artifact['schema_sha256'], expected_counts=artifact['row_counts'],
                             expected_content=expected_content, row_hash_memory_bytes=row_hash_memory_bytes,
                             max_content_value_bytes=max_content_value_bytes,
                             max_content_row_bytes=max_content_row_bytes, max_content_rows=max_content_rows, max_content_bytes=max_content_bytes,
                             report_content_mismatches=report_content_mismatches)
            if mismatches:
                return {'output': str(source), 'verified': False, 'integrity_check': 'ok',
                        'schema_equal': True, 'row_counts_equal': True, 'content_verified': False,
                        'content_algorithm': expected_content['algorithm'], 'content_mismatches': mismatches}
            return {'output': str(source), 'verified': True, 'integrity_check': 'ok',
                    'schema_equal': True, 'row_counts_equal': True, 'row_counts': artifact['row_counts'],
                    'content_verified': expected_content is not None, 'content': expected_content,
                    'output_sha256': fingerprint['database']['sha256'],
                    'notes': [('Typed row content matches the trusted plan; digests do not authenticate the plan.' if expected_content is not None
                               else 'Structural consistency only: integrity, schema and table row counts; not row-content authentication.'),
                              'Verification does not restore the source or establish directory durability.']}
    except (sqlite3.Error, OSError) as exc:
        if report_content_mismatches:
            raise DietError('copy verification failed; no report emitted') from exc
        raise

def reclaim(path, *, out, overwrite=False, force=False, plan_file=None,
            row_hash_memory_bytes=DEFAULT_ROW_HASH_MEMORY_BYTES, max_content_value_bytes=None, max_content_row_bytes=None, max_content_rows=None, max_content_bytes=None):
    validate_row_hash_memory_bytes(row_hash_memory_bytes)
    validate_max_content_value_bytes(max_content_value_bytes)
    validate_max_content_row_bytes(max_content_row_bytes)
    validate_max_content_rows(max_content_rows)
    validate_max_content_bytes(max_content_bytes)
    artifact, artifact_path = load_plan(plan_file) if plan_file is not None else (None, None)
    expected_content = artifact.get('content') if artifact is not None else None
    fingerprint = {} if artifact is not None else None
    with source_database(path, force=force, fingerprint=fingerprint) as (conn, source, wal):
        detect_schema(conn)
        expected_schema = schema_sha256(conn)
        counts = row_counts(conn)
        if artifact is not None:
            if (artifact['source'] != fingerprint or artifact['schema_sha256'] != expected_schema
                    or artifact['row_counts'] != counts
                    or (expected_content is not None and content_digests(conn, row_hash_memory_bytes=row_hash_memory_bytes,
                                                                            max_content_value_bytes=max_content_value_bytes,
                                                                            max_content_row_bytes=max_content_row_bytes, max_content_rows=max_content_rows, max_content_bytes=max_content_bytes) != expected_content)):
                raise DietError('stale reclamation plan: source fingerprint, schema, row counts or content changed; create a new plan')
            # Preserve the reusable artifact, including hard-link aliases.
            output_target(out, artifact_path, overwrite)
        target = output_target(out, source, overwrite)
        before = page_stats(conn, source)
        if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise DietError('source integrity_check failed; no output published')
        # VACUUM cannot run in a transaction. Source locks remain held throughout.
        conn.rollback()
        conn.execute('PRAGMA query_only=OFF')
        recovery_hint = ('use verify-copy with the saved reclamation plan before recovery' if artifact is not None
                         else 'check copy integrity, schema and row counts before recovery')
        with staged_output(target, overwrite=overwrite, recovery_hint=recovery_hint) as staging:
            # VACUUM INTO requires an empty target and supports bound filenames.
            conn.execute('VACUUM main INTO ?', (str(staging),))
            copied = connect(staging, immutable=True)
            try:
                verify_structure(copied, expected_schema=expected_schema, expected_counts=counts,
                                 expected_content=expected_content, row_hash_memory_bytes=row_hash_memory_bytes,
                                 max_content_value_bytes=max_content_value_bytes,
                                 max_content_row_bytes=max_content_row_bytes, max_content_rows=max_content_rows, max_content_bytes=max_content_bytes)
                after = page_stats(copied, staging)
            finally:
                copied.close()
        return {'output': str(target), 'wal_snapshot': wal, 'source_modified': False,
                'plan_verified': artifact is not None,
                'content_verified': expected_content is not None,
                'before': before, 'after': after, 'saved_bytes': before['file_bytes'] - after['file_bytes'],
                'logical_page_bytes_saved': before['database_page_bytes'] - after['database_page_bytes'],
                'integrity_check': 'ok', 'row_counts_equal': True, 'row_counts': counts,
                'notes': ['Only existing free space is reclaimed; execution rows are unchanged.',
                          'Do not replace a live n8n database. Stop n8n and follow a separate backup/restore procedure.',
                          'With WAL snapshots file-byte savings exclude the WAL; logical page-byte savings compare the committed database.']}
