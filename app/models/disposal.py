from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, Text, Float, Index, text
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import DisposalState, DisposalAction


class DisposalCase(Base, TimestampMixin):
    """预警处置闭环：一张处置单对应一次闭环周期。

    预警复发时新建处置单并通过 parent_case_id 关联上一次闭环，
    从而在重新打开时保留上一次的根因、措施与复核记录。
    """

    __tablename__ = "disposal_cases"
    __table_args__ = (
        # 同一预警至多存在一张未闭环处置单，兜底并发分派。
        Index(
            "ux_disposal_open_per_warning",
            "warning_id",
            unique=True,
            sqlite_where=text("state IN ('NEW', 'ASSIGNED', 'WAITING_REVIEW')"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_no = Column(String(40), unique=True, nullable=False, index=True, comment="处置单编号")
    warning_id = Column(Integer, ForeignKey("warnings.id", ondelete="RESTRICT"), nullable=False, index=True, comment="关联预警ID")

    state = Column(Enum(DisposalState), nullable=False, default=DisposalState.NEW, comment="处置状态")
    cycle_no = Column(Integer, nullable=False, default=1, comment="闭环轮次，从1开始")

    # 责任范围：责任学院与具体被分派人。
    college_id = Column(Integer, ForeignKey("colleges.id"), nullable=True, index=True, comment="责任学院ID")
    college_name = Column(String(100), nullable=True, comment="责任学院名称")
    assignee_id = Column(String(50), nullable=True, index=True, comment="被分派人账号")
    assignee_name = Column(String(50), nullable=True, comment="被分派人姓名")
    claimed_by = Column(String(50), nullable=True, comment="实际认领人账号")
    claimed_at = Column(DateTime, nullable=True, comment="认领时间")

    # 处理期限（SLA 天数）与截止时间，逾期在查询时计算，无后台进程。
    sla_days = Column(Integer, nullable=False, default=7, comment="处理期限(工作日/自然日天数)")
    deadline = Column(DateTime, nullable=True, index=True, comment="处理截止时间")
    closed_at = Column(DateTime, nullable=True, comment="闭环时间")

    # 闭环时刻风险数据覆盖的最新届次/指标值：复发判定的基线快照，
    # 防止同一批数据重复检测时把已闭环预警反复重开。
    closed_end_year = Column(Integer, nullable=True, comment="闭环时风险覆盖的最新届次")
    closed_current_value = Column(Float, nullable=True, comment="闭环时指标值(%)")

    # 根因与改进措施。
    root_cause = Column(Text, nullable=True, comment="根因分析")
    improvement_action = Column(Text, nullable=True, comment="改进措施")

    # 复核信息。
    submitted_by = Column(String(50), nullable=True, comment="提交复核人")
    submitted_at = Column(DateTime, nullable=True, comment="提交复核时间")
    reviewed_by = Column(String(50), nullable=True, comment="复核人")
    review_comment = Column(Text, nullable=True, comment="最近一次复核意见")
    review_result = Column(String(20), nullable=True, comment="最近复核结果: approved/returned")

    # 复发关联：同一条预警的处置单形成链。
    parent_case_id = Column(Integer, ForeignKey("disposal_cases.id"), nullable=True, index=True, comment="上一轮闭环处置单")
    recur_reason = Column(Text, nullable=True, comment="复发/重开原因")

    warning = relationship("Warning", back_populates="disposal_cases")
    parent = relationship("DisposalCase", remote_side=[id], foreign_keys=[parent_case_id])
    evidences = relationship(
        "DisposalEvidence", back_populates="case",
        cascade="all, delete-orphan", order_by="DisposalEvidence.id",
    )
    action_logs = relationship(
        "DisposalActionLog", back_populates="case",
        cascade="all, delete-orphan", order_by="DisposalActionLog.id",
    )


class DisposalEvidence(Base, TimestampMixin):
    """证据附件摘要：只存摘要与定位信息，文件本体由外部存储承载。"""

    __tablename__ = "disposal_evidences"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("disposal_cases.id"), nullable=False, index=True)

    file_name = Column(String(255), nullable=False, comment="附件名称")
    file_key = Column(String(255), nullable=True, comment="附件存储标识/定位号")
    summary = Column(Text, nullable=False, comment="证据内容摘要")
    uploaded_by = Column(String(50), nullable=False, comment="登记人账号")

    case = relationship("DisposalCase", back_populates="evidences")


class DisposalActionLog(Base):
    """不可变动作记录：只追加，不提供修改/删除接口。"""

    __tablename__ = "disposal_action_logs"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("disposal_cases.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, comment="单内动作序号")

    action = Column(Enum(DisposalAction), nullable=False, comment="动作")
    from_state = Column(Enum(DisposalState), nullable=True, comment="动作前状态")
    to_state = Column(Enum(DisposalState), nullable=True, comment="动作后状态")
    actor_id = Column(String(50), nullable=False, comment="操作人账号")
    actor_role = Column(String(20), nullable=False, comment="操作人角色")
    detail = Column(Text, nullable=True, comment="动作明细JSON")
    created_at = Column(DateTime, nullable=False, comment="动作时间")

    case = relationship("DisposalCase", back_populates="action_logs")
