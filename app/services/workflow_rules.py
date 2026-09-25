"""预警处置闭环的纯业务状态规则（无数据库、无 IO，便于单测）。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping


class CaseState(str, Enum):
    NEW = "待分派"
    ASSIGNED = "处理中"
    WAITING_REVIEW = "待复核"
    CLOSED = "已闭环"


class Action(str, Enum):
    DISPATCH = "分派"
    CLAIM = "认领"
    UPDATE_PROGRESS = "补充处置信息"
    ADD_EVIDENCE = "登记证据摘要"
    SUBMIT_REVIEW = "提交复核"
    APPROVE = "复核通过"
    RETURN = "复核退回"
    REASSIGN = "改派"
    RECUR = "复发重开"


class Role(str, Enum):
    MANAGER = "manager"
    OPERATOR = "operator"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class WorkflowDecision:
    allowed: bool
    from_state: CaseState
    to_state: CaseState
    reason: str
    required_fields: tuple[str, ...] = ()
    audit_category: str = "workflow"


# 状态迁移表：None 表示动作不改变状态（只追加业务信息/动作记录）。
TRANSITIONS: Mapping[tuple[CaseState, Action], CaseState | None] = {
    (CaseState.NEW, Action.DISPATCH): CaseState.ASSIGNED,
    (CaseState.ASSIGNED, Action.CLAIM): None,
    (CaseState.ASSIGNED, Action.UPDATE_PROGRESS): None,
    (CaseState.ASSIGNED, Action.ADD_EVIDENCE): None,
    (CaseState.ASSIGNED, Action.SUBMIT_REVIEW): CaseState.WAITING_REVIEW,
    (CaseState.ASSIGNED, Action.REASSIGN): CaseState.ASSIGNED,
    (CaseState.WAITING_REVIEW, Action.APPROVE): CaseState.CLOSED,
    (CaseState.WAITING_REVIEW, Action.RETURN): CaseState.ASSIGNED,
    (CaseState.WAITING_REVIEW, Action.REASSIGN): CaseState.ASSIGNED,
    (CaseState.CLOSED, Action.RECUR): CaseState.ASSIGNED,
}

ROLE_ACTIONS: Mapping[str, frozenset[Action]] = {
    Role.MANAGER.value: frozenset({
        Action.DISPATCH,
        Action.REASSIGN,
        Action.RECUR,
        Action.UPDATE_PROGRESS,
        Action.ADD_EVIDENCE,
        Action.SUBMIT_REVIEW,
    }),
    Role.OPERATOR.value: frozenset({
        Action.CLAIM,
        Action.UPDATE_PROGRESS,
        Action.ADD_EVIDENCE,
        Action.SUBMIT_REVIEW,
    }),
    Role.REVIEWER.value: frozenset({Action.APPROVE, Action.RETURN}),
}

# 提交动作时必须提供的字段。
ACTION_FIELDS: Mapping[Action, tuple[str, ...]] = {
    Action.DISPATCH: ("assignee_id", "assignee_name"),
    Action.CLAIM: ("actor_id",),
    Action.UPDATE_PROGRESS: ("root_cause", "improvement_action"),
    Action.ADD_EVIDENCE: ("file_name", "summary"),
    Action.SUBMIT_REVIEW: ("root_cause", "improvement_action", "summary"),
    Action.APPROVE: ("review_comment",),
    Action.RETURN: ("review_comment",),
    Action.REASSIGN: ("assignee_id", "assignee_name"),
    Action.RECUR: ("recur_reason",),
}

# 复核闭环必须同时具备归因、改进措施和至少一条证据摘要。
SUBMIT_REQUIRED_SNAPSHOT_FIELDS = ("root_cause", "improvement_action")


def _missing_fields(action: Action, payload: Mapping[str, object]) -> tuple[str, ...]:
    missing = []
    for name in ACTION_FIELDS.get(action, ()):
        value = payload.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return tuple(missing)


def decide_action(
    *,
    current: CaseState,
    action: Action,
    role: str,
    payload: Mapping[str, object],
    actor_id: str | None = None,
    assignee_id: str | None = None,
    claimed_by: str | None = None,
    evidence_count: int = 0,
) -> WorkflowDecision:
    """在不修改状态的情况下判断一次处置动作是否允许。"""

    allowed_actions = ROLE_ACTIONS.get(role, frozenset())
    if action not in allowed_actions:
        return WorkflowDecision(False, current, current, "当前角色无权执行该动作")

    if (current, action) not in TRANSITIONS:
        return WorkflowDecision(False, current, current, "当前状态不接受该动作")

    missing = _missing_fields(action, payload)
    if missing:
        return WorkflowDecision(False, current, current, "动作缺少必要信息", missing)

    # 认领：只有被分派人本人可以认领；管理处代填处置信息不强制认领。
    if action == Action.CLAIM and assignee_id and actor_id and assignee_id != actor_id:
        return WorkflowDecision(False, current, current, "只能由被分派人本人认领")

    # 经办侧写操作（认领后）必须是认领人本人；未认领时被分派人本人也可操作。
    if action in {Action.UPDATE_PROGRESS, Action.ADD_EVIDENCE, Action.SUBMIT_REVIEW}:
        if role == Role.OPERATOR.value:
            holder = claimed_by or assignee_id
            if holder and actor_id and holder != actor_id:
                return WorkflowDecision(False, current, current, "该处置单由其他学院人员负责")

    if action == Action.SUBMIT_REVIEW and evidence_count <= 0:
        return WorkflowDecision(False, current, current, "提交复核前至少需要一条证据附件摘要")

    # 职责分离：复核人不能复核自己经办的处置单。
    if action in {Action.APPROVE, Action.RETURN} and actor_id:
        handlers = {assignee_id, claimed_by}
        if actor_id in handlers:
            return WorkflowDecision(False, current, current, "经办人与复核人不能为同一人")

    target = TRANSITIONS[(current, action)] or current
    category = "resolution" if target == CaseState.CLOSED else "workflow"
    return WorkflowDecision(True, current, target, "动作允许", audit_category=category)


def available_actions(current: CaseState, role: str) -> tuple[Action, ...]:
    allowed = ROLE_ACTIONS.get(role, frozenset())
    actions = [action for action in allowed if (current, action) in TRANSITIONS]
    return tuple(sorted(actions, key=lambda value: value.value))


def is_overdue(
    *,
    deadline: datetime | None,
    state: CaseState,
    now: datetime,
) -> bool:
    """逾期是查询时按 deadline 计算的派生属性，不依赖后台进程。"""
    if deadline is None or state == CaseState.CLOSED:
        return False
    return now > deadline


def validate_transition_table() -> None:
    for (source, action), target in TRANSITIONS.items():
        if target == CaseState.CLOSED and action != Action.APPROVE:
            raise ValueError("闭环状态必须由复核通过产生")
        if source == CaseState.CLOSED and action != Action.RECUR:
            raise ValueError("已闭环的处置单只能通过复发重开离开")
        if target == CaseState.NEW:
            raise ValueError("处置单一旦分派不能回到待分派")


validate_transition_table()
