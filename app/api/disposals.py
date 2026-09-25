from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.core import get_db
from app.core.security import CurrentUser, get_current_user
from app.models import (
    Warning,
    DisposalCase,
    DisposalState,
)
from app.schemas import (
    DisposalDispatchRequest,
    DisposalProgressRequest,
    DisposalEvidenceCreate,
    DisposalSubmitRequest,
    DisposalReviewRequest,
    DisposalReassignRequest,
    DisposalRecurRequest,
    DisposalCase as DisposalCaseSchema,
    DisposalCaseSummary,
    DisposalCaseListResponse,
    DisposalTodoResponse,
    DisposalTodoItem,
    DisposalActionResult,
    DisposalEvidence as DisposalEvidenceSchema,
)
from app.services import disposal_service as svc
from app.services.workflow_rules import CaseState

router = APIRouter(tags=["预警处置闭环"])


def _case_state_value(case: DisposalCase) -> CaseState:
    return CaseState(case.state.value)


def _summary(db: Session, case: DisposalCase, user: CurrentUser, now: datetime,
             reasons: Optional[List[str]] = None) -> DisposalTodoItem:
    warning = db.query(Warning).filter(Warning.id == case.warning_id).first()
    evidence_count = len(case.evidences)
    overdue = svc.is_overdue(
        deadline=case.deadline, state=_case_state_value(case), now=now
    )
    can_view = True
    if user.is_operator and case.college_id != user.college_id:
        can_view = False

    kwargs = dict(
        id=case.id,
        case_no=case.case_no,
        warning_id=case.warning_id,
        state=case.state.value,
        cycle_no=case.cycle_no,
        college_id=case.college_id,
        college_name=case.college_name,
        assignee_id=case.assignee_id,
        assignee_name=case.assignee_name,
        claimed_by=case.claimed_by,
        claimed_at=case.claimed_at,
        sla_days=case.sla_days,
        deadline=case.deadline,
        root_cause=case.root_cause,
        improvement_action=case.improvement_action,
        submitted_by=case.submitted_by,
        submitted_at=case.submitted_at,
        reviewed_by=case.reviewed_by,
        review_comment=case.review_comment,
        review_result=case.review_result,
        parent_case_id=case.parent_case_id,
        recur_reason=case.recur_reason,
        closed_at=case.closed_at,
        closed_end_year=case.closed_end_year,
        closed_current_value=case.closed_current_value,
        created_at=case.created_at,
        updated_at=case.updated_at,
        warning_type=warning.warning_type.value if warning else None,
        warning_level=warning.warning_level.value if warning else None,
        warning_status=warning.status.value if warning else None,
        target_type=warning.target_type if warning else None,
        target_id=warning.target_id if warning else None,
        target_name=warning.target_name if warning else None,
        evidence_count=evidence_count,
        overdue=overdue,
        can_view=can_view,
    )
    if reasons is not None:
        kwargs["todo_reasons"] = reasons
        kwargs["available_actions"] = svc.actions_for(case, user)
    else:
        kwargs["todo_reasons"] = []
        kwargs["available_actions"] = []
    return DisposalTodoItem(**kwargs)


# ---------------------------------------------------------------------------
# 分派与复发（挂在预警下）
# ---------------------------------------------------------------------------

@router.post("/warnings/{warning_id}/disposal/dispatch", response_model=DisposalActionResult)
def dispatch_warning(
    warning_id: int,
    payload: DisposalDispatchRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    warning = svc.get_warning_or_404(db, warning_id)
    case = svc.dispatch(db, warning, payload, user)
    return DisposalActionResult(message="分派成功", case=case, warning_status=warning.status.value)


@router.post("/warnings/{warning_id}/disposal/recur", response_model=DisposalActionResult)
def recur_warning(
    warning_id: int,
    payload: DisposalRecurRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    warning = svc.get_warning_or_404(db, warning_id)
    case = svc.latest_case(db, warning_id)
    if not case:
        raise HTTPException(status_code=409, detail="该预警尚未形成闭环，不能登记复发")
    new_case = svc.recur(db, warning, case, payload, user)
    return DisposalActionResult(message="已登记复发并开启新一轮处置", case=new_case,
                                warning_status=warning.status.value)


@router.get("/warnings/{warning_id}/disposal", response_model=List[DisposalCaseSummary])
def list_warning_cases(
    warning_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    svc.get_warning_or_404(db, warning_id)
    now = datetime.now()
    cases = (
        db.query(DisposalCase)
        .filter(DisposalCase.warning_id == warning_id)
        .order_by(DisposalCase.cycle_no.asc())
        .all()
    )
    result = []
    for case in cases:
        if user.is_operator and case.college_id != user.college_id:
            continue
        result.append(_summary(db, case, user, now))
    return result


# ---------------------------------------------------------------------------
# 处置单动作
# ---------------------------------------------------------------------------

@router.post("/disposals/{case_id}/claim", response_model=DisposalActionResult)
def claim_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.claim(db, case, user)
    return DisposalActionResult(message="认领成功", case=case, warning_status=warning.status.value)


@router.put("/disposals/{case_id}/progress", response_model=DisposalActionResult)
def update_progress(
    case_id: int,
    payload: DisposalProgressRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.update_progress(db, case, payload, user)
    return DisposalActionResult(message="处置信息已更新", case=case,
                                warning_status=warning.status.value)


@router.post("/disposals/{case_id}/evidences", response_model=DisposalEvidenceSchema)
def add_evidence(
    case_id: int,
    payload: DisposalEvidenceCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    return svc.add_evidence(db, case, payload, user)


@router.post("/disposals/{case_id}/submit-review", response_model=DisposalActionResult)
def submit_review(
    case_id: int,
    payload: DisposalSubmitRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.submit_review(db, warning, case, payload, user)
    return DisposalActionResult(message="已提交复核", case=case,
                                warning_status=warning.status.value)


@router.post("/disposals/{case_id}/approve", response_model=DisposalActionResult)
def approve_case(
    case_id: int,
    payload: DisposalReviewRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.review(db, warning, case, True, payload.review_comment, user)
    return DisposalActionResult(message="复核通过，处置闭环", case=case,
                                warning_status=warning.status.value)


@router.post("/disposals/{case_id}/return", response_model=DisposalActionResult)
def return_case(
    case_id: int,
    payload: DisposalReviewRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.review(db, warning, case, False, payload.review_comment, user)
    return DisposalActionResult(message="复核退回，继续整改", case=case,
                                warning_status=warning.status.value)


@router.post("/disposals/{case_id}/reassign", response_model=DisposalActionResult)
def reassign_case(
    case_id: int,
    payload: DisposalReassignRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    warning = svc.get_warning_or_404(db, case.warning_id)
    case = svc.reassign(db, warning, case, payload, user)
    return DisposalActionResult(message="改派成功", case=case,
                                warning_status=warning.status.value)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

@router.get("/disposals/todo", response_model=DisposalTodoResponse)
def my_todo(
    overdue_only: bool = Query(False, description="仅返回逾期事项"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """查询期待办：逾期按 deadline 实时计算，服务重启后结果不变（无后台进程）。"""
    now = datetime.now()
    pairs = svc.todo_for_user(db, user, now=now)
    if overdue_only:
        pairs = [
            (case, reasons) for case, reasons in pairs
            if svc.is_overdue(deadline=case.deadline,
                              state=CaseState(case.state.value), now=now)
        ]
    data = [_summary(db, case, user, now, reasons=reasons) for case, reasons in pairs]
    overdue_count = sum(1 for item in data if item.overdue)
    return DisposalTodoResponse(total=len(data), overdue_count=overdue_count, data=data)


@router.get("/disposals", response_model=DisposalCaseListResponse)
def list_cases(
    state: Optional[str] = Query(None, description="处置状态: 待分派/处理中/待复核/已闭环"),
    overdue_only: bool = Query(False),
    warning_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    state_enum = None
    if state:
        try:
            state_enum = DisposalState(state)
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="非法的处置状态")
    now = datetime.now()
    cases = svc.list_cases_for_user(
        db, user,
        state=state_enum,
        overdue_only=overdue_only,
        warning_id=warning_id,
        now=now,
    )
    return DisposalCaseListResponse(
        total=len(cases),
        data=[_summary(db, case, user, now) for case in cases],
    )


@router.get("/disposals/{case_id}", response_model=DisposalCaseSchema)
def get_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    case = svc.get_case_or_404(db, case_id)
    svc._ensure_can_access_case(user, case)
    return case
