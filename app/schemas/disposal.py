from datetime import datetime
from typing import Optional, List

from .common import BaseSchema, TimestampSchema


class CollegeScope(BaseSchema):
    college_id: Optional[int] = None
    college_name: Optional[str] = None


class DisposalDispatchRequest(BaseSchema):
    college_id: int
    assignee_id: str
    assignee_name: str
    sla_days: int = 7
    remark: Optional[str] = None


class DisposalClaimRequest(BaseSchema):
    remark: Optional[str] = None


class DisposalProgressRequest(BaseSchema):
    root_cause: Optional[str] = None
    improvement_action: Optional[str] = None
    remark: Optional[str] = None


class DisposalEvidenceCreate(BaseSchema):
    file_name: str
    file_key: Optional[str] = None
    summary: str


class DisposalSubmitRequest(BaseSchema):
    root_cause: str
    improvement_action: str
    summary: str


class DisposalReviewRequest(BaseSchema):
    review_comment: str


class DisposalReassignRequest(BaseSchema):
    college_id: Optional[int] = None
    assignee_id: str
    assignee_name: str
    reason: str
    sla_days: Optional[int] = None


class DisposalRecurRequest(BaseSchema):
    recur_reason: str
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    college_id: Optional[int] = None
    sla_days: int = 7


class DisposalEvidence(BaseSchema):
    id: int
    case_id: int
    file_name: str
    file_key: Optional[str] = None
    summary: str
    uploaded_by: str
    created_at: datetime


class DisposalActionLogItem(BaseSchema):
    id: int
    seq: int
    action: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    actor_id: str
    actor_role: str
    detail: Optional[str] = None
    created_at: datetime


class DisposalCaseBase(BaseSchema):
    id: int
    case_no: str
    warning_id: int
    state: str
    cycle_no: int
    college_id: Optional[int] = None
    college_name: Optional[str] = None
    assignee_id: Optional[str] = None
    assignee_name: Optional[str] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[datetime] = None
    sla_days: int
    deadline: Optional[datetime] = None
    root_cause: Optional[str] = None
    improvement_action: Optional[str] = None
    submitted_by: Optional[str] = None
    submitted_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    review_comment: Optional[str] = None
    review_result: Optional[str] = None
    parent_case_id: Optional[int] = None
    recur_reason: Optional[str] = None
    closed_at: Optional[datetime] = None
    closed_end_year: Optional[int] = None
    closed_current_value: Optional[float] = None


class DisposalCase(DisposalCaseBase, TimestampSchema):
    evidences: List[DisposalEvidence] = []
    action_logs: List[DisposalActionLogItem] = []


class DisposalCaseSummary(DisposalCaseBase, TimestampSchema):
    warning_type: Optional[str] = None
    warning_level: Optional[str] = None
    warning_status: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    target_name: Optional[str] = None
    evidence_count: int = 0
    overdue: bool = False
    can_view: bool = True


class DisposalTodoItem(DisposalCaseSummary):
    todo_reasons: List[str] = []
    available_actions: List[str] = []


class DisposalTodoResponse(BaseSchema):
    total: int
    overdue_count: int
    data: List[DisposalTodoItem]


class DisposalCaseListResponse(BaseSchema):
    total: int
    data: List[DisposalCaseSummary]


class DisposalActionResult(BaseSchema):
    message: str
    case: DisposalCase
    warning_status: str
