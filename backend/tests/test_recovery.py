"""备份恢复端到端测试。

无 PostgreSQL 的环境可用 SQLite 运行：
    DB_DRIVER=sqlite DB_NAME=/tmp/recovery-test.db AUTH_KEY=test-key \
        python -m tests.test_recovery

测试结束后会自行清理数据库文件。生产默认仍是 PostgreSQL + psycopg。
"""
import os
import tempfile
import threading
import uuid

DB_PATH = os.path.join(tempfile.gettempdir(), f'recovery-test-{uuid.uuid4().hex}.db')
os.environ.setdefault('DB_DRIVER', 'sqlite')
os.environ['DB_NAME'] = DB_PATH
os.environ['AUTH_KEY'] = 'test-key'
os.environ['SEED_DEMO'] = 'false'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6390/0'

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from scripts.seed_demo import (COMPLETE_RUN_ID, CONFLICT_RUN_ID, RESTORE_RUN_ID,
                               seed)  # noqa: E402
from src.config.settings import engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models.tables import audit, backups, evidence, runs, schema  # noqa: E402

PASS = 0


def check(name, condition, detail=''):
    global PASS
    assert condition, f'{name} 失败 {detail}'
    PASS += 1
    print(f'  ✓ {name}')


def login(client, username):
    response = client.post('/login', json={'username': username,
                           'password': 'Demo-Walkthrough-2026'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def main():
    schema.create_all(engine)
    seed()
    with TestClient(app) as client:
        duty_headers = login(client, 'duty')
        reviewer_headers = login(client, 'reviewer')
        viewer_headers = login(client, 'viewer')

        print('权限控制')
        response = client.get('/duty/backups')
        check('未登录不能访问值班接口', response.status_code == 401)
        response = client.get('/duty/backups', headers=viewer_headers)
        check('观察员不能访问值班接口', response.status_code == 403)
        response = client.get('/duty/backups', headers=duty_headers)
        check('值班员可读取备份清单', response.status_code == 200
              and len(response.json()) == 3)

        print('场景一：误删后按原编号恢复')
        with engine.connect() as conn:
            check('恢复前执行单已删除', conn.execute(
                select(runs.c.id).where(runs.c.id == RESTORE_RUN_ID)).first() is None)
            check('恢复前凭证已删除', conn.execute(
                select(evidence.c.id).where(evidence.c.run_id == RESTORE_RUN_ID)).first() is None)
            backup = dict(conn.execute(select(backups).where(
                backups.c.run_id == RESTORE_RUN_ID)).mappings().one())
        response = client.get(f'/duty/runs/{RESTORE_RUN_ID}/recovery-status',
                              headers=duty_headers)
        check('状态查询返回可恢复', response.status_code == 200
              and response.json()['outcome'] == 'recoverable')

        response = client.post(f'/duty/runs/{RESTORE_RUN_ID}/recover',
                               headers=duty_headers)
        body = response.json()
        check('恢复返回已恢复', response.status_code == 200
              and body['outcome'] == 'restored', response.text)
        with engine.begin() as conn:
            restored_run = dict(conn.execute(select(runs).where(
                runs.c.id == RESTORE_RUN_ID)).mappings().one())
            restored_evidence = dict(conn.execute(select(evidence).where(
                evidence.c.run_id == RESTORE_RUN_ID)).mappings().one())
        check('执行单按原编号补回', restored_run['id'] == RESTORE_RUN_ID)
        check('执行单内容与备份一致',
              {k: restored_run[k] for k in backup['run_snapshot']} == backup['run_snapshot'])
        check('凭证按原编号补回且校验码照旧',
              restored_evidence['id'] == backup['evidence_snapshot']['id']
              and restored_evidence['digest'] == backup['evidence_snapshot']['digest'])
        check('签署信息照旧',
              restored_evidence['reviewer'] == backup['evidence_snapshot']['reviewer']
              and restored_evidence['review_note'] == backup['evidence_snapshot']['review_note']
              and restored_evidence['reviewed_at'] == backup['evidence_snapshot']['reviewed_at'])

        print('幂等：中断重试不新增副本')
        before = engine.connect().execute(
            select(runs.c.id).where(runs.c.id == RESTORE_RUN_ID)).all()
        evidence_before = engine.connect().execute(
            select(evidence.c.id).where(evidence.c.run_id == RESTORE_RUN_ID)).all()
        response = client.post(f'/duty/runs/{RESTORE_RUN_ID}/recover',
                               headers=duty_headers)
        check('重试返回原已齐全', response.status_code == 200
              and response.json()['outcome'] == 'already_complete')
        with engine.connect() as conn:
            check('重试不新增执行单副本', conn.execute(
                select(runs.c.id).where(runs.c.id == RESTORE_RUN_ID)).all() == before)
            check('重试不新增凭证副本', conn.execute(
                select(evidence.c.id).where(evidence.c.run_id == RESTORE_RUN_ID)).all()
                == evidence_before)
        response = client.get(f'/duty/runs/{RESTORE_RUN_ID}/recovery-status',
                              headers=duty_headers)
        check('状态查询返回原已齐全', response.json()['outcome'] == 'already_complete')

        print('场景二：本来就齐全')
        response = client.post(f'/duty/runs/{COMPLETE_RUN_ID}/recover',
                               headers=duty_headers)
        check('齐全单据返回原已齐全且不改动', response.status_code == 200
              and response.json()['outcome'] == 'already_complete')

        print('场景三：同号记录与备份不符，整单冲突')
        response = client.get(f'/duty/runs/{CONFLICT_RUN_ID}/recovery-status',
                              headers=duty_headers)
        check('状态查询识别冲突', response.json()['outcome'] == 'conflict'
              and 'status' in response.json()['detail']['run_mismatched_fields'])
        response = client.post(f'/duty/runs/{CONFLICT_RUN_ID}/recover',
                               headers=duty_headers)
        check('恢复返回 409 冲突', response.status_code == 409
              and response.json()['detail']['outcome'] == 'conflict')
        with engine.connect() as conn:
            run = dict(conn.execute(select(runs).where(
                runs.c.id == CONFLICT_RUN_ID)).mappings().one())
            conflict_log = conn.execute(select(audit).where(
                audit.c.action == 'recovery_conflict')).mappings().all()
        check('冲突不覆盖现库内容', run['status'] == 'failed')
        check('冲突留下审计记录', len(conflict_log) == 1
              and conflict_log[0]['reference'] == str(CONFLICT_RUN_ID))

        print('不存在的单号')
        response = client.post('/duty/runs/999999/recover', headers=duty_headers)
        check('备份无此单返回 404', response.status_code == 404)
        response = client.get('/duty/runs/999999/recovery-status',
                              headers=duty_headers)
        check('状态查询返回备份无此单', response.json()['outcome'] == 'not_found')

        print('值班员备份接口')
        response = client.post(f'/duty/runs/{COMPLETE_RUN_ID}/backup',
                               headers=duty_headers)
        check('重复备份被拒', response.status_code == 409)
        with engine.begin() as conn:
            source = dict(conn.execute(select(backups).where(
                backups.c.run_id == COMPLETE_RUN_ID)).mappings().one())
            run_snapshot = dict(source['run_snapshot'])
            ev_snapshot = dict(source['evidence_snapshot'])
            run_snapshot['id'] = 9301
            ev_snapshot['id'] = 9401
            ev_snapshot['run_id'] = 9301
            conn.execute(runs.insert().values(**run_snapshot))
            conn.execute(evidence.insert().values(**ev_snapshot))
        response = client.post('/duty/runs/9301/backup', headers=duty_headers)
        check('现库办结单可纳入备份', response.status_code == 201
              and response.json()['run_id'] == 9301)
        response = client.get('/duty/runs/9301/recovery-status',
                              headers=duty_headers)
        check('新备份单据状态为原已齐全', response.json()['outcome']
              == 'already_complete')

        print('原编号恢复后新单据不撞号')
        operator_headers = login(client, 'operator')
        demo_case = client.get('/cases', headers=operator_headers).json()[0]
        new_run = client.post(f"/cases/{demo_case['id']}/runs",
                              headers=operator_headers)
        check('恢复高编号后新建执行单成功', new_run.status_code == 202)
        check('新执行单编号避开已恢复编号', new_run.json()['id'] > CONFLICT_RUN_ID)

        print('恢复期间线上照常签署，并发按现库内容比对')
        # 再造一个可恢复的未签署场景：复制 9102 的备份到新单号 9104 后删除现库记录。
        with engine.begin() as conn:
            source = dict(conn.execute(select(backups).where(
                backups.c.run_id == COMPLETE_RUN_ID)).mappings().one())
            run_snapshot = dict(source['run_snapshot'])
            ev_snapshot = dict(source['evidence_snapshot'])
            run_snapshot['id'] = 9104
            ev_snapshot['id'] = 9204
            ev_snapshot['run_id'] = 9104
            ev_snapshot['reviewer'] = None
            ev_snapshot['review_note'] = None
            ev_snapshot['reviewed_at'] = None
            conn.execute(backups.insert().values(
                run_id=9104, case_id=run_snapshot['case_id'],
                run_snapshot=run_snapshot, evidence_snapshot=ev_snapshot,
                created_at=source['created_at']))
        response = client.post('/duty/runs/9104/recover', headers=duty_headers)
        check('未签署凭证随执行单一起恢复', response.status_code == 200
              and response.json()['outcome'] == 'restored')
        evidence_id = response.json()['evidence']['id']
        response = client.post(f'/evidence/{evidence_id}/review',
                               headers=reviewer_headers, json={'note': '恢复后线上签署'})
        check('恢复后线上可正常签署', response.status_code == 200
              and response.json()['reviewer'] == 'reviewer')
        response = client.post(f'/evidence/{evidence_id}/review',
                               headers=reviewer_headers, json={'note': '再次签署应被拒绝'})
        check('重复签署被拒', response.status_code == 409)
        # 现库签署后与未签署备份不符，再次恢复必须报冲突。
        response = client.post('/duty/runs/9104/recover', headers=duty_headers)
        check('签署后再恢复按现库比对报冲突', response.status_code == 409)

        print('并发恢复与签署：同一时刻只有一方生效，无重复副本')
        with engine.begin() as conn:
            source = dict(conn.execute(select(backups).where(
                backups.c.run_id == COMPLETE_RUN_ID)).mappings().one())
            run_snapshot = dict(source['run_snapshot'])
            ev_snapshot = dict(source['evidence_snapshot'])
            run_snapshot['id'] = 9105
            ev_snapshot['id'] = 9205
            ev_snapshot['run_id'] = 9105
            ev_snapshot['reviewer'] = None
            ev_snapshot['review_note'] = None
            ev_snapshot['reviewed_at'] = None
            conn.execute(backups.insert().values(
                run_id=9105, case_id=run_snapshot['case_id'],
                run_snapshot=run_snapshot, evidence_snapshot=ev_snapshot,
                created_at=source['created_at']))
        barrier = threading.Barrier(2)
        outcomes = []

        def restore():
            barrier.wait()
            outcomes.append(('restore', client.post(
                '/duty/runs/9105/recover', headers=duty_headers).status_code))

        def sign():
            barrier.wait()
            # 先等恢复拿到锁，再尝试签署，验证串行化而不是死锁。
            import time
            time.sleep(0.2)
            response = client.post('/evidence/9205/review',
                                   headers=reviewer_headers, json={'note': '并发签署'})
            outcomes.append(('sign', response.status_code))

        threads = [threading.Thread(target=restore), threading.Thread(target=sign)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = dict(outcomes)
        check('并发恢复成功', statuses.get('restore') == 200, statuses)
        check('并发签署不报错（恢复后签署成功或检测到冲突）',
              statuses.get('sign') in (200, 409), statuses)
        with engine.connect() as conn:
            check('并发后仍只有一条执行单', len(conn.execute(
                select(runs.c.id).where(runs.c.id == 9105)).all()) == 1)
            check('并发后仍只有一条凭证', len(conn.execute(
                select(evidence.c.id).where(evidence.c.id == 9205)).all()) == 1)

        print('备份防篡改：校验码不符拒绝恢复')
        with engine.begin() as conn:
            tampered = dict(conn.execute(select(backups).where(
                backups.c.run_id == COMPLETE_RUN_ID)).mappings().one())
            evidence_snapshot = dict(tampered['evidence_snapshot'])
            evidence_snapshot['digest'] = '0' * 64
            conn.execute(backups.update().where(backups.c.run_id == COMPLETE_RUN_ID)
                         .values(evidence_snapshot=evidence_snapshot))
        response = client.get(f'/duty/runs/{COMPLETE_RUN_ID}/recovery-status',
                              headers=duty_headers)
        check('损坏备份被识别并拒绝', response.status_code == 422)

    print(f'\n全部 {PASS} 项检查通过')


if __name__ == '__main__':
    try:
        main()
    finally:
        try:
            os.remove(DB_PATH)
        except OSError:
            pass
