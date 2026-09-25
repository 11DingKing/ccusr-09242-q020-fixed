"""预警处置闭环接口。

身份与角色通过请求头传入（与内部教学系统网关联动）：
- X-User-Id / X-User-Name：操作人
- X-User-Role：operator（学院处理人）/ reviewer（复核人）/ manager（就业管理处）
- X-College-Id：operator/reviewer 所属学院，用于责任范围隔离
"""

from typing import Optional, List

from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy.orm import Session

from app.core import get_db
from app.models import (
    WarningDisposition,
    DispositionActionLog,
    DispositionCycle,
    WarningRecurrence,
    Warning,
)
from app.schemas import (
    DispositionDispatch,
    ClaimRequest,
    RequestMaterialBody,
    ReceiveMaterialBody,
    SubmitReviewBody,
    ReviewBody,
    TransferBody,
    ReasonBody,
    DispositionUpdate,
    EvidenceCreate,
    RecurrenceCreate,
    DispositionDetail,
    DispositionListResponse,
    ActionLogItem,
    CycleItem,
    EvidenceItem,
    TodoResponse,
    ActionResult,
    RecurrenceItem,
    RecurrenceOriginBrief,
)
from app.services.workflow_rules import Action
from app.services import disposition_service as svc

router = APIRouter(tags=["预警处置闭环"])

VALID_ROLES = {"operator", "reviewer", "manager"}


def get_actor(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(None, alias="X-User-Name"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_college_id: Optional[int] = Header(None, alias="X-College-Id"),
) -> svc.Actor:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="缺少操作人身份（X-User-Id）")
    if x_user_role not in VALID_ROLES:
        raise HTTPException(status_code=401, detail="缺少有效角色（X-User-Role: operator/reviewer/manager）")
    return svc.Actor(
        id=x_user_id,
        name=x_user_name,
        role=x_user_role,
        college_id=x_college_id,
    )


def _handle(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except svc.WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


def _result(db: Session, disposition: WarningDisposition, actor: svc.Actor, message: str) -> ActionResult:
    return ActionResult(message=message, disposition=svc.build_detail(db, disposition, actor))


@router.post("/dispositions/dispatch", response_model=ActionResult)
def dispatch_warning(
    body: DispositionDispatch,
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    """就业管理处分派预警：责任范围 + 处理期限。"""
    disposition = _handle(
        svc.dispatch,
        db,
        warning_id=body.warning_id,
        scope=body.scope,
        actor=actor,
        due_date=body.due_date,
        duration_days=body.duration_days,
        scope_remark=body.scope_remark,
    )
    return _result(db, disposition, actor, "分派成功")


@router.get("/dispositions", response_model=DispositionListResponse)
def list_dispositions(
    state: Optional[str] = Query(None, description="处置状态"),
    scope: Optional[str] = Query(None, description="责任范围"),
    warning_id: Optional[int] = Query(None),
    assignee_id: Optional[str] = Query(None),
    overdue_only: bool = Query(False),
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    dispositions = _handle(
        svc.list_dispositions,
        db, actor,
        state=state, scope=scope, warning_id=warning_id,
        overdue_only=overdue_only, assignee_id=assignee_id,
    )
    items = [svc.build_detail(db, d, actor) for d in dispositions]
    overdue_count = sum(1 for i in items if i.overdue)
    return DispositionListResponse(total=len(items), overdue_count=overdue_count, data=items)


@router.get("/dispositions/{disposition_id}", response_model=DispositionDetail)
def get_disposition(
    disposition_id: int,
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.id == disposition_id
    ).first()
    if not disposition:
        raise HTTPException(status_code=404, detail="处置单不存在")
    return _handle(svc.build_detail, db, disposition, actor)


@router.patch("/dispositions/{disposition_id}", response_model=DispositionDetail)
def patch_disposition(
    disposition_id: int,
    body: DispositionUpdate,
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    """维护根因、改进措施、责任范围补充说明。"""
    disposition = _handle(
        svc.update_fields, db, disposition_id, actor,
        body.model_dump(exclude_unset=True),
    )
    return _handle(svc.build_detail, db, disposition, actor)


def _action_endpoint(db: Session, disposition_id: int, actor: svc.Actor, action: Action, payload: dict):
    disposition = _handle(svc.perform_action, db, disposition_id, action, actor, payload)
    return _result(db, disposition, actor, f"动作 {action.value} 完成")


@router.post("/dispositions/{disposition_id}/claim", response_model=ActionResult)
def claim(disposition_id: int, body: ClaimRequest = ClaimRequest(), db: Session = Depends(get_db),
          actor: svc.Actor = Depends(get_actor)):
    payload = {"assignee_id": actor.id, "lease_days": body.lease_days}
    return _action_endpoint(db, disposition_id, actor, Action.CLAIM, payload)


@router.post("/dispositions/{disposition_id}/request-material", response_model=ActionResult)
def request_material(disposition_id: int, body: RequestMaterialBody, db: Session = Depends(get_db),
                     actor: svc.Actor = Depends(get_actor)):
    payload = body.model_dump(exclude_unset=True)
    payload["material_deadline"] = body.material_deadline.isoformat() if body.material_deadline else None
    return _action_endpoint(db, disposition_id, actor, Action.REQUEST_MATERIAL, payload)


@router.post("/dispositions/{disposition_id}/receive-material", response_model=ActionResult)
def receive_material(disposition_id: int, body: ReceiveMaterialBody, db: Session = Depends(get_db),
                     actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.RECEIVE_MATERIAL,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/submit-review", response_model=ActionResult)
def submit_review(disposition_id: int, body: SubmitReviewBody, db: Session = Depends(get_db),
                  actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.SUBMIT_REVIEW,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/approve", response_model=ActionResult)
def approve(disposition_id: int, body: ReviewBody, db: Session = Depends(get_db),
            actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.APPROVE,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/return", response_model=ActionResult)
def review_return(disposition_id: int, body: ReviewBody, db: Session = Depends(get_db),
                  actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.RETURN,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/transfer", response_model=ActionResult)
def transfer(disposition_id: int, body: TransferBody, db: Session = Depends(get_db),
             actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.TRANSFER,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/cancel", response_model=ActionResult)
def cancel(disposition_id: int, body: ReasonBody, db: Session = Depends(get_db),
           actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.CANCEL,
                            body.model_dump(exclude_unset=True))


@router.post("/dispositions/{disposition_id}/reopen", response_model=ActionResult)
def reopen(disposition_id: int, body: ReasonBody, db: Session = Depends(get_db),
           actor: svc.Actor = Depends(get_actor)):
    return _action_endpoint(db, disposition_id, actor, Action.REOPEN,
                            body.model_dump(exclude_unset=True))


@router.get("/dispositions/{disposition_id}/logs", response_model=List[ActionLogItem])
def list_logs(disposition_id: int, db: Session = Depends(get_db),
              actor: svc.Actor = Depends(get_actor)):
    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.id == disposition_id
    ).first()
    if not disposition:
        raise HTTPException(status_code=404, detail="处置单不存在")
    warning = db.query(Warning).filter(Warning.id == disposition.warning_id).first()
    _handle(svc.ensure_college_scope, db, actor, warning)
    return db.query(DispositionActionLog).filter(
        DispositionActionLog.disposition_id == disposition_id
    ).order_by(DispositionActionLog.id).all()


@router.get("/dispositions/{disposition_id}/cycles", response_model=List[CycleItem])
def list_cycles(disposition_id: int, db: Session = Depends(get_db),
                actor: svc.Actor = Depends(get_actor)):
    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.id == disposition_id
    ).first()
    if not disposition:
        raise HTTPException(status_code=404, detail="处置单不存在")
    warning = db.query(Warning).filter(Warning.id == disposition.warning_id).first()
    _handle(svc.ensure_college_scope, db, actor, warning)
    return db.query(DispositionCycle).filter(
        DispositionCycle.disposition_id == disposition_id
    ).order_by(DispositionCycle.cycle_no).all()


@router.post("/dispositions/{disposition_id}/evidences", response_model=EvidenceItem)
def add_evidence(disposition_id: int, body: EvidenceCreate, db: Session = Depends(get_db),
                 actor: svc.Actor = Depends(get_actor)):
    return _handle(
        svc.add_evidence, db, disposition_id, actor, body.model_dump(exclude_unset=True)
    )


@router.get("/todos", response_model=TodoResponse)
def my_todos(
    reason: Optional[str] = Query(None, description="overdue/waiting_review/lease_expired/unassigned_overdue"),
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    """实时待办（逾期/待复核/租约过期），重启后可直接查询。"""
    todos = _handle(svc.build_todos, db, actor, reason)
    overdue_count = sum(1 for t in todos if "overdue" in t.reasons or "unassigned_overdue" in t.reasons)
    return TodoResponse(total=len(todos), overdue_count=overdue_count, data=todos)


@router.post("/recurrences", response_model=RecurrenceItem)
def create_recurrence(body: RecurrenceCreate, db: Session = Depends(get_db),
                      actor: svc.Actor = Depends(get_actor)):
    link = _handle(
        svc.link_recurrence,
        db,
        warning_id=body.warning_id,
        origin_warning_id=body.origin_warning_id,
        reason=body.reason,
        actor=actor,
    )
    return _recurrence_item(db, link)


@router.get("/recurrences", response_model=List[RecurrenceItem])
def list_recurrences(
    warning_id: Optional[int] = Query(None),
    origin_warning_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    actor: svc.Actor = Depends(get_actor),
):
    query = db.query(WarningRecurrence)
    if warning_id:
        query = query.filter(WarningRecurrence.warning_id == warning_id)
    if origin_warning_id:
        query = query.filter(WarningRecurrence.origin_warning_id == origin_warning_id)
    links = query.order_by(WarningRecurrence.id).all()
    result = []
    for link in links:
        warning = db.query(Warning).filter(Warning.id == link.warning_id).first()
        if actor.role != "manager" and svc.target_college_id(db, warning) != actor.college_id:
            continue
        result.append(_recurrence_item(db, link))
    return result


def _recurrence_item(db: Session, link: WarningRecurrence) -> RecurrenceItem:
    origin_disp = db.query(WarningDisposition).filter(
        WarningDisposition.warning_id == link.origin_warning_id
    ).first()
    cycles = []
    if origin_disp:
        cycles = db.query(DispositionCycle).filter(
            DispositionCycle.disposition_id == origin_disp.id
        ).order_by(DispositionCycle.cycle_no.desc()).all()
    origin = db.query(Warning).filter(Warning.id == link.origin_warning_id).first()
    latest = cycles[0] if cycles else None
    brief = RecurrenceOriginBrief(
        origin_warning_id=link.origin_warning_id,
        origin_target_name=origin.target_name if origin else "未知",
        origin_disposition_id=origin_disp.id if origin_disp else 0,
        closed_cycles=len(cycles),
        last_closed_at=latest.closed_at if latest else None,
        last_root_cause=latest.root_cause if latest else None,
        last_improvement=latest.improvement if latest else None,
        reason=link.reason,
    )
    return RecurrenceItem(
        id=link.id,
        warning_id=link.warning_id,
        origin_warning_id=link.origin_warning_id,
        disposition_id=link.disposition_id,
        reason=link.reason,
        created_by=link.created_by,
        created_by_name=link.created_by_name,
        created_at=link.created_at,
        origin=brief,
    )
