import hashlib

from sqlalchemy import func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.config.settings import IS_MYSQL, IS_SQLITE, engine
from src.constants.workflow import Role, Status
from src.models.tables import audit, backups, cases, evidence, runs, schema, users
from src.services.domain import SPEC, solve
from src.services.security import hash_password
from src.utils.encoding import canonical
from src.utils.time import utc_time

# 备份恢复演练数据使用固定高编号，避免与正常演示流程新建的记录争用主键。
RECOVERY_CASE_ID = 9001
RESTORE_RUN_ID = 9101
COMPLETE_RUN_ID = 9102
CONFLICT_RUN_ID = 9103
RESTORE_EVIDENCE_ID = 9201
COMPLETE_EVIDENCE_ID = 9202
CONFLICT_EVIDENCE_ID = 9203


def keep_existing(conn, table, key, values):
    if IS_MYSQL:
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(id=table.c.id)
    else:
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key])
    conn.execute(statement)


def reset_sequence(conn, table, identity):
    if IS_SQLITE:
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        )).first()
        if exists:
            conn.execute(text(
                'INSERT INTO sqlite_sequence(name, seq) VALUES (:table, :value) '
                'ON CONFLICT(name) DO UPDATE SET seq = excluded.seq'),
                {'table': table.name, 'value': identity})
    elif IS_MYSQL:
        conn.execute(text(f'ALTER TABLE {table.name} AUTO_INCREMENT = :value'),
                     {'value': identity + 1})
    else:
        conn.execute(text(
            "SELECT setval(pg_get_serial_sequence(:table, 'id'), :value, true)"),
            {'table': table.name, 'value': identity})


def _finished_run(values):
    base = {'status': Status.COMPLETE, 'owner': 'operator', 'attempt_token': None,
            'started_at': utc_time(-120), 'finished_at': utc_time(-60),
            'created_at': utc_time(-180), 'error': None}
    base.update(values)
    return base


def _seed_recovery_demo(conn):
    """构造备份恢复三场景：可恢复、原已齐全、同号不符冲突。"""
    if conn.execute(select(backups.c.id).where(backups.c.run_id == RESTORE_RUN_ID)).first():
        return

    keep_existing(conn, cases, 'slug', {'id': RECOVERY_CASE_ID, 'slug': 'recovery-demo',
        'title': '备份恢复演练档案', 'payload': SPEC['sample'], 'author': 'demo',
        'created_at': utc_time()})

    report = solve(SPEC['sample'], engine)
    stamp = utc_time(-90)
    run_values = {
        RESTORE_RUN_ID: _finished_run({'case_id': RECOVERY_CASE_ID}),
        COMPLETE_RUN_ID: _finished_run({'case_id': RECOVERY_CASE_ID}),
        CONFLICT_RUN_ID: _finished_run({'case_id': RECOVERY_CASE_ID}),
    }
    evidence_values = {
        RESTORE_RUN_ID: RESTORE_EVIDENCE_ID,
        COMPLETE_RUN_ID: COMPLETE_EVIDENCE_ID,
        CONFLICT_RUN_ID: CONFLICT_EVIDENCE_ID,
    }
    for run_id, values in run_values.items():
        keep_existing(conn, runs, 'id', {'id': run_id, **values})
        reset_sequence(conn, runs, run_id)
    for run_id, evidence_id in evidence_values.items():
        keep_existing(conn, evidence, 'id', {
            'id': evidence_id, 'run_id': run_id, 'report': report,
            'digest': hashlib.sha256(canonical(report)).hexdigest(),
            'reviewer': 'reviewer', 'review_note': '备份时点的原始签署信息',
            'reviewed_at': stamp})
        reset_sequence(conn, evidence, evidence_id)

    for run_id in run_values:
        run_row = dict(conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one())
        evidence_row = dict(conn.execute(
            select(evidence).where(evidence.c.run_id == run_id)).mappings().one())
        keep_existing(conn, backups, 'run_id', {
            'run_id': run_id, 'case_id': RECOVERY_CASE_ID,
            'run_snapshot': {key: run_row[key] for key in (
                'id', 'case_id', 'status', 'owner', 'attempt_token', 'started_at',
                'finished_at', 'created_at', 'error')},
            'evidence_snapshot': {key: evidence_row[key] for key in (
                'id', 'run_id', 'report', 'digest', 'reviewer', 'review_note', 'reviewed_at')},
            'created_at': utc_time(-30)})

    # 场景一（9101）：执行单与凭证均被误删，备份仍在，等待值班员按原编号找回。
    conn.execute(evidence.delete().where(evidence.c.id == RESTORE_EVIDENCE_ID))
    conn.execute(runs.delete().where(runs.c.id == RESTORE_RUN_ID))
    # 场景三（9103）：同号执行单被线上新内容覆盖，与备份不符，恢复须整单报冲突。
    conn.execute(runs.update().where(runs.c.id == CONFLICT_RUN_ID)
                 .values(status=Status.FAILED, error='线上重跑产生的现库内容，与备份时点不一致'))
    # 场景二（9102）：执行单与凭证齐全且与备份一致，接口应返回原已齐全。


def seed():
    with engine.begin() as conn:
        for role in Role:
            keep_existing(conn, users, 'username', {'username': role.value, 'role': role.value,
                'password_hash': hash_password('Demo-Walkthrough-2026')})
        keep_existing(conn, cases, 'slug', {'slug': 'demo', 'title': SPEC['name'],
            'payload': SPEC['sample'], 'author': 'demo', 'created_at': utc_time()})
        _seed_recovery_demo(conn)
        return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (users, cases, runs, evidence, backups, audit)}


if __name__ == '__main__':
    schema.create_all(engine)
    print(seed())
