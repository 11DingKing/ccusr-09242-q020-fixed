"""就业核验与预警处置的纯业务状态规则。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping


class CaseState(str, Enum):
    NEW = "new"
    CLAIMED = "claimed"
    WAITING_MATERIAL = "waiting_material"
    PROCESSING = "processing"
    WAITING_REVIEW = "waiting_review"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class Action(str, Enum):
    CLAIM = "claim"
    REQUEST_MATERIAL = "request_material"
    RECEIVE_MATERIAL = "receive_material"
    SUBMIT_REVIEW = "submit_review"
    APPROVE = "approve"
    RETURN = "return"
    TRANSFER = "transfer"
    CANCEL = "cancel"
    REOPEN = "reopen"


@dataclass(frozen=True)
class WorkflowDecision:
    allowed: bool
    from_state: CaseState
    to_state: CaseState
    reason: str
    required_fields: tuple[str, ...] = ()
    audit_category: str = "workflow"


TRANSITIONS: Mapping[tuple[CaseState, Action], CaseState] = {
    (CaseState.NEW, Action.CLAIM): CaseState.CLAIMED,
    (CaseState.NEW, Action.CANCEL): CaseState.CANCELLED,
    (CaseState.CLAIMED, Action.REQUEST_MATERIAL): CaseState.WAITING_MATERIAL,
    (CaseState.CLAIMED, Action.SUBMIT_REVIEW): CaseState.WAITING_REVIEW,
    (CaseState.CLAIMED, Action.TRANSFER): CaseState.CLAIMED,
    (CaseState.CLAIMED, Action.CLAIM): CaseState.CLAIMED,
    (CaseState.CLAIMED, Action.CANCEL): CaseState.CANCELLED,
    (CaseState.WAITING_MATERIAL, Action.RECEIVE_MATERIAL): CaseState.PROCESSING,
    (CaseState.WAITING_MATERIAL, Action.TRANSFER): CaseState.WAITING_MATERIAL,
    (CaseState.WAITING_MATERIAL, Action.CLAIM): CaseState.WAITING_MATERIAL,
    (CaseState.WAITING_MATERIAL, Action.CANCEL): CaseState.CANCELLED,
    (CaseState.PROCESSING, Action.REQUEST_MATERIAL): CaseState.WAITING_MATERIAL,
    (CaseState.PROCESSING, Action.SUBMIT_REVIEW): CaseState.WAITING_REVIEW,
    (CaseState.PROCESSING, Action.TRANSFER): CaseState.PROCESSING,
    (CaseState.PROCESSING, Action.CLAIM): CaseState.PROCESSING,
    (CaseState.WAITING_REVIEW, Action.APPROVE): CaseState.CLOSED,
    (CaseState.WAITING_REVIEW, Action.RETURN): CaseState.PROCESSING,
    (CaseState.CLOSED, Action.REOPEN): CaseState.PROCESSING,
    (CaseState.CANCELLED, Action.REOPEN): CaseState.NEW,
}


ROLE_ACTIONS: Mapping[str, frozenset[Action]] = {
    "operator": frozenset({
        Action.CLAIM,
        Action.REQUEST_MATERIAL,
        Action.RECEIVE_MATERIAL,
        Action.SUBMIT_REVIEW,
        Action.TRANSFER,
    }),
    "reviewer": frozenset({Action.APPROVE, Action.RETURN}),
    "manager": frozenset(Action),
}


ACTION_FIELDS: Mapping[Action, tuple[str, ...]] = {
    Action.CLAIM: ("assignee_id",),
    Action.REQUEST_MATERIAL: ("reason", "material_deadline"),
    Action.RECEIVE_MATERIAL: ("material_version",),
    Action.SUBMIT_REVIEW: ("summary",),
    Action.APPROVE: ("review_comment",),
    Action.RETURN: ("review_comment",),
    Action.TRANSFER: ("assignee_id", "reason"),
    Action.CANCEL: ("reason",),
    Action.REOPEN: ("reason",),
}


def _missing_fields(action: Action, payload: Mapping[str, object]) -> tuple[str, ...]:
    missing = []
    for name in ACTION_FIELDS.get(action, ()):
        value = payload.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return tuple(missing)


def decide_action(
    *, current: CaseState, action: Action, role: str,
    payload: Mapping[str, object], lease_owner: str | None = None,
    actor_id: str | None = None, lease_until: datetime | None = None,
    now: datetime | None = None,
) -> WorkflowDecision:
    """在不修改状态的情况下判断一次动作。"""

    allowed_actions = ROLE_ACTIONS.get(role, frozenset())
    if action not in allowed_actions:
        return WorkflowDecision(False, current, current, "当前角色无权执行该动作")
    target = TRANSITIONS.get((current, action))
    if target is None:
        return WorkflowDecision(False, current, current, "当前状态不接受该动作")
    missing = _missing_fields(action, payload)
    if missing:
        return WorkflowDecision(False, current, current, "动作缺少必要信息", missing)
    if current in {CaseState.CLAIMED, CaseState.PROCESSING, CaseState.WAITING_MATERIAL}:
        lease_expired = (
            lease_until is not None and now is not None and now > lease_until
        )
        # 他人持有时一律拒绝；但租约已过期时允许通过认领接手
        if lease_owner and actor_id and lease_owner != actor_id and role != "manager":
            if not (action == Action.CLAIM and lease_expired):
                return WorkflowDecision(False, current, current, "事项由其他人员持有")
        if lease_expired and action not in {Action.CLAIM, Action.TRANSFER, Action.CANCEL}:
            return WorkflowDecision(False, current, current, "持有租约已经到期")
    if action == Action.APPROVE and role == "reviewer" and lease_owner == actor_id:
        return WorkflowDecision(False, current, current, "处理人与复核人不能相同")
    category = "resolution" if target in {CaseState.CLOSED, CaseState.CANCELLED} else "workflow"
    return WorkflowDecision(True, current, target, "动作允许", audit_category=category)


def available_actions(current: CaseState, role: str) -> tuple[Action, ...]:
    allowed = ROLE_ACTIONS.get(role, frozenset())
    actions = [action for action in allowed if (current, action) in TRANSITIONS]
    return tuple(sorted(actions, key=lambda value: value.value))


def validate_transition_table() -> None:
    for (source, action), target in TRANSITIONS.items():
        if source == target and action not in {Action.TRANSFER, Action.CLAIM}:
            raise ValueError("只有转交或过期接手动作允许保持原状态")
        if source in {CaseState.CLOSED, CaseState.CANCELLED} and action != Action.REOPEN:
            raise ValueError("终态只能通过重新打开离开")
        if target == CaseState.CLOSED and action != Action.APPROVE:
            raise ValueError("关闭状态必须由复核通过产生")


validate_transition_table()
