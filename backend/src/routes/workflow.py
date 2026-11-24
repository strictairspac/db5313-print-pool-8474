from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, backups, cases, evidence, runs
from src.schemas.workflow import CaseForm, LoginForm, ReviewForm
from src.services import domain, recovery, workflow
from src.services.security import current_user, duty, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


@router.get('/duty/backups')
def duty_backups(_=Depends(duty)):
    return recovery.list_backups()


@router.post('/duty/runs/{run_id}/backup', status_code=201)
def duty_make_backup(run_id: int, actor=Depends(duty)):
    return recovery.make_backup(run_id, actor)


@router.get('/duty/runs/{run_id}/recovery-status')
def duty_recovery_status(run_id: int, _=Depends(duty)):
    return recovery.recovery_status(run_id)


@router.post('/duty/runs/{run_id}/recover')
def duty_recover(run_id: int, actor=Depends(duty)):
    return recovery.recover(run_id, actor)
