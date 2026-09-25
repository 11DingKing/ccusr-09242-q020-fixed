from sqlalchemy import Column, Integer, String, ForeignKey, Float, Enum, Text
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import WarningType, WarningLevel, WarningStatus


class Warning(Base, TimestampMixin):
    __tablename__ = "warnings"

    id = Column(Integer, primary_key=True, index=True)
    warning_type = Column(Enum(WarningType), nullable=False, comment="预警类型")
    warning_level = Column(Enum(WarningLevel), nullable=False, comment="预警级别")
    status = Column(Enum(WarningStatus), nullable=False, default=WarningStatus.ACTIVE, comment="预警状态")

    target_type = Column(String(20), nullable=False, comment="预警对象: micro_major/college")
    target_id = Column(Integer, nullable=False, index=True, comment="预警对象ID")
    target_name = Column(String(100), nullable=False, comment="预警对象名称")

    indicator = Column(String(50), nullable=False, comment="预警指标: confirmed_rate/aligned_rate")
    current_value = Column(Float, nullable=False, comment="当前指标值(%)")
    province_value = Column(Float, nullable=True, comment="全省对照值(%)")
    gap = Column(Float, nullable=True, comment="与省线差距(%)")

    start_year = Column(Integer, nullable=False, comment="下降起始届次")
    end_year = Column(Integer, nullable=False, comment="最新届次")
    decline_count = Column(Integer, nullable=False, default=1, comment="连续下降届数")
    decline_details = Column(Text, comment="各届数据明细JSON")

    description = Column(Text, comment="预警说明")

    attribution_records = relationship("AttributionRecord", back_populates="warning", cascade="all, delete-orphan")
    disposal_cases = relationship(
        "DisposalCase", back_populates="warning",
        order_by="DisposalCase.cycle_no",
        doc="不随预警删除级联删除，以保留不可变处置记录",
    )
