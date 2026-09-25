"""预警处置闭环的请求/响应结构。"""

from datetime import date, datetime
from typing import Optional, List

from .common import BaseSchema, TimestampSchema


# ---------- 请求 ----------

class DispositionDispatch(BaseSchema):
    """就业管理处分派：指定责任范围与处理期限。"""

    warning_id: int
    scope: str
    scope_remark: Optional[str] = None
    due_date: Optional[date] = None
    duration_days: Optional[int] = None


class ClaimRequest(BaseSchema):
    lease_days: Optional[int] = None


class RequestMaterialBody(BaseSchema):
    reason: str
    material_deadline: Optional[date] = None


class ReceiveMaterialBody(BaseSchema):
    material_version: str
    note: Optional[str] = None


class SubmitReviewBody(BaseSchema):
    summary: str
    root_cause: Optional[str] = None
    improvement: Optional[str] = None


class ReviewBody(BaseSchema):
    review_comment: str


class TransferBody(BaseSchema):
    assignee_id: str
    assignee_name: Optional[str] = None
    reason: str


class ReasonBody(BaseSchema):
    reason: str
    duration_days: Optional[int] = None


class DispositionUpdate(BaseSchema):
    scope: Optional[str] = None
    scope_remark: Optional[str] = None
    root_cause: Optional[str] = None
    improvement: Optional[str] = None


class EvidenceCreate(BaseSchema):
    file_name: str
    content_summary: str
    file_digest: Optional[str] = None
    digest_alg: Optional[str] = None
    file_size: Optional[int] = None


class RecurrenceCreate(BaseSchema):
    warning_id: int
    origin_warning_id: int
    reason: Optional[str] = None


# ---------- 响应 ----------

class WarningBrief(BaseSchema):
    id: int
    warning_type: str
    warning_level: str
    status: str
    target_type: str
    target_id: int
    target_name: str
    indicator: Optional[str] = None


class EvidenceItem(TimestampSchema):
    id: int
    disposition_id: int
    file_name: str
    file_digest: Optional[str] = None
    digest_alg: Optional[str] = None
    file_size: Optional[int] = None
    content_summary: str
    uploaded_by: Optional[str] = None
    uploaded_by_name: Optional[str] = None


class ActionLogItem(BaseSchema):
    id: int
    disposition_id: int
    warning_id: int
    cycle_no: int
    seq: int
    action: str
    from_state: str
    to_state: str
    actor_id: str
    actor_name: Optional[str] = None
    actor_role: str
    payload_json: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime


class CycleItem(BaseSchema):
    id: int
    cycle_no: int
    warning_id: int
    scope: Optional[str] = None
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    root_cause: Optional[str] = None
    improvement: Optional[str] = None
    review_comment: Optional[str] = None
    claimed_at: Optional[datetime] = None
    due_date: Optional[date] = None
    submitted_at: Optional[datetime] = None
    closed_at: datetime


class RecurrenceOriginBrief(BaseSchema):
    """复发预警上一次闭环的归档摘要。"""

    origin_warning_id: int
    origin_target_name: str
    origin_disposition_id: int
    closed_cycles: int
    last_closed_at: Optional[datetime] = None
    last_root_cause: Optional[str] = None
    last_improvement: Optional[str] = None
    reason: Optional[str] = None


class DispositionBrief(BaseSchema):
    id: int
    warning_id: int
    state: str
    cycle_no: int
    scope: Optional[str] = None
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    due_date: Optional[date] = None
    overdue: bool = False
    overdue_days: Optional[int] = None
    lease_expired: bool = False


class DispositionDetail(TimestampSchema):
    id: int
    warning_id: int
    state: str
    cycle_no: int

    scope: Optional[str] = None
    scope_remark: Optional[str] = None

    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    claimed_at: Optional[datetime] = None
    lease_until: Optional[datetime] = None
    due_date: Optional[date] = None
    duration_days: Optional[int] = None
    closed_at: Optional[datetime] = None

    root_cause: Optional[str] = None
    improvement: Optional[str] = None
    submit_summary: Optional[str] = None

    reviewer_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    review_comment: Optional[str] = None
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None

    recurrence_of_id: Optional[int] = None
    recurrence_reason: Optional[str] = None

    overdue: bool = False
    overdue_days: Optional[int] = None
    days_left: Optional[int] = None
    lease_expired: bool = False
    available_actions: List[str] = []
    warning: Optional[WarningBrief] = None
    recurrence_origin: Optional[RecurrenceOriginBrief] = None
    evidences: List[EvidenceItem] = []
    cycles: List[CycleItem] = []


class DispositionListResponse(BaseSchema):
    total: int
    overdue_count: int
    data: List[DispositionDetail]


class TodoItem(BaseSchema):
    disposition_id: int
    warning_id: int
    state: str
    cycle_no: int
    target_type: str
    target_id: int
    target_name: str
    warning_level: str
    scope: Optional[str] = None
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    due_date: Optional[date] = None
    days_overdue: Optional[int] = None
    lease_until: Optional[datetime] = None
    reasons: List[str] = []


class TodoResponse(BaseSchema):
    total: int
    overdue_count: int
    data: List[TodoItem]


class ActionResult(BaseSchema):
    message: str
    disposition: DispositionDetail


class RecurrenceItem(BaseSchema):
    id: int
    warning_id: int
    origin_warning_id: int
    disposition_id: Optional[int] = None
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    origin: Optional[RecurrenceOriginBrief] = None
