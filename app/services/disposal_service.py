"""预警处置闭环服务：状态推进、角色限制、不可变动作记录与查询期待办。"""

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import (
    Warning,
    College,
    DisposalCase,
    DisposalEvidence,
    DisposalActionLog,
    DisposalState,
    DisposalAction,
    WarningStatus,
    UserRole,
)
from app.services.workflow_rules import (
    CaseState,
    Action,
    WorkflowDecision,
    decide_action,
    available_actions,
    is_overdue,
)
from app.core.security import CurrentUser

# 处置单状态 -> 预警主状态
_WARNING_STATUS_BY_CASE = {
    CaseState.NEW: WarningStatus.RECURRED,
    CaseState.ASSIGNED: WarningStatus.DISPATCHED,
    CaseState.WAITING_REVIEW: WarningStatus.WAITING_REVIEW,
    CaseState.CLOSED: WarningStatus.RESOLVED,
}

_ACTION_MAP = {
    Action.DISPATCH: DisposalAction.DISPATCH,
    Action.CLAIM: DisposalAction.CLAIM,
    Action.UPDATE_PROGRESS: DisposalAction.UPDATE_PROGRESS,
    Action.ADD_EVIDENCE: DisposalAction.ADD_EVIDENCE,
    Action.SUBMIT_REVIEW: DisposalAction.SUBMIT_REVIEW,
    Action.APPROVE: DisposalAction.APPROVE,
    Action.RETURN: DisposalAction.RETURN,
    Action.REASSIGN: DisposalAction.REASSIGN,
    Action.RECUR: DisposalAction.RECUR,
}


class WorkflowError(HTTPException):
    def __init__(self, decision: WorkflowDecision):
        detail = decision.reason
        if decision.required_fields:
            detail += f"，缺少字段: {', '.join(decision.required_fields)}"
        super().__init__(status_code=409, detail=detail)


def compute_deadline(start: datetime, sla_days: int) -> datetime:
    """处理期限 = 分派时刻 + sla_days 个自然日。"""
    if sla_days < 0:
        raise ValueError("处理期限不能为负数")
    return start + timedelta(days=sla_days)


def _commit_or_conflict(db: Session, message: str) -> None:
    """提交时若撞上“每条预警至多一张未闭环处置单”的唯一约束，转为 409。"""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=message)


def _case_no(warning_id: int, cycle_no: int) -> str:
    return f"W{warning_id:06d}-C{cycle_no:02d}"


def _role_value(user: CurrentUser) -> str:
    return user.role.value


def _append_log(
    db: Session,
    case: DisposalCase,
    action: Action,
    old: Optional[CaseState],
    new: CaseState,
    user: CurrentUser,
    detail: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> DisposalActionLog:
    seq = (
        db.query(DisposalActionLog)
        .filter(DisposalActionLog.case_id == case.id)
        .count()
    ) + 1
    log = DisposalActionLog(
        case_id=case.id,
        seq=seq,
        action=_ACTION_MAP[action],
        from_state=DisposalState(old.value) if old else None,
        to_state=DisposalState(new.value),
        actor_id=user.user_id,
        actor_role=_role_value(user),
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
        created_at=now or datetime.now(),
    )
    db.add(log)
    return log


def get_warning_or_404(db: Session, warning_id: int) -> Warning:
    warning = db.query(Warning).filter(Warning.id == warning_id).first()
    if not warning:
        raise HTTPException(status_code=404, detail="预警不存在")
    return warning


def get_case_or_404(db: Session, case_id: int) -> DisposalCase:
    case = db.query(DisposalCase).filter(DisposalCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="处置单不存在")
    return case


def latest_case(db: Session, warning_id: int) -> Optional[DisposalCase]:
    return (
        db.query(DisposalCase)
        .filter(DisposalCase.warning_id == warning_id)
        .order_by(DisposalCase.cycle_no.desc())
        .first()
    )


def _resolve_college(db: Session, college_id: int) -> College:
    college = db.query(College).filter(College.id == college_id).first()
    if not college:
        raise HTTPException(status_code=400, detail="责任学院不存在")
    return college


def _ensure_can_access_case(user: CurrentUser, case: DisposalCase) -> None:
    """学院经办人只能看到本学院的处置单；管理处与复核人不受学院隔离。"""
    if user.is_operator:
        if case.college_id != user.college_id:
            raise HTTPException(status_code=403, detail="无权访问其他学院的处置单")


def _sync_warning_status(warning: Warning, case_state: CaseState) -> None:
    warning.status = _WARNING_STATUS_BY_CASE[case_state]


def _check(decision: WorkflowDecision) -> None:
    if not decision.allowed:
        raise WorkflowError(decision)


# ---------------------------------------------------------------------------
# 状态推进动作
# ---------------------------------------------------------------------------

def dispatch(db: Session, warning: Warning, payload, user: CurrentUser) -> DisposalCase:
    """就业管理处把预警分派给责任学院，开启第一轮（或复发后的新一轮）处置。"""
    if not user.is_manager:
        raise HTTPException(status_code=403, detail="只有就业管理处可以分派预警")

    college = _resolve_college(db, payload.college_id)
    case = latest_case(db, warning.id)
    now = datetime.now()

    if case is None:
        old_state = None
        new_case = DisposalCase(
            case_no=_case_no(warning.id, 1),
            warning_id=warning.id,
            state=DisposalState.ASSIGNED,
            cycle_no=1,
            college_id=college.id,
            college_name=college.name,
            assignee_id=payload.assignee_id,
            assignee_name=payload.assignee_name,
            sla_days=payload.sla_days,
            deadline=compute_deadline(now, payload.sla_days),
        )
        new_state = CaseState.ASSIGNED
        db.add(new_case)
        db.flush()
        case = new_case
    elif case.state == DisposalState.NEW:
        # 复发重开后新一轮处于待分派，此时正式分派给责任学院。
        old_state = CaseState.NEW
        decision = decide_action(
            current=old_state,
            action=Action.DISPATCH,
            role=_role_value(user),
            payload={
                "assignee_id": payload.assignee_id,
                "assignee_name": payload.assignee_name,
            },
            actor_id=user.user_id,
        )
        _check(decision)
        case.college_id = college.id
        case.college_name = college.name
        case.assignee_id = payload.assignee_id
        case.assignee_name = payload.assignee_name
        case.sla_days = payload.sla_days
        case.deadline = compute_deadline(now, payload.sla_days)
        case.state = DisposalState.ASSIGNED
        new_state = CaseState.ASSIGNED
    else:
        if case.state == DisposalState.CLOSED:
            raise HTTPException(
                status_code=409,
                detail="该预警已复核闭环；再次出现风险请先登记复发",
            )
        raise HTTPException(status_code=409, detail="该预警已有进行中的处置单，不能重复分派")

    _sync_warning_status(warning, new_state)
    _append_log(
        db, case, Action.DISPATCH, old_state, new_state, user,
        detail={
            "college_id": college.id,
            "college_name": college.name,
            "assignee_id": payload.assignee_id,
            "assignee_name": payload.assignee_name,
            "sla_days": payload.sla_days,
            "deadline": case.deadline,
            "remark": payload.remark,
        },
        now=now,
    )
    _commit_or_conflict(db, "该预警已有进行中的处置单，不能重复分派")
    db.refresh(case)
    return case


def claim(db: Session, case: DisposalCase, user: CurrentUser) -> DisposalCase:
    """并发安全认领：条件 UPDATE 保证只有一个请求成功。"""
    _ensure_can_access_case(user, case)
    current = CaseState(case.state.value)
    decision = decide_action(
        current=current,
        action=Action.CLAIM,
        role=_role_value(user),
        payload={"actor_id": user.user_id},
        actor_id=user.user_id,
        assignee_id=case.assignee_id,
    )
    _check(decision)

    now = datetime.now()
    updated = (
        db.query(DisposalCase)
        .filter(
            DisposalCase.id == case.id,
            DisposalCase.state == DisposalState.ASSIGNED,
            DisposalCase.claimed_by.is_(None),
        )
        .update(
            {DisposalCase.claimed_by: user.user_id, DisposalCase.claimed_at: now},
            synchronize_session=False,
        )
    )
    if updated == 0:
        db.rollback()
        raise HTTPException(status_code=409, detail="该处置单已被其他人认领")

    _append_log(db, case, Action.CLAIM, current, current, user, now=now)
    db.commit()
    db.refresh(case)
    return case


def update_progress(db: Session, case: DisposalCase, payload, user: CurrentUser) -> DisposalCase:
    _ensure_can_access_case(user, case)
    current = CaseState(case.state.value)
    decision = decide_action(
        current=current,
        action=Action.UPDATE_PROGRESS,
        role=_role_value(user),
        payload={
            "root_cause": payload.root_cause or case.root_cause,
            "improvement_action": payload.improvement_action or case.improvement_action,
        },
        actor_id=user.user_id,
        assignee_id=case.assignee_id,
        claimed_by=case.claimed_by,
    )
    _check(decision)

    if payload.root_cause:
        case.root_cause = payload.root_cause
    if payload.improvement_action:
        case.improvement_action = payload.improvement_action

    _append_log(
        db, case, Action.UPDATE_PROGRESS, current, current, user,
        detail={
            "root_cause": case.root_cause,
            "improvement_action": case.improvement_action,
            "remark": payload.remark,
        },
    )
    db.commit()
    db.refresh(case)
    return case


def add_evidence(db: Session, case: DisposalCase, payload, user: CurrentUser) -> DisposalEvidence:
    _ensure_can_access_case(user, case)
    current = CaseState(case.state.value)
    decision = decide_action(
        current=current,
        action=Action.ADD_EVIDENCE,
        role=_role_value(user),
        payload={"file_name": payload.file_name, "summary": payload.summary},
        actor_id=user.user_id,
        assignee_id=case.assignee_id,
        claimed_by=case.claimed_by,
    )
    _check(decision)

    evidence = DisposalEvidence(
        case_id=case.id,
        file_name=payload.file_name,
        file_key=payload.file_key,
        summary=payload.summary,
        uploaded_by=user.user_id,
    )
    db.add(evidence)
    db.flush()
    _append_log(
        db, case, Action.ADD_EVIDENCE, current, current, user,
        detail={"evidence_id": evidence.id, "file_name": payload.file_name, "summary": payload.summary},
    )
    db.commit()
    db.refresh(evidence)
    return evidence


def submit_review(db: Session, warning: Warning, case: DisposalCase, payload, user: CurrentUser) -> DisposalCase:
    _ensure_can_access_case(user, case)
    evidence_count = (
        db.query(DisposalEvidence).filter(DisposalEvidence.case_id == case.id).count()
    )
    current = CaseState(case.state.value)
    decision = decide_action(
        current=current,
        action=Action.SUBMIT_REVIEW,
        role=_role_value(user),
        payload={
            "root_cause": payload.root_cause,
            "improvement_action": payload.improvement_action,
            "summary": payload.summary,
        },
        actor_id=user.user_id,
        assignee_id=case.assignee_id,
        claimed_by=case.claimed_by,
        evidence_count=evidence_count,
    )
    _check(decision)

    now = datetime.now()
    case.root_cause = payload.root_cause
    case.improvement_action = payload.improvement_action
    case.submitted_by = user.user_id
    case.submitted_at = now
    case.state = DisposalState.WAITING_REVIEW
    case.review_result = None
    _sync_warning_status(warning, CaseState.WAITING_REVIEW)
    _append_log(
        db, case, Action.SUBMIT_REVIEW, current, CaseState.WAITING_REVIEW, user,
        detail={"summary": payload.summary},
        now=now,
    )
    db.commit()
    db.refresh(case)
    return case


def review(db: Session, warning: Warning, case: DisposalCase, approved: bool, comment: str,
           user: CurrentUser) -> DisposalCase:
    if not user.is_reviewer:
        raise HTTPException(status_code=403, detail="只有复核人可以出具复核意见")
    current = CaseState(case.state.value)
    action = Action.APPROVE if approved else Action.RETURN
    decision = decide_action(
        current=current,
        action=action,
        role=_role_value(user),
        payload={"review_comment": comment},
        actor_id=user.user_id,
        assignee_id=case.assignee_id,
        claimed_by=case.claimed_by,
    )
    _check(decision)

    now = datetime.now()
    case.reviewed_by = user.user_id
    case.review_comment = comment
    case.review_result = "approved" if approved else "returned"
    new_state = CaseState.CLOSED if approved else CaseState.ASSIGNED
    case.state = DisposalState(new_state.value)
    if approved:
        case.closed_at = now
        # 固化闭环时的风险覆盖范围，作为“是否新复发”的判定基线。
        case.closed_end_year = warning.end_year
        case.closed_current_value = warning.current_value
    _sync_warning_status(warning, new_state)
    _append_log(db, case, action, current, new_state, user,
                detail={"review_comment": comment}, now=now)
    db.commit()
    db.refresh(case)
    return case


def reassign(db: Session, warning: Warning, case: DisposalCase, payload, user: CurrentUser) -> DisposalCase:
    if not user.is_manager:
        raise HTTPException(status_code=403, detail="只有就业管理处可以改派")
    college = _resolve_college(db, payload.college_id) if payload.college_id else None
    current = CaseState(case.state.value)
    decision = decide_action(
        current=current,
        action=Action.REASSIGN,
        role=_role_value(user),
        payload={"assignee_id": payload.assignee_id, "assignee_name": payload.assignee_name},
        actor_id=user.user_id,
    )
    _check(decision)

    now = datetime.now()
    old_assignee = case.assignee_id
    if college:
        case.college_id = college.id
        case.college_name = college.name
    case.assignee_id = payload.assignee_id
    case.assignee_name = payload.assignee_name
    case.claimed_by = None
    case.claimed_at = None
    # 改派撤回复核：清空复核结论与提交信息，预警回到处理中。
    case.submitted_by = None
    case.submitted_at = None
    case.review_result = None
    new_state = CaseState.ASSIGNED
    case.state = DisposalState.ASSIGNED
    _sync_warning_status(warning, new_state)
    if payload.sla_days is not None:
        case.sla_days = payload.sla_days
        case.deadline = compute_deadline(now, payload.sla_days)
    _append_log(
        db, case, Action.REASSIGN, current, new_state, user,
        detail={
            "old_assignee_id": old_assignee,
            "new_assignee_id": payload.assignee_id,
            "new_assignee_name": payload.assignee_name,
            "college_id": case.college_id,
            "college_name": case.college_name,
            "reason": payload.reason,
        },
        now=now,
    )
    db.commit()
    db.refresh(case)
    return case


def recur(db: Session, warning: Warning, case: DisposalCase, payload, user: CurrentUser,
          *, auto: bool = False) -> DisposalCase:
    """复发重开：新建下一轮处置单并关联上一次闭环，历史记录原样保留。"""
    if not auto and not user.is_manager:
        raise HTTPException(status_code=403, detail="只有就业管理处可以登记复发")
    if case.state != DisposalState.CLOSED:
        raise HTTPException(status_code=409, detail="只有已闭环的处置单可以复发重开")

    current = CaseState(case.state.value)
    if not auto:
        decision = decide_action(
            current=current,
            action=Action.RECUR,
            role=_role_value(user),
            payload={"recur_reason": payload.recur_reason},
            actor_id=user.user_id,
        )
        _check(decision)

    now = datetime.now()
    next_cycle = case.cycle_no + 1
    assigned = bool(payload.assignee_id and payload.assignee_name)
    college = None
    if payload.college_id is not None:
        college = _resolve_college(db, payload.college_id)
    elif case.college_id is not None:
        college = db.query(College).filter(College.id == case.college_id).first()

    new_case = DisposalCase(
        case_no=_case_no(warning.id, next_cycle),
        warning_id=warning.id,
        state=DisposalState.ASSIGNED if assigned else DisposalState.NEW,
        cycle_no=next_cycle,
        college_id=college.id if college else None,
        college_name=college.name if college else None,
        assignee_id=payload.assignee_id if assigned else None,
        assignee_name=payload.assignee_name if assigned else None,
        sla_days=payload.sla_days,
        deadline=compute_deadline(now, payload.sla_days) if assigned else None,
        parent_case_id=case.id,
        recur_reason=payload.recur_reason,
    )
    db.add(new_case)
    db.flush()
    new_state = CaseState.ASSIGNED if assigned else CaseState.NEW
    warning.status = (
        WarningStatus.DISPATCHED if assigned else WarningStatus.RECURRED
    )
    actor = user
    if auto:
        # 系统自动复发：以系统账号记录，角色记为就业管理处。
        actor = _SYSTEM_USER
    _append_log(
        db, new_case, Action.RECUR, CaseState.CLOSED, new_state, actor,
        detail={"recur_reason": payload.recur_reason, "auto": auto,
                "parent_case_id": case.id},
        now=now,
    )
    _commit_or_conflict(db, "该预警已存在进行中的新一轮处置单，无需重复复发")
    db.refresh(new_case)
    return new_case


_SYSTEM_USER = CurrentUser(
    user_id="system", user_name="预警检测", role=UserRole.MANAGER,
)


def _system_recur_payload(recur_reason: str):
    """检测侧复发重开使用的最小载荷：新一轮待分派，不指定被分派人。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        recur_reason=recur_reason,
        assignee_id=None,
        assignee_name=None,
        college_id=None,
        sla_days=7,
    )


def is_new_recurrence(
    db: Session, warning: Warning, new_end_year: int, new_value: Optional[float] = None
) -> bool:
    """判断已闭环预警命中的信号是否构成“新复发”。

    只有风险覆盖到更新的届次，或同一最新届次指标继续恶化时才算复发；
    对同一批数据的重复检测保持已闭环，不会误判为复发/未解决。
    """
    case = latest_case(db, warning.id)
    if case is None or case.state != DisposalState.CLOSED:
        return False
    if case.closed_end_year is None:
        # 历史闭环单没有基线快照：保守按新届次判定。
        return new_end_year > (warning.end_year or new_end_year)
    if new_end_year > case.closed_end_year:
        return True
    if (
        new_value is not None
        and case.closed_current_value is not None
        and new_value < case.closed_current_value - 1e-9
    ):
        return True
    return False


def reopen_resolved_warning(db: Session, warning: Warning, recur_reason: str) -> bool:
    """检测再次命中已闭环预警时调用：开启新一轮待分派处置单。

    返回 True 表示已复发重开；若预警未闭环或已有进行中的处置单则返回 False。
    """
    case = latest_case(db, warning.id)
    if case is None or case.state != DisposalState.CLOSED:
        return False
    recur(db, warning, case, _system_recur_payload(recur_reason), _SYSTEM_USER, auto=True)
    return True


# ---------------------------------------------------------------------------
# 查询期待办（逾期在查询时按 deadline 计算，无后台常驻进程）
# ---------------------------------------------------------------------------
def list_cases_for_user(
    db: Session,
    user: CurrentUser,
    *,
    state: Optional[DisposalState] = None,
    overdue_only: bool = False,
    warning_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> list[DisposalCase]:
    now = now or datetime.now()
    query = db.query(DisposalCase)
    if warning_id is not None:
        query = query.filter(DisposalCase.warning_id == warning_id)
    if state is not None:
        query = query.filter(DisposalCase.state == state)

    if user.is_operator:
        query = query.filter(DisposalCase.college_id == user.college_id)
    # manager / reviewer 看全部范围（复核人在 todo 中只看待复核）。

    cases = query.order_by(DisposalCase.deadline.is_(None), DisposalCase.deadline.asc(),
                           DisposalCase.id.desc()).all()
    if overdue_only:
        cases = [
            c for c in cases
            if is_overdue(deadline=c.deadline, state=CaseState(c.state.value), now=now)
        ]
    return cases


def todo_reasons(case: DisposalCase, user: CurrentUser, now: datetime) -> list[str]:
    state = CaseState(case.state.value)
    reasons: list[str] = []
    overdue = is_overdue(deadline=case.deadline, state=state, now=now)

    if user.is_manager:
        if state == CaseState.NEW:
            reasons.append("复发预警待分派")
        if state == CaseState.ASSIGNED:
            if case.claimed_by is None:
                reasons.append("待学院认领")
            else:
                reasons.append("学院处置中")
        if state == CaseState.WAITING_REVIEW:
            reasons.append("等待复核结论")
        if overdue:
            reasons.append("已超过处理期限")
    elif user.is_operator:
        if state == CaseState.ASSIGNED:
            if case.claimed_by is None:
                reasons.append("待认领")
            elif case.claimed_by == user.user_id:
                if case.review_result == "returned":
                    reasons.append("复核退回，需补充整改")
                else:
                    reasons.append("我的处置任务")
        if overdue:
            reasons.append("已超过处理期限")
    elif user.is_reviewer:
        if state == CaseState.WAITING_REVIEW:
            reasons.append("待复核")
        if overdue:
            reasons.append("复核已超过处理期限")
    return reasons


def todo_for_user(db: Session, user: CurrentUser, now: Optional[datetime] = None):
    now = now or datetime.now()
    query = db.query(DisposalCase).filter(DisposalCase.state != DisposalState.CLOSED)
    if user.is_operator:
        query = query.filter(DisposalCase.college_id == user.college_id)
    elif user.is_reviewer:
        query = query.filter(DisposalCase.state == DisposalState.WAITING_REVIEW)

    cases = query.order_by(
        DisposalCase.deadline.is_(None), DisposalCase.deadline.asc()
    ).all()

    result = []
    for case in cases:
        reasons = todo_reasons(case, user, now)
        if not reasons:
            continue
        result.append((case, reasons))
    return result


def actions_for(case: DisposalCase, user: CurrentUser) -> list[str]:
    return [a.value for a in available_actions(CaseState(case.state.value), _role_value(user))]
