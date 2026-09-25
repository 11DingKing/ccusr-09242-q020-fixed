"""预警处置闭环领域服务。

设计要点：
- 状态推进全部经 app.services.workflow_rules.decide_action 校验（角色矩阵 +
  租约归属 + 处理/复核分离），服务层不私自实现第二套规则。
- 逾期、待办均在查询时由数据库中的 due_date/lease_until/state 实时计算，
  不依赖任何后台常驻进程；服务重启后结果一致。
- 每次动作向 disposition_action_logs 追加一条不可变记录；复核通过时归档
  一个闭环周期，重新打开只新增轮次，不覆盖历史。
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import update as sa_update, func
from sqlalchemy.orm import Session

from app.models import (
    Warning,
    WarningStatus,
    MicroMajor,
    WarningDisposition,
    DispositionActionLog,
    DispositionCycle,
    DispositionEvidence,
    WarningRecurrence,
    DispositionScope,
)
from app.services.workflow_rules import Action, CaseState, decide_action
from app.schemas import (
    DispositionDetail,
    WarningBrief,
    CycleItem,
    EvidenceItem,
    TodoItem,
    RecurrenceOriginBrief,
)


# 分派后默认处理期限（自然日），按预警级别
DEFAULT_SLA_DAYS = {
    "红色预警": 7,
    "橙色预警": 10,
    "黄色预警": 15,
}
DEFAULT_LEASE_DAYS = 3
ACTIVE_STATES = {
    CaseState.NEW.value,
    CaseState.CLAIMED.value,
    CaseState.WAITING_MATERIAL.value,
    CaseState.PROCESSING.value,
    CaseState.WAITING_REVIEW.value,
}
TERMINAL_STATES = {CaseState.CLOSED.value, CaseState.CANCELLED.value}


@dataclass(frozen=True)
class Actor:
    id: str
    name: Optional[str]
    role: str
    college_id: Optional[int] = None


class WorkflowError(Exception):
    """业务规则冲突，由 API 层映射为 4xx。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------- 工具 ----------

def target_college_id(db: Session, warning: Warning) -> Optional[int]:
    """预警对象归属的学院：学院预警取对象ID，微专业预警取其所属学院。"""
    if warning.target_type == "college":
        return warning.target_id
    if warning.target_type == "micro_major":
        mm = db.query(MicroMajor).filter(MicroMajor.id == warning.target_id).first()
        return mm.college_id if mm else None
    return None


def ensure_college_scope(db: Session, actor: Actor, warning: Warning) -> None:
    """非管理处角色只能操作本学院责任范围的预警。"""
    if actor.role == "manager":
        return
    if actor.college_id is None:
        raise WorkflowError("缺少责任范围信息（X-College-Id）", 403)
    college_id = target_college_id(db, warning)
    if college_id is None or college_id != actor.college_id:
        raise WorkflowError("无权操作其他责任范围的预警", 403)


def compute_due_date(warning: Warning, duration_days: Optional[int], today: Optional[date] = None) -> date:
    if duration_days is None:
        level = warning.warning_level.value if hasattr(warning.warning_level, "value") else str(warning.warning_level)
        duration_days = DEFAULT_SLA_DAYS.get(level, 15)
    return (today or date.today()) + timedelta(days=duration_days)


def _next_seq(db: Session, disposition_id: int) -> int:
    current = db.query(func.max(DispositionActionLog.seq)).filter(
        DispositionActionLog.disposition_id == disposition_id
    ).scalar()
    return (current or 0) + 1


def append_log(
    db: Session,
    disposition: WarningDisposition,
    *,
    action: str,
    from_state: str,
    to_state: str,
    actor: Actor,
    note: Optional[str] = None,
    payload: Optional[dict] = None,
) -> DispositionActionLog:
    log = DispositionActionLog(
        disposition_id=disposition.id,
        warning_id=disposition.warning_id,
        cycle_no=disposition.cycle_no,
        seq=_next_seq(db, disposition.id),
        action=action,
        from_state=from_state,
        to_state=to_state,
        actor_id=actor.id,
        actor_name=actor.name,
        actor_role=actor.role,
        payload_json=json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
        note=note,
        created_at=datetime.now(),
    )
    db.add(log)
    return log


def _scope_value(scope) -> Optional[str]:
    return scope.value if hasattr(scope, "value") else scope


# ---------- 分派 ----------

def dispatch(
    db: Session,
    *,
    warning_id: int,
    scope: str,
    actor: Actor,
    due_date: Optional[date] = None,
    duration_days: Optional[int] = None,
    scope_remark: Optional[str] = None,
) -> WarningDisposition:
    if actor.role != "manager":
        raise WorkflowError("只有就业管理处可以分派预警", 403)

    try:
        scope_enum = DispositionScope(scope)
    except ValueError:
        raise WorkflowError(f"未知责任范围：{scope}", 400)

    warning = db.query(Warning).filter(Warning.id == warning_id).first()
    if not warning:
        raise WorkflowError("预警不存在", 404)
    if warning.status != WarningStatus.ACTIVE:
        raise WorkflowError("预警已闭环，复发风险请新建复发预警并关联", 409)

    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.warning_id == warning_id
    ).first()

    if disposition and disposition.state != CaseState.NEW.value:
        raise WorkflowError("预警已分派并进入处置流程，不能重复分派", 409)

    resolved_due = due_date or compute_due_date(warning, duration_days)

    if disposition is None:
        disposition = WarningDisposition(
            warning_id=warning_id,
            state=CaseState.NEW.value,
            cycle_no=1,
            scope=scope_enum,
            scope_remark=scope_remark,
            due_date=resolved_due,
            duration_days=(resolved_due - date.today()).days,
        )
        db.add(disposition)
        db.flush()
    else:
        disposition.scope = scope_enum
        disposition.scope_remark = scope_remark
        disposition.due_date = resolved_due
        disposition.duration_days = (resolved_due - date.today()).days

    append_log(
        db, disposition,
        action="dispatch", from_state=disposition.state, to_state=disposition.state,
        actor=actor, note=scope_remark,
        payload={
            "scope": scope_enum.value,
            "due_date": resolved_due.isoformat(),
            "duration_days": disposition.duration_days,
        },
    )
    db.commit()
    db.refresh(disposition)
    return disposition


# ---------- 状态动作 ----------

def _load_for_action(db: Session, disposition_id: int, actor: Actor) -> tuple[WarningDisposition, Warning]:
    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.id == disposition_id
    ).first()
    if not disposition:
        raise WorkflowError("处置单不存在", 404)
    warning = db.query(Warning).filter(Warning.id == disposition.warning_id).first()
    if not warning:
        raise WorkflowError("关联预警不存在", 404)
    ensure_college_scope(db, actor, warning)
    return disposition, warning


def perform_action(
    db: Session,
    disposition_id: int,
    action: Action,
    actor: Actor,
    payload: dict,
) -> WarningDisposition:
    disposition, warning = _load_for_action(db, disposition_id, actor)
    now = datetime.now()

    decision = decide_action(
        current=CaseState(disposition.state),
        action=action,
        role=actor.role,
        payload=payload,
        lease_owner=disposition.assignee_id,
        actor_id=actor.id,
        lease_until=disposition.lease_until,
        now=now,
    )
    if not decision.allowed:
        status = 403 if "无权" in decision.reason else 409
        if decision.required_fields:
            status = 400
        raise WorkflowError(
            f"{decision.reason}"
            + (f"：{', '.join(decision.required_fields)}" if decision.required_fields else ""),
            status,
        )

    old_state = disposition.state
    new_state = decision.to_state.value

    # 并发认领：条件更新保证只有一方成功
    # - 新单：state=new 且无人持有，置为 claimed
    # - 接手：租约已过期的在办单，状态保持不变、仅更换持有人
    if action == Action.CLAIM:
        lease_days = int(payload.get("lease_days") or DEFAULT_LEASE_DAYS)
        new_lease = now + timedelta(days=lease_days)
        if old_state == CaseState.NEW.value:
            stmt = (
                sa_update(WarningDisposition)
                .where(
                    WarningDisposition.id == disposition.id,
                    WarningDisposition.state == CaseState.NEW.value,
                    WarningDisposition.assignee_id.is_(None),
                )
                .values(
                    state=CaseState.CLAIMED.value,
                    assignee_id=actor.id,
                    assignee_name=actor.name,
                    claimed_at=now,
                    lease_until=new_lease,
                )
            )
        else:
            stmt = (
                sa_update(WarningDisposition)
                .where(
                    WarningDisposition.id == disposition.id,
                    WarningDisposition.state == old_state,
                    WarningDisposition.lease_until.isnot(None),
                    WarningDisposition.lease_until < now,
                )
                .values(
                    assignee_id=actor.id,
                    assignee_name=actor.name,
                    claimed_at=now,
                    lease_until=new_lease,
                )
            )
        result = db.execute(stmt)
        db.flush()
        if result.rowcount == 0:
            db.rollback()
            raise WorkflowError("预警已被其他人员认领或租约未到期", 409)
        db.refresh(disposition)
        new_state = disposition.state
        log_payload = {"lease_days": lease_days, "lease_until": new_lease.isoformat()}
        if old_state != CaseState.NEW.value:
            log_payload["takeover"] = True
            log_payload["from_state_takeover"] = old_state
    else:
        log_payload = _apply_side_effects(
            db, disposition, warning, action, actor, payload, now
        )

    append_log(
        db, disposition,
        action=action.value, from_state=old_state, to_state=new_state,
        actor=actor, note=payload.get("review_comment") or payload.get("reason") or payload.get("summary"),
        payload=log_payload,
    )
    db.commit()
    db.refresh(disposition)
    return disposition


def _apply_side_effects(
    db: Session,
    disposition: WarningDisposition,
    warning: Warning,
    action: Action,
    actor: Actor,
    payload: dict,
    now: datetime,
) -> dict:
    log_payload: dict = {}

    if action == Action.REQUEST_MATERIAL:
        log_payload["material_deadline"] = payload.get("material_deadline")
        disposition.state = CaseState.WAITING_MATERIAL.value

    elif action == Action.RECEIVE_MATERIAL:
        log_payload["material_version"] = payload.get("material_version")
        disposition.state = CaseState.PROCESSING.value

    elif action == Action.SUBMIT_REVIEW:
        if not (payload.get("root_cause") or disposition.root_cause):
            raise WorkflowError("提交复核前必须记录根因", 400)
        if not (payload.get("improvement") or disposition.improvement):
            raise WorkflowError("提交复核前必须记录改进措施", 400)
        disposition.root_cause = payload.get("root_cause") or disposition.root_cause
        disposition.improvement = payload.get("improvement") or disposition.improvement
        disposition.submit_summary = payload.get("summary")
        disposition.submitted_at = now
        disposition.state = CaseState.WAITING_REVIEW.value
        log_payload = {"summary": payload.get("summary")}

    elif action == Action.APPROVE:
        # 处理人与复核人不能为同一人
        if disposition.assignee_id and disposition.assignee_id == actor.id:
            raise WorkflowError("处理人与复核人不能相同", 409)
        disposition.reviewer_id = actor.id
        disposition.reviewer_name = actor.name
        disposition.review_comment = payload.get("review_comment")
        disposition.reviewed_at = now
        disposition.closed_at = now
        disposition.state = CaseState.CLOSED.value
        # 只有复核通过才能把预警置为已解决
        warning.status = WarningStatus.RESOLVED
        _archive_cycle(db, disposition, now)
        log_payload["closed_cycle"] = disposition.cycle_no

    elif action == Action.RETURN:
        disposition.reviewer_id = actor.id
        disposition.reviewer_name = actor.name
        disposition.review_comment = payload.get("review_comment")
        disposition.reviewed_at = now
        disposition.submitted_at = None
        disposition.state = CaseState.PROCESSING.value

    elif action == Action.TRANSFER:
        disposition.assignee_id = payload.get("assignee_id")
        disposition.assignee_name = payload.get("assignee_name")
        disposition.lease_until = now + timedelta(days=DEFAULT_LEASE_DAYS)
        log_payload = {
            "assignee_id": disposition.assignee_id,
            "lease_until": disposition.lease_until.isoformat(),
        }
        # 转交保持原状态（状态机已保证 to_state == 当前状态）

    elif action == Action.CANCEL:
        disposition.state = CaseState.CANCELLED.value
        # 撤销处置（如误报）对应预警“已忽略”，不再计入活跃风险
        warning.status = WarningStatus.DISMISSED

    elif action == Action.REOPEN:
        if disposition.state == CaseState.CLOSED.value:
            if warning.status != WarningStatus.RESOLVED:
                raise WorkflowError("闭环状态与预警不一致，无法重新打开", 409)
            # 上一轮闭环已归档到 disposition_cycles，这里只开启新一轮
            disposition.cycle_no += 1
            disposition.state = CaseState.PROCESSING.value
            disposition.submitted_at = None
            disposition.review_comment = None
            disposition.reviewer_id = None
            disposition.reviewer_name = None
            disposition.reviewed_at = None
            disposition.closed_at = None
            disposition.lease_until = now + timedelta(days=DEFAULT_LEASE_DAYS)
            if payload.get("duration_days"):
                days = int(payload["duration_days"])
                disposition.due_date = date.today() + timedelta(days=days)
                disposition.duration_days = days
                log_payload["new_due_date"] = disposition.due_date.isoformat()
            log_payload["new_cycle"] = disposition.cycle_no
        elif disposition.state == CaseState.CANCELLED.value:
            # 撤销恢复：重新分派流程，清理旧的持有信息
            disposition.state = CaseState.NEW.value
            disposition.assignee_id = None
            disposition.assignee_name = None
            disposition.claimed_at = None
            disposition.lease_until = None
            disposition.submitted_at = None
        else:
            raise WorkflowError("只有已闭环或已撤销的处置单可以重新打开", 409)
        warning.status = WarningStatus.ACTIVE

    return log_payload


def _archive_cycle(db: Session, disposition: WarningDisposition, closed_at: datetime) -> None:
    cycle = DispositionCycle(
        disposition_id=disposition.id,
        cycle_no=disposition.cycle_no,
        warning_id=disposition.warning_id,
        scope=disposition.scope,
        assignee_id=disposition.assignee_id,
        assignee_name=disposition.assignee_name,
        reviewer_id=disposition.reviewer_id,
        reviewer_name=disposition.reviewer_name,
        root_cause=disposition.root_cause,
        improvement=disposition.improvement,
        review_comment=disposition.review_comment,
        claimed_at=disposition.claimed_at,
        due_date=disposition.due_date,
        submitted_at=disposition.submitted_at,
        closed_at=closed_at,
    )
    db.add(cycle)
    db.flush()


# ---------- 处置内容维护 / 证据 ----------

def update_fields(db: Session, disposition_id: int, actor: Actor, data: dict) -> WarningDisposition:
    disposition, warning = _load_for_action(db, disposition_id, actor)
    if disposition.state in TERMINAL_STATES:
        raise WorkflowError("已终结的处置单不能修改", 409)

    # 责任范围归属由就业管理处调整；处理人只维护根因、措施与补充说明
    if data.get("scope") is not None and actor.role != "manager":
        raise WorkflowError("只有就业管理处可以调整责任范围", 403)
    if actor.role != "manager" and disposition.assignee_id != actor.id:
        raise WorkflowError("只有当前处理人可以维护处置内容", 403)

    if data.get("scope") is not None:
        try:
            disposition.scope = DispositionScope(data["scope"])
        except ValueError:
            raise WorkflowError(f"未知责任范围：{data['scope']}", 400)
    if data.get("scope_remark") is not None:
        disposition.scope_remark = data["scope_remark"]
    if data.get("root_cause") is not None:
        disposition.root_cause = data["root_cause"]
    if data.get("improvement") is not None:
        disposition.improvement = data["improvement"]

    db.commit()
    db.refresh(disposition)
    return disposition


def add_evidence(db: Session, disposition_id: int, actor: Actor, data: dict) -> DispositionEvidence:
    disposition, warning = _load_for_action(db, disposition_id, actor)
    if actor.role == "reviewer":
        raise WorkflowError("复核人不能上传处置证据", 403)
    if disposition.state in TERMINAL_STATES:
        raise WorkflowError("已终结的处置单不能补充证据", 409)

    evidence = DispositionEvidence(
        disposition_id=disposition.id,
        file_name=data["file_name"],
        content_summary=data["content_summary"],
        file_digest=data.get("file_digest"),
        digest_alg=data.get("digest_alg"),
        file_size=data.get("file_size"),
        uploaded_by=actor.id,
        uploaded_by_name=actor.name,
    )
    db.add(evidence)
    db.flush()
    append_log(
        db, disposition,
        action="upload_evidence", from_state=disposition.state, to_state=disposition.state,
        actor=actor, note=data["file_name"],
        payload={"evidence_id": evidence.id, "file_digest": evidence.file_digest},
    )
    db.commit()
    db.refresh(evidence)
    return evidence


# ---------- 复发关联 ----------

def link_recurrence(
    db: Session,
    *,
    warning_id: int,
    origin_warning_id: int,
    reason: Optional[str],
    actor: Actor,
) -> WarningRecurrence:
    if actor.role != "manager":
        raise WorkflowError("只有就业管理处可以登记复发关联", 403)
    if warning_id == origin_warning_id:
        raise WorkflowError("复发预警与历史预警不能相同", 400)

    warning = db.query(Warning).filter(Warning.id == warning_id).first()
    origin = db.query(Warning).filter(Warning.id == origin_warning_id).first()
    if not warning or not origin:
        raise WorkflowError("预警不存在", 404)

    same_risk = (
        warning.target_type == origin.target_type
        and warning.target_id == origin.target_id
        and warning.indicator == origin.indicator
    )
    if not same_risk:
        raise WorkflowError("只能关联同一对象、同一指标的历史闭环预警", 400)

    origin_disp = db.query(WarningDisposition).filter(
        WarningDisposition.warning_id == origin_warning_id
    ).first()
    if not origin_disp or origin_disp.state != CaseState.CLOSED.value:
        raise WorkflowError("被复发的历史预警尚未完成复核闭环", 400)

    existing = db.query(WarningRecurrence).filter(
        WarningRecurrence.warning_id == warning_id
    ).first()
    if existing:
        raise WorkflowError("该预警已登记复发关联", 409)

    link = WarningRecurrence(
        warning_id=warning_id,
        origin_warning_id=origin_warning_id,
        disposition_id=origin_disp.id,
        reason=reason,
        created_by=actor.id,
        created_by_name=actor.name,
        created_at=datetime.now(),
    )
    db.add(link)

    disposition = db.query(WarningDisposition).filter(
        WarningDisposition.warning_id == warning_id
    ).first()
    if disposition is None:
        disposition = WarningDisposition(
            warning_id=warning_id,
            state=CaseState.NEW.value,
            cycle_no=1,
            scope=origin_disp.scope,
            recurrence_of_id=origin_warning_id,
            recurrence_reason=reason,
            due_date=compute_due_date(warning, None),
            duration_days=DEFAULT_SLA_DAYS.get(warning.warning_level.value, 15),
        )
        db.add(disposition)
        db.flush()
    else:
        disposition.recurrence_of_id = origin_warning_id
        disposition.recurrence_reason = reason

    append_log(
        db, disposition,
        action="link_recurrence", from_state=disposition.state, to_state=disposition.state,
        actor=actor, note=reason,
        payload={"origin_warning_id": origin_warning_id, "origin_cycle": origin_disp.cycle_no},
    )
    db.commit()
    db.refresh(link)
    return link


def maybe_link_recurrence(db: Session, warning: Warning) -> None:
    """检测产生新预警时，若同一风险存在已复核闭环的历史预警，自动标记复发。"""
    origin_disp = (
        db.query(WarningDisposition)
        .join(Warning, Warning.id == WarningDisposition.warning_id)
        .filter(
            Warning.target_type == warning.target_type,
            Warning.target_id == warning.target_id,
            Warning.indicator == warning.indicator,
            Warning.warning_type == warning.warning_type,
            Warning.id != warning.id,
            WarningDisposition.state == CaseState.CLOSED.value,
        )
        .order_by(WarningDisposition.closed_at.desc())
        .first()
    )
    if origin_disp is None:
        return

    exists = db.query(WarningRecurrence).filter(
        WarningRecurrence.warning_id == warning.id
    ).first()
    if exists:
        return

    link = WarningRecurrence(
        warning_id=warning.id,
        origin_warning_id=origin_disp.warning_id,
        disposition_id=origin_disp.id,
        reason="检测到同一风险在历史闭环后再次触发",
        created_by="system",
        created_by_name="预警检测",
        created_at=datetime.now(),
    )
    db.add(link)
    db.flush()

    disposition = WarningDisposition(
        warning_id=warning.id,
        state=CaseState.NEW.value,
        cycle_no=1,
        scope=origin_disp.scope,
        recurrence_of_id=origin_disp.warning_id,
        recurrence_reason=link.reason,
        due_date=compute_due_date(warning, None),
        duration_days=DEFAULT_SLA_DAYS.get(warning.warning_level.value, 15),
    )
    db.add(disposition)
    db.flush()
    system = Actor(id="system", name="预警检测", role="manager")
    append_log(
        db, disposition,
        action="link_recurrence", from_state=CaseState.NEW.value, to_state=CaseState.NEW.value,
        actor=system, note=link.reason,
        payload={"origin_warning_id": origin_disp.warning_id},
    )


# ---------- 查询 / 序列化 ----------

def _recurrence_brief(db: Session, origin_warning_id: int, reason: Optional[str]) -> RecurrenceOriginBrief:
    origin = db.query(Warning).filter(Warning.id == origin_warning_id).first()
    disp = db.query(WarningDisposition).filter(
        WarningDisposition.warning_id == origin_warning_id
    ).first()
    cycles = []
    if disp:
        cycles = db.query(DispositionCycle).filter(
            DispositionCycle.disposition_id == disp.id
        ).order_by(DispositionCycle.cycle_no.desc()).all()
    latest = cycles[0] if cycles else None
    return RecurrenceOriginBrief(
        origin_warning_id=origin_warning_id,
        origin_target_name=origin.target_name if origin else "未知",
        origin_disposition_id=disp.id if disp else 0,
        closed_cycles=len(cycles),
        last_closed_at=latest.closed_at if latest else None,
        last_root_cause=latest.root_cause if latest else None,
        last_improvement=latest.improvement if latest else None,
        reason=reason,
    )


def build_detail(db: Session, disposition: WarningDisposition, actor: Actor) -> DispositionDetail:
    warning = db.query(Warning).filter(Warning.id == disposition.warning_id).first()
    ensure_college_scope(db, actor, warning)

    today = date.today()
    now = datetime.now()
    overdue = False
    overdue_days = None
    days_left = None
    if disposition.due_date and disposition.state not in TERMINAL_STATES:
        delta = (today - disposition.due_date).days
        if delta > 0:
            overdue, overdue_days = True, delta
        else:
            days_left = -delta
    lease_expired = (
        disposition.lease_until is not None
        and disposition.state in {
            CaseState.CLAIMED.value,
            CaseState.PROCESSING.value,
            CaseState.WAITING_MATERIAL.value,
        }
        and now > disposition.lease_until
    )

    from app.services.workflow_rules import available_actions
    actions = [a.value for a in available_actions(CaseState(disposition.state), actor.role)]

    brief = WarningBrief(
        id=warning.id,
        warning_type=warning.warning_type.value,
        warning_level=warning.warning_level.value,
        status=warning.status.value,
        target_type=warning.target_type,
        target_id=warning.target_id,
        target_name=warning.target_name,
        indicator=warning.indicator,
    )

    recurrence = None
    if disposition.recurrence_of_id:
        recurrence = _recurrence_brief(db, disposition.recurrence_of_id, disposition.recurrence_reason)

    evidences = [
        EvidenceItem.model_validate(e)
        for e in sorted(disposition.evidences, key=lambda x: x.id)
    ]
    cycles = [
        CycleItem.model_validate(c)
        for c in sorted(disposition.cycles, key=lambda x: x.cycle_no)
    ]

    return DispositionDetail(
        id=disposition.id,
        warning_id=disposition.warning_id,
        state=disposition.state,
        cycle_no=disposition.cycle_no,
        scope=_scope_value(disposition.scope),
        scope_remark=disposition.scope_remark,
        assignee_id=disposition.assignee_id,
        assignee_name=disposition.assignee_name,
        claimed_at=disposition.claimed_at,
        lease_until=disposition.lease_until,
        due_date=disposition.due_date,
        duration_days=disposition.duration_days,
        closed_at=disposition.closed_at,
        root_cause=disposition.root_cause,
        improvement=disposition.improvement,
        submit_summary=disposition.submit_summary,
        reviewer_id=disposition.reviewer_id,
        reviewer_name=disposition.reviewer_name,
        review_comment=disposition.review_comment,
        submitted_at=disposition.submitted_at,
        reviewed_at=disposition.reviewed_at,
        recurrence_of_id=disposition.recurrence_of_id,
        recurrence_reason=disposition.recurrence_reason,
        overdue=overdue,
        overdue_days=overdue_days,
        days_left=days_left,
        lease_expired=lease_expired,
        available_actions=actions,
        warning=brief,
        recurrence_origin=recurrence,
        evidences=evidences,
        cycles=cycles,
        created_at=disposition.created_at,
        updated_at=disposition.updated_at,
    )


def list_dispositions(
    db: Session,
    actor: Actor,
    *,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    warning_id: Optional[int] = None,
    overdue_only: bool = False,
    assignee_id: Optional[str] = None,
) -> list[WarningDisposition]:
    query = db.query(WarningDisposition).join(
        Warning, Warning.id == WarningDisposition.warning_id
    )
    if state:
        query = query.filter(WarningDisposition.state == state)
    if scope:
        try:
            scope_enum = DispositionScope(scope)
        except ValueError:
            raise WorkflowError(f"未知责任范围：{scope}", 400)
        query = query.filter(WarningDisposition.scope == scope_enum)
    if warning_id:
        query = query.filter(WarningDisposition.warning_id == warning_id)
    if assignee_id:
        query = query.filter(WarningDisposition.assignee_id == assignee_id)
    if overdue_only:
        query = query.filter(
            WarningDisposition.due_date.isnot(None),
            WarningDisposition.due_date < date.today(),
            WarningDisposition.state.notin_(list(TERMINAL_STATES)),
        )

    dispositions = query.order_by(WarningDisposition.due_date.is_(None), WarningDisposition.due_date).all()
    if actor.role != "manager":
        dispositions = [
            d for d in dispositions
            if target_college_id(db, d.warning) == actor.college_id
        ]
    return dispositions


def _todo_reasons(disposition: WarningDisposition, today: date, now: datetime) -> list[str]:
    reasons: list[str] = []
    if disposition.state in TERMINAL_STATES:
        return reasons
    if disposition.due_date and (today - disposition.due_date).days > 0:
        reasons.append("overdue")
    if disposition.state == CaseState.WAITING_REVIEW.value:
        reasons.append("waiting_review")
    if (
        disposition.state in {CaseState.CLAIMED.value, CaseState.PROCESSING.value, CaseState.WAITING_MATERIAL.value}
        and disposition.lease_until is not None and now > disposition.lease_until
    ):
        reasons.append("lease_expired")
    if disposition.state == CaseState.NEW.value and disposition.due_date and (today - disposition.due_date).days > 0:
        reasons.append("unassigned_overdue")
    return reasons


def build_todos(db: Session, actor: Actor, reason: Optional[str] = None) -> list[TodoItem]:
    """实时汇总待办：逾期、待复核、租约过期。无后台进程，重启后一致。"""
    today, now = date.today(), datetime.now()
    query = db.query(WarningDisposition).join(
        Warning, Warning.id == WarningDisposition.warning_id
    ).filter(WarningDisposition.state.in_(list(ACTIVE_STATES)))

    dispositions = query.order_by(WarningDisposition.due_date.is_(None), WarningDisposition.due_date).all()

    todos = []
    for d in dispositions:
        if actor.role != "manager" and target_college_id(db, d.warning) != actor.college_id:
            continue
        reasons = _todo_reasons(d, today, now)
        if not reasons:
            continue
        if reason and reason not in reasons:
            continue
        # 学院处理人只看分派给本院的；待复核对本院复核人同样可见
        w = d.warning
        todos.append(TodoItem(
            disposition_id=d.id,
            warning_id=d.warning_id,
            state=d.state,
            cycle_no=d.cycle_no,
            target_type=w.target_type,
            target_id=w.target_id,
            target_name=w.target_name,
            warning_level=w.warning_level.value,
            scope=_scope_value(d.scope),
            assignee_id=d.assignee_id,
            assignee_name=d.assignee_name,
            due_date=d.due_date,
            days_overdue=(today - d.due_date).days if d.due_date and (today - d.due_date).days > 0 else None,
            lease_until=d.lease_until,
            reasons=reasons,
        ))
    return todos
