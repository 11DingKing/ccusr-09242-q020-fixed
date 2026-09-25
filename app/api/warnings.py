from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from app.core import get_db
from app.models import (
    Warning,
    WarningStatus,
    WarningType,
    WarningLevel,
    AttributionRecord,
    OPEN_WARNING_STATUSES,
)
from app.schemas import (
    Warning as WarningSchema,
    WarningUpdate,
    WarningListItem,
    WarningListResponse,
)
from app.utils import run_full_warning_detection, run_warning_detection_for_target

router = APIRouter(prefix="/warnings", tags=["预警管理"])


@router.get("", response_model=WarningListResponse)
def list_warnings(
    status: Optional[str] = Query(None, description="预警状态：预警中/已解决/已忽略"),
    warning_type: Optional[str] = Query(None, description="预警类型"),
    warning_level: Optional[str] = Query(None, description="预警级别"),
    target_type: Optional[str] = Query(None, description="预警对象类型：micro_major/college"),
    target_id: Optional[int] = Query(None, description="预警对象ID"),
    db: Session = Depends(get_db),
):
    query = db.query(Warning)

    if status:
        # 同时接受枚举名(ACTIVE)与中文展示值(预警中)。
        status_enum = next(
            (s for s in WarningStatus if s.value == status or s.name == status),
            None,
        )
        if status_enum is None:
            raise HTTPException(status_code=400, detail=f"未知预警状态: {status}")
        query = query.filter(Warning.status == status_enum)
    if warning_type:
        type_enum = next(
            (t for t in WarningType if t.value == warning_type or t.name == warning_type),
            None,
        )
        if type_enum is None:
            raise HTTPException(status_code=400, detail=f"未知预警类型: {warning_type}")
        query = query.filter(Warning.warning_type == type_enum)
    if warning_level:
        level_enum = next(
            (lv for lv in WarningLevel if lv.value == warning_level or lv.name == warning_level),
            None,
        )
        if level_enum is None:
            raise HTTPException(status_code=400, detail=f"未知预警级别: {warning_level}")
        query = query.filter(Warning.warning_level == level_enum)
    if target_type:
        query = query.filter(Warning.target_type == target_type)
    if target_id:
        query = query.filter(Warning.target_id == target_id)

    warnings = query.order_by(Warning.created_at.desc()).all()

    active_count = db.query(Warning).filter(Warning.status.in_(OPEN_WARNING_STATUSES)).count()
    resolved_count = db.query(Warning).filter(Warning.status == WarningStatus.RESOLVED).count()

    data = []
    for w in warnings:
        attribution_count = db.query(AttributionRecord).filter(
            AttributionRecord.warning_id == w.id
        ).count()
        data.append(WarningListItem(
            id=w.id,
            warning_type=w.warning_type.value,
            warning_level=w.warning_level.value,
            status=w.status.value,
            target_type=w.target_type,
            target_id=w.target_id,
            target_name=w.target_name,
            indicator=w.indicator,
            current_value=w.current_value,
            province_value=w.province_value,
            gap=w.gap,
            start_year=w.start_year,
            end_year=w.end_year,
            decline_count=w.decline_count,
            description=w.description,
            attribution_count=attribution_count,
            created_at=w.created_at,
        ))

    return WarningListResponse(
        total=len(warnings),
        active_count=active_count,
        resolved_count=resolved_count,
        data=data,
    )


@router.get("/{warning_id}", response_model=WarningSchema)
def get_warning(warning_id: int, db: Session = Depends(get_db)):
    warning = db.query(Warning).filter(Warning.id == warning_id).first()
    if not warning:
        raise HTTPException(status_code=404, detail="预警不存在")
    return warning


@router.put("/{warning_id}", response_model=WarningSchema)
def update_warning(
    warning_id: int,
    warning_in: WarningUpdate,
    db: Session = Depends(get_db),
):
    warning = db.query(Warning).filter(Warning.id == warning_id).first()
    if not warning:
        raise HTTPException(status_code=404, detail="预警不存在")

    update_data = warning_in.model_dump(exclude_unset=True)
    if "status" in update_data:
        # 预警状态只能随处置闭环推进（分派/复核/复发），禁止直接改写，
        # 避免绕过复核把同一风险误判为已解决。
        raise HTTPException(
            status_code=409,
            detail="预警状态由处置闭环驱动，请通过处置接口推进",
        )
    for key, value in update_data.items():
        setattr(warning, key, value)

    db.commit()
    db.refresh(warning)
    return warning


@router.post("/detect")
def run_detection(
    target_type: Optional[str] = Query(None, description="对象类型：micro_major/college，不传则检测全部"),
    target_id: Optional[int] = Query(None, description="对象ID，与target_type配合使用"),
    db: Session = Depends(get_db),
):
    if target_type and target_id:
        warnings = run_warning_detection_for_target(db, target_type, target_id)
        return {
            "message": f"针对{target_type}#{target_id}的预警检测完成",
            "warnings_count": len(warnings),
            "warnings": [
                {
                    "id": w.id,
                    "type": w.warning_type.value,
                    "level": w.warning_level.value,
                    "description": w.description,
                }
                for w in warnings
            ],
        }
    else:
        result = run_full_warning_detection(db)
        return {
            "message": "全量预警检测完成",
            **result,
        }
