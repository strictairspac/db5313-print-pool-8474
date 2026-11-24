"""误删办结单的备份恢复。

备份按执行单原编号保存同一时点的执行单与凭证快照。恢复在单个数据库事务内
完成：先按写入时的现库内容逐字段比对，同号记录与备份不符即整单报冲突；
执行单与凭证一起生效，事务中断后重试不会新增副本。
"""
import hashlib
import logging

from fastapi import HTTPException
from sqlalchemy import select, text

from src.config.settings import IS_MYSQL, IS_SQLITE, engine
from src.constants.workflow import RecoveryOutcome, Status
from src.models.tables import audit, backups, cases, evidence, runs
from src.utils.encoding import canonical
from src.utils.time import utc_time

# 执行单按原编号恢复时必须保持一致的字段。
RUN_FIELDS = ('case_id', 'status', 'owner', 'attempt_token', 'started_at',
              'finished_at', 'created_at', 'error')
# 凭证恢复时必须保持一致的字段（含校验码与签署信息）。
EVIDENCE_FIELDS = ('run_id', 'report', 'digest', 'reviewer', 'review_note',
                   'reviewed_at')


def _conflict(message, detail):
    raise HTTPException(409, {'message': message, 'outcome': RecoveryOutcome.CONFLICT,
                              'detail': detail})


def _snapshot(snapshot, fields, label):
    if not isinstance(snapshot, dict):
        raise HTTPException(422, f'备份{label}快照结构损坏')
    missing = [field for field in fields if field not in snapshot]
    if missing:
        raise HTTPException(422, f'备份{label}快照缺少字段：{"、".join(missing)}')
    return dict(snapshot)


def _validate_backup(data):
    """校验备份快照的编号一致性与凭证校验码，返回两份快照。"""
    run_snapshot = _snapshot(data['run_snapshot'], RUN_FIELDS, '执行单')
    if run_snapshot.get('id') != data['run_id'] or run_snapshot['case_id'] != data['case_id']:
        raise HTTPException(422, '备份执行单编号或档案编号不一致')
    evidence_snapshot = _snapshot(data['evidence_snapshot'], EVIDENCE_FIELDS, '凭证')
    if evidence_snapshot.get('id') is None:
        raise HTTPException(422, '备份凭证快照缺少原编号')
    if evidence_snapshot['run_id'] != data['run_id']:
        raise HTTPException(422, '备份凭证与执行单编号不一致')
    digest = hashlib.sha256(canonical(evidence_snapshot['report'])).hexdigest()
    if digest != evidence_snapshot['digest']:
        raise HTTPException(422, '备份凭证校验码与报告内容不符，备份已损坏或被篡改')
    return run_snapshot, evidence_snapshot


def _load_backup(conn, run_id):
    row = conn.execute(select(backups).where(backups.c.run_id == run_id)).mappings().first()
    if row is None:
        raise HTTPException(404, f'备份中找不到执行单 {run_id}')
    data = dict(row)
    run_snapshot, evidence_snapshot = _validate_backup(data)
    return data, run_snapshot, evidence_snapshot


def _differs(current, snapshot, fields):
    return [field for field in fields if current.get(field) != snapshot[field]]


def _reset_sequence(conn, table, identity):
    """按原编号显式插入后，把自增序列推到该编号之后，避免后续分配撞号。"""
    if IS_SQLITE:
        # 只有 AUTOINCREMENT 表才有 sqlite_sequence；普通 rowid 表自动取
        # max(id)+1，显式插入大编号后不会撞号，无需处理。
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


def _log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action,
                                       reference=str(reference), created_at=utc_time()))


def _response(outcome, run_id, run_snapshot=None, evidence_snapshot=None, detail=None):
    body = {'outcome': outcome, 'run_id': run_id}
    if run_snapshot is not None:
        body['run'] = run_snapshot
    if evidence_snapshot is not None:
        body['evidence'] = evidence_snapshot
    if detail:
        body['detail'] = detail
    return body


def recover(run_id, actor):
    """按原编号从备份恢复执行单与凭证，返回已恢复、原已齐全或冲突。"""
    try:
        with engine.begin() as conn:
            _, run_snapshot, evidence_snapshot = _load_backup(conn, run_id)
            current_run = conn.execute(
                select(runs).where(runs.c.id == run_id).with_for_update()).mappings().first()
            current_evidence = conn.execute(
                select(evidence).where(evidence.c.run_id == run_id)
                .with_for_update()).mappings().first()

            run_exists = current_run is not None
            evidence_exists = current_evidence is not None

            if run_exists:
                mismatched = _differs(dict(current_run), run_snapshot, RUN_FIELDS)
                if mismatched:
                    _conflict(f'执行单 {run_id} 现库内容与备份不符',
                              {'run_mismatched_fields': mismatched})
            if evidence_exists:
                mismatched = _differs(dict(current_evidence), evidence_snapshot, EVIDENCE_FIELDS)
                if mismatched:
                    _conflict(f'执行单 {run_id} 的凭证现库内容与备份不符',
                              {'evidence_mismatched_fields': mismatched})

            if run_exists and evidence_exists:
                _log(conn, actor, 'recovery_skipped', run_id)
                return _response(RecoveryOutcome.ALREADY_COMPLETE, run_id,
                                 run_snapshot, evidence_snapshot,
                                 '现库执行单与凭证齐全且与备份一致，无需恢复。')

            # 部分缺失或整单缺失：执行单与凭证在同一事务内一起补回。
            if not run_exists:
                if conn.execute(select(cases.c.id).where(
                        cases.c.id == run_snapshot['case_id'])).first() is None:
                    raise HTTPException(422, '备份引用的档案已不存在，无法恢复执行单')
                conn.execute(runs.insert().values(**run_snapshot))
                _reset_sequence(conn, runs, run_id)
            if not evidence_exists:
                conn.execute(evidence.insert().values(**evidence_snapshot))
                _reset_sequence(conn, evidence, evidence_snapshot['id'])
            _log(conn, actor, 'recovery_restore', run_id)
            return _response(RecoveryOutcome.RESTORED, run_id,
                             run_snapshot, evidence_snapshot,
                             '执行单与凭证已按备份时点一起恢复，原编号、校验码和签署信息照旧。')
    except HTTPException as exc:
        if getattr(exc, 'status_code', None) == 409:
            # 冲突事务已整体回滚，另起事务留痕，便于值班组追查。
            try:
                with engine.begin() as audit_conn:
                    _log(audit_conn, actor, 'recovery_conflict', run_id)
            except Exception:
                logging.exception('recovery_conflict 审计写入失败：%s', run_id)
        raise


def recovery_status(run_id):
    """值班接口按单号返回恢复状态，不修改任何数据。"""
    with engine.connect() as conn:
        backup = conn.execute(select(backups).where(backups.c.run_id == run_id)).mappings().first()
        if backup is None:
            return _response(RecoveryOutcome.NOT_FOUND, run_id,
                             detail='备份中没有该执行单。')
        backup = dict(backup)
        run_snapshot, evidence_snapshot = _validate_backup(backup)
        current_run = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
        current_evidence = conn.execute(
            select(evidence).where(evidence.c.run_id == run_id)).mappings().first()

        run_ok = current_run is not None and not _differs(
            dict(current_run), run_snapshot, RUN_FIELDS)
        evidence_ok = current_evidence is not None and not _differs(
            dict(current_evidence), evidence_snapshot, EVIDENCE_FIELDS)

        if run_ok and evidence_ok:
            return _response(RecoveryOutcome.ALREADY_COMPLETE, run_id,
                             run_snapshot, evidence_snapshot,
                             '现库执行单与凭证齐全且与备份一致。')
        if (current_run is not None and not run_ok) or \
           (current_evidence is not None and not evidence_ok):
            detail = {}
            if current_run is not None and not run_ok:
                detail['run_mismatched_fields'] = _differs(
                    dict(current_run), run_snapshot, RUN_FIELDS)
            if current_evidence is not None and not evidence_ok:
                detail['evidence_mismatched_fields'] = _differs(
                    dict(current_evidence), evidence_snapshot, EVIDENCE_FIELDS)
            return _response(RecoveryOutcome.CONFLICT, run_id, detail=detail)
        return _response(RecoveryOutcome.RECOVERABLE, run_id,
                         run_snapshot, evidence_snapshot,
                         '现库记录缺失，可按备份恢复。')


def list_backups():
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(backups.c.id, backups.c.run_id, backups.c.case_id, backups.c.created_at)
            .order_by(backups.c.run_id.desc()).limit(100)).mappings()]


def snapshot_bundle(conn, run_id):
    """从现库导出某条办结执行单与其凭证的同时点快照。"""
    run_row = conn.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    if run_row is None:
        raise HTTPException(404, '找不到该执行单')
    evidence_row = conn.execute(
        select(evidence).where(evidence.c.run_id == run_id)).mappings().first()
    if evidence_row is None:
        raise HTTPException(422, '该执行单尚无凭证，不能备份办结单')
    run_snapshot = {field: run_row[field] for field in RUN_FIELDS}
    run_snapshot['id'] = run_row['id']
    evidence_snapshot = {field: evidence_row[field] for field in EVIDENCE_FIELDS}
    evidence_snapshot['id'] = evidence_row['id']
    if run_snapshot['status'] not in (Status.COMPLETE, Status.FAILED):
        raise HTTPException(422, '执行单尚未办结，不能纳入备份')
    return run_snapshot, evidence_snapshot


def make_backup(run_id, actor):
    with engine.begin() as conn:
        run_snapshot, evidence_snapshot = snapshot_bundle(conn, run_id)
        existing = conn.execute(
            select(backups.c.id).where(backups.c.run_id == run_id)).first()
        if existing is not None:
            raise HTTPException(409, f'执行单 {run_id} 的备份已存在')
        backup_id = conn.execute(backups.insert().values(
            run_id=run_id, case_id=run_snapshot['case_id'],
            run_snapshot=run_snapshot, evidence_snapshot=evidence_snapshot,
            created_at=utc_time())).inserted_primary_key[0]
        _log(conn, actor, 'backup_create', run_id)
        row = conn.execute(select(backups).where(backups.c.id == backup_id)).mappings().first()
        return dict(row)
