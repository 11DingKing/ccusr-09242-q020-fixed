"""预警处置闭环的数据模型。

- WarningDisposition：一张预警一张处置单，承载责任范围、期限、租约与复核意见。
- DispositionActionLog：只追加、不可变的动作记录（仅允许 INSERT）。
- DispositionCycle：每次复核通过归档一个闭环周期，重新打开时历史周期保留。
- DispositionEvidence：证据附件摘要（不落文件本体，只存摘要与定位信息）。
- WarningRecurrence：复发关联，记录由哪张历史闭环预警复发为新预警。
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Date,
    Enum,
    Text,
    UniqueConstraint,
    Index,
    event,
    DDL,
)
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import DispositionScope
from app.services.workflow_rules import CaseState


class WarningDisposition(Base, TimestampMixin):
    __tablename__ = "warning_dispositions"

    id = Column(Integer, primary_key=True, index=True)
    warning_id = Column(
        Integer, ForeignKey("warnings.id"), nullable=False, unique=True, index=True,
        comment="关联预警ID（一警一单）",
    )

    # 状态机：取值见 app.services.workflow_rules.CaseState
    state = Column(
        String(20), nullable=False, default=CaseState.NEW.value,
        comment="处置状态：new/claimed/waiting_material/processing/waiting_review/closed/cancelled",
    )
    cycle_no = Column(Integer, nullable=False, default=1, comment="当前闭环轮次（每重新打开一次加一）")

    # 责任范围
    scope = Column(
        Enum(DispositionScope), nullable=True,
        comment="责任范围（学院领导/教务部门/学工部门/就业指导/微专业教研组）",
    )
    scope_remark = Column(String(200), nullable=True, comment="责任范围补充说明")

    # 认领与处理（租约）
    assignee_id = Column(String(50), nullable=True, index=True, comment="当前处理人ID")
    assignee_name = Column(String(50), nullable=True, comment="当前处理人姓名")
    claimed_at = Column(DateTime, nullable=True, comment="首次认领时间")
    lease_until = Column(DateTime, nullable=True, comment="处理租约到期时间（过期可被他人重新认领）")

    # 处理期限：实时与当前时间比较判定逾期，不依赖后台任务
    due_date = Column(Date, nullable=True, index=True, comment="处理期限")
    duration_days = Column(Integer, nullable=True, comment="期限天数（认领时按SLA计算）")
    closed_at = Column(DateTime, nullable=True, comment="最近一次复核通过时间")

    # 处置内容
    root_cause = Column(Text, nullable=True, comment="根因分析")
    improvement = Column(Text, nullable=True, comment="改进措施")
    submit_summary = Column(Text, nullable=True, comment="提交复核时的处理小结")

    # 复核
    reviewer_id = Column(String(50), nullable=True, comment="复核人ID")
    reviewer_name = Column(String(50), nullable=True, comment="复核人姓名")
    review_comment = Column(Text, nullable=True, comment="最近一次复核意见（含退回意见）")
    submitted_at = Column(DateTime, nullable=True, comment="提交复核时间")
    reviewed_at = Column(DateTime, nullable=True, comment="最近复核时间")

    # 复发：新预警可指向历史闭环预警
    recurrence_of_id = Column(
        Integer, ForeignKey("warnings.id"), nullable=True, index=True,
        comment="若为复发预警，指向最初（或上一张）已闭环预警ID",
    )
    recurrence_reason = Column(Text, nullable=True, comment="复发判定说明")

    warning = relationship("Warning", foreign_keys=[warning_id])
    recurrence_of = relationship("Warning", foreign_keys=[recurrence_of_id])
    cycles = relationship(
        "DispositionCycle", back_populates="disposition",
        cascade="all, delete-orphan", order_by="DispositionCycle.cycle_no",
    )
    evidences = relationship(
        "DispositionEvidence", back_populates="disposition",
        cascade="all, delete-orphan", order_by="DispositionEvidence.id",
    )
    action_logs = relationship(
        "DispositionActionLog", back_populates="disposition",
        cascade="all, delete-orphan", order_by="DispositionActionLog.id",
    )


class DispositionCycle(Base):
    """闭环周期归档：复核通过时写入，重新打开不删除、不修改。"""

    __tablename__ = "disposition_cycles"

    id = Column(Integer, primary_key=True, index=True)
    disposition_id = Column(
        Integer, ForeignKey("warning_dispositions.id"), nullable=False, index=True,
    )
    cycle_no = Column(Integer, nullable=False, comment="第几轮闭环")
    warning_id = Column(Integer, nullable=False, comment="冗余预警ID，便于归档查询")

    scope = Column(Enum(DispositionScope), nullable=True, comment="责任范围")
    assignee_id = Column(String(50), nullable=True, comment="处理人ID")
    assignee_name = Column(String(50), nullable=True, comment="处理人姓名")
    reviewer_id = Column(String(50), nullable=True, comment="复核人ID")
    reviewer_name = Column(String(50), nullable=True, comment="复核人姓名")

    root_cause = Column(Text, nullable=True, comment="根因（归档快照）")
    improvement = Column(Text, nullable=True, comment="改进措施（归档快照）")
    review_comment = Column(Text, nullable=True, comment="复核通过意见（归档快照）")

    claimed_at = Column(DateTime, nullable=True)
    due_date = Column(Date, nullable=True, comment="本轮处理期限")
    submitted_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=False, comment="本轮闭环时间")

    disposition = relationship("WarningDisposition", back_populates="cycles")

    __table_args__ = (
        UniqueConstraint("disposition_id", "cycle_no", name="uq_cycle_no_per_disposition"),
    )


class DispositionEvidence(Base, TimestampMixin):
    """证据附件摘要：只保存摘要与定位信息，不保存文件本体。"""

    __tablename__ = "disposition_evidences"

    id = Column(Integer, primary_key=True, index=True)
    disposition_id = Column(
        Integer, ForeignKey("warning_dispositions.id"), nullable=False, index=True,
    )
    file_name = Column(String(200), nullable=False, comment="附件文件名")
    file_digest = Column(String(128), nullable=True, comment="附件摘要值（如SHA-256）")
    digest_alg = Column(String(20), nullable=True, comment="摘要算法")
    file_size = Column(Integer, nullable=True, comment="附件大小（字节）")
    content_summary = Column(Text, nullable=False, comment="证据内容摘要")
    uploaded_by = Column(String(50), nullable=True, comment="上传人ID")
    uploaded_by_name = Column(String(50), nullable=True, comment="上传人姓名")

    disposition = relationship("WarningDisposition", back_populates="evidences")


class DispositionActionLog(Base):
    """不可变动作记录：仅允许 INSERT，任何 UPDATE/DELETE 由数据库层拒绝。"""

    __tablename__ = "disposition_action_logs"

    id = Column(Integer, primary_key=True, index=True)
    disposition_id = Column(
        Integer, ForeignKey("warning_dispositions.id"), nullable=False, index=True,
    )
    warning_id = Column(Integer, nullable=False, index=True, comment="冗余预警ID")
    cycle_no = Column(Integer, nullable=False, comment="动作发生时的闭环轮次")
    seq = Column(Integer, nullable=False, comment="单内动作序号（从1递增）")

    action = Column(String(40), nullable=False, comment="动作（workflow_rules.Action）")
    from_state = Column(String(20), nullable=False, comment="动作前状态")
    to_state = Column(String(20), nullable=False, comment="动作后状态")
    actor_id = Column(String(50), nullable=False, comment="操作人ID")
    actor_name = Column(String(50), nullable=True, comment="操作人姓名")
    actor_role = Column(String(20), nullable=False, comment="操作人角色：operator/reviewer/manager")
    payload_json = Column(Text, nullable=True, comment="动作附带信息（JSON快照）")
    note = Column(Text, nullable=True, comment="备注/意见原文")
    created_at = Column(DateTime, nullable=False, comment="动作时间")

    disposition = relationship("WarningDisposition", back_populates="action_logs")

    __table_args__ = (
        UniqueConstraint("disposition_id", "seq", name="uq_action_seq_per_disposition"),
        Index("ix_action_logs_warning_cycle", "warning_id", "cycle_no"),
    )


@event.listens_for(DispositionActionLog, "before_update")
def _block_action_log_update(mapper, connection, target):  # pragma: no cover - 触发器保护
    raise RuntimeError("处置动作记录不可修改")


@event.listens_for(DispositionActionLog, "before_delete")
def _block_action_log_delete(mapper, connection, target):  # pragma: no cover - 触发器保护
    raise RuntimeError("处置动作记录不可删除")


class WarningRecurrence(Base):
    """复发关联：一张新（复发）预警对应一张已闭环的历史预警。"""

    __tablename__ = "warning_recurrences"

    id = Column(Integer, primary_key=True, index=True)
    warning_id = Column(
        Integer, ForeignKey("warnings.id"), nullable=False, unique=True, index=True,
        comment="复发的新预警ID",
    )
    origin_warning_id = Column(
        Integer, ForeignKey("warnings.id"), nullable=False, index=True,
        comment="被复发的历史闭环预警ID",
    )
    disposition_id = Column(
        Integer, ForeignKey("warning_dispositions.id"), nullable=True,
        comment="历史预警的处置单（上一次闭环）",
    )
    reason = Column(Text, nullable=True, comment="复发判定说明")
    created_by = Column(String(50), nullable=True, comment="登记人ID")
    created_by_name = Column(String(50), nullable=True, comment="登记人姓名")
    created_at = Column(DateTime, nullable=False, comment="关联建立时间")

    __table_args__ = (
        UniqueConstraint("warning_id", "origin_warning_id", name="uq_recurrence_pair"),
    )


# SQLite 触发器：即便绕过 ORM 直接执行 SQL，动作记录仍然不可变。
_IMMUTABLE_LOG_TRIGGERS = (
    DDL(
        "CREATE TRIGGER IF NOT EXISTS trg_action_logs_no_update "
        "BEFORE UPDATE ON disposition_action_logs "
        "BEGIN SELECT RAISE(ABORT, '处置动作记录不可修改'); END"
    ),
    DDL(
        "CREATE TRIGGER IF NOT EXISTS trg_action_logs_no_delete "
        "BEFORE DELETE ON disposition_action_logs "
        "BEGIN SELECT RAISE(ABORT, '处置动作记录不可删除'); END"
    ),
)

for _ddl in _IMMUTABLE_LOG_TRIGGERS:
    event.listen(
        DispositionActionLog.__table__,
        "after_create",
        _ddl.execute_if(dialect="sqlite"),
    )
