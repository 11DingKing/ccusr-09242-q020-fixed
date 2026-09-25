from typing import Optional, List, Dict
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from collections import Counter

from app.core import get_db
from app.models import (
    Graduate,
    College,
    MicroMajor,
    EmployerFollowUp,
    DestinationStatus,
    DestinationType,
    Warning,
    WarningStatus,
    OPEN_WARNING_STATUSES,
    WarningLevel,
    WarningType,
    AttributionRecord,
    AttributionCategory,
    ProvinceReferenceLine,
)
from app.schemas import (
    ComparisonStats,
    FollowUpComparisonStats,
    YearlyTrendResponse,
    YearlyTrendItem,
    ReportResponse,
    ReportItem,
    WarningListResponse,
    WarningListItem,
    AttributionDistributionResponse,
    AttributionDistributionItem,
)
from app.utils import (
    get_comparison_stats,
    get_follow_up_comparison,
    calculate_group_stats,
    format_salary_display,
    _format_satisfaction,
    _format_retention,
    run_warning_detection_for_target,
    calculate_yearly_indicators,
)
from app.utils.stats_calculator import _eager_load_follow_ups

router = APIRouter(prefix="/statistics", tags=["统计分析"])


@router.get("/comparison", response_model=ComparisonStats)
def get_group_comparison(
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    college_id: Optional[int] = Query(None, description="学院ID"),
    micro_major_id: Optional[int] = Query(None, description="微专业ID"),
    db: Session = Depends(get_db),
):
    return get_comparison_stats(
        db=db,
        graduation_year=graduation_year,
        college_id=college_id,
        micro_major_id=micro_major_id
    )


@router.get("/follow-up-comparison", response_model=FollowUpComparisonStats)
def get_follow_up_group_comparison(
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    college_id: Optional[int] = Query(None, description="学院ID"),
    micro_major_id: Optional[int] = Query(None, description="微专业ID"),
    db: Session = Depends(get_db),
):
    return get_follow_up_comparison(
        db=db,
        graduation_year=graduation_year,
        college_id=college_id,
        micro_major_id=micro_major_id,
    )


@router.get("/trend/{micro_major_id}", response_model=YearlyTrendResponse)
def get_yearly_trend(
    micro_major_id: int,
    run_detection: bool = Query(False, description="是否先运行预警检测"),
    db: Session = Depends(get_db),
):
    micro_major = db.query(MicroMajor).filter(MicroMajor.id == micro_major_id).first()
    if not micro_major:
        raise HTTPException(status_code=404, detail="微专业不存在")

    if run_detection:
        run_warning_detection_for_target(db, "micro_major", micro_major_id)

    active_warnings = db.query(Warning).filter(
        Warning.target_type == "micro_major",
        Warning.target_id == micro_major_id,
        Warning.status.in_(OPEN_WARNING_STATUSES),
    ).all()

    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]

    yearly_indicators = calculate_yearly_indicators(db, "micro_major", micro_major_id)
    indicator_map = {d["year"]: d for d in yearly_indicators}

    trend = []
    for year in years:
        all_graduates = db.query(Graduate).filter(
            Graduate.graduation_year == year
        ).all()

        with_micro = [
            g for g in all_graduates
            if g.has_micro_major and g.micro_major_id == micro_major_id
        ]
        without_micro = [
            g for g in all_graduates
            if not g.has_micro_major
        ]

        with_micro_count = len(with_micro)
        without_micro_count = len(without_micro)

        def calc_confirmed_rate(grads):
            if not grads:
                return 0.0
            confirmed = [
                g for g in grads
                if g.destination_status in (DestinationStatus.CONFIRMED, DestinationStatus.VERIFIED)
            ]
            return round((len(confirmed) / len(grads)) * 100, 2)

        year_warnings = [
            w for w in active_warnings
            if w.start_year <= year <= w.end_year
        ]
        has_warning = len(year_warnings) > 0
        warning_types = [w.warning_type.value for w in year_warnings]

        indicator = indicator_map.get(year, {})

        trend.append(YearlyTrendItem(
            year=year,
            with_micro_rate=calc_confirmed_rate(with_micro),
            without_micro_rate=calc_confirmed_rate(without_micro),
            with_micro_count=with_micro_count,
            without_micro_count=without_micro_count,
            has_warning=has_warning,
            warning_types=warning_types,
            confirmed_rate=indicator.get("confirmed_rate", 0.0),
            aligned_rate=indicator.get("aligned_rate", 0.0),
        ))

    return YearlyTrendResponse(
        micro_major_name=micro_major.name,
        trend=trend,
        has_active_warnings=len(active_warnings) > 0,
        active_warning_count=len(active_warnings),
    )


@router.get("/reports/by-college", response_model=ReportResponse)
def get_report_by_college(db: Session = Depends(get_db)):
    colleges = db.query(College).all()
    data = []

    for college in colleges:
        graduates = db.query(Graduate).filter(
            Graduate.college_id == college.id
        ).all()
        _eager_load_follow_ups(db, graduates)

        stats = calculate_group_stats(graduates)
        data.append(ReportItem(
            dimension="学院",
            dimension_value=college.name,
            total_count=stats.total_count,
            confirmed_rate=stats.confirmed_rate,
            aligned_rate=stats.aligned_rate,
            avg_salary_display=stats.avg_salary_display,
            avg_satisfaction_display=stats.avg_satisfaction_display,
            retention_rate_display=stats.retention_rate_display,
            follow_up_count=stats.follow_up_count,
        ))

    return ReportResponse(
        report_type="按学院统计",
        data=data,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/reports/by-micro-major", response_model=ReportResponse)
def get_report_by_micro_major(db: Session = Depends(get_db)):
    micro_majors = db.query(MicroMajor).all()
    data = []

    all_without_micro = db.query(Graduate).filter(
        Graduate.has_micro_major == False
    ).all()
    _eager_load_follow_ups(db, all_without_micro)
    stats_all = calculate_group_stats(all_without_micro)
    data.append(ReportItem(
        dimension="微专业",
        dimension_value="未修读微专业",
        total_count=stats_all.total_count,
        confirmed_rate=stats_all.confirmed_rate,
        aligned_rate=stats_all.aligned_rate,
        avg_salary_display=stats_all.avg_salary_display,
        avg_satisfaction_display=stats_all.avg_satisfaction_display,
        retention_rate_display=stats_all.retention_rate_display,
        follow_up_count=stats_all.follow_up_count,
    ))

    for mm in micro_majors:
        graduates = db.query(Graduate).filter(
            Graduate.micro_major_id == mm.id,
            Graduate.has_micro_major == True
        ).all()
        _eager_load_follow_ups(db, graduates)

        stats = calculate_group_stats(graduates)
        data.append(ReportItem(
            dimension="微专业",
            dimension_value=mm.name,
            total_count=stats.total_count,
            confirmed_rate=stats.confirmed_rate,
            aligned_rate=stats.aligned_rate,
            avg_salary_display=stats.avg_salary_display,
            avg_satisfaction_display=stats.avg_satisfaction_display,
            retention_rate_display=stats.retention_rate_display,
            follow_up_count=stats.follow_up_count,
        ))

    return ReportResponse(
        report_type="按微专业统计",
        data=data,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/reports/by-year", response_model=ReportResponse)
def get_report_by_year(db: Session = Depends(get_db)):
    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]
    data = []

    for year in years:
        graduates = db.query(Graduate).filter(
            Graduate.graduation_year == year
        ).all()
        _eager_load_follow_ups(db, graduates)

        stats = calculate_group_stats(graduates)
        data.append(ReportItem(
            dimension="届次",
            dimension_value=f"{year}届",
            total_count=stats.total_count,
            confirmed_rate=stats.confirmed_rate,
            aligned_rate=stats.aligned_rate,
            avg_salary_display=stats.avg_salary_display,
            avg_satisfaction_display=stats.avg_satisfaction_display,
            retention_rate_display=stats.retention_rate_display,
            follow_up_count=stats.follow_up_count,
        ))

    return ReportResponse(
        report_type="按届次统计",
        data=data,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/reports/by-employer-follow-up", response_model=ReportResponse)
def get_report_by_employer_follow_up(db: Session = Depends(get_db)):
    employed_graduates = db.query(Graduate).filter(
        Graduate.destination_type == DestinationType.EMPLOYMENT
    ).all()
    _eager_load_follow_ups(db, employed_graduates)

    with_micro = [g for g in employed_graduates if g.has_micro_major]
    without_micro = [g for g in employed_graduates if not g.has_micro_major]

    stats_with = calculate_group_stats(with_micro)
    stats_without = calculate_group_stats(without_micro)

    data = [
        ReportItem(
            dimension="用人单位回访",
            dimension_value="修读微专业",
            total_count=stats_with.total_count,
            confirmed_rate=stats_with.confirmed_rate,
            aligned_rate=stats_with.aligned_rate,
            avg_salary_display=stats_with.avg_salary_display,
            avg_satisfaction_display=stats_with.avg_satisfaction_display,
            retention_rate_display=stats_with.retention_rate_display,
            follow_up_count=stats_with.follow_up_count,
        ),
        ReportItem(
            dimension="用人单位回访",
            dimension_value="未修读微专业",
            total_count=stats_without.total_count,
            confirmed_rate=stats_without.confirmed_rate,
            aligned_rate=stats_without.aligned_rate,
            avg_salary_display=stats_without.avg_salary_display,
            avg_satisfaction_display=stats_without.avg_satisfaction_display,
            retention_rate_display=stats_without.retention_rate_display,
            follow_up_count=stats_without.follow_up_count,
        ),
    ]

    return ReportResponse(
        report_type="用人单位回访对照统计",
        data=data,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/reports/warnings", response_model=WarningListResponse)
def get_warning_report(
    status: Optional[str] = Query(None, description="预警状态：预警中/已解决/已忽略"),
    warning_level: Optional[str] = Query(None, description="预警级别"),
    target_type: Optional[str] = Query(None, description="预警对象类型：micro_major/college"),
    run_detection: bool = Query(False, description="是否先运行全量预警检测"),
    db: Session = Depends(get_db),
):
    if run_detection:
        from app.utils import run_full_warning_detection
        run_full_warning_detection(db)

    query = db.query(Warning)

    if status:
        status_enum = next(
            (s for s in WarningStatus if s.value == status or s.name == status),
            None,
        )
        if status_enum is None:
            raise HTTPException(status_code=400, detail=f"未知预警状态: {status}")
        query = query.filter(Warning.status == status_enum)
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

    warnings = query.order_by(
        Warning.warning_level.desc(),
        Warning.created_at.desc(),
    ).all()

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


@router.get("/reports/attribution-distribution", response_model=AttributionDistributionResponse)
def get_attribution_distribution_report(
    target_type: Optional[str] = Query(None, description="对象类型：micro_major/college"),
    db: Session = Depends(get_db),
):
    query = db.query(AttributionRecord)

    if target_type:
        warning_query = db.query(Warning.id).filter(Warning.target_type == target_type)
        warning_ids = [w[0] for w in warning_query.all()]
        if warning_ids:
            query = query.filter(AttributionRecord.warning_id.in_(warning_ids))
        else:
            query = query.filter(False)

    all_records = query.all()
    total_records = len(all_records)

    category_counter = Counter()
    examples_by_category = {}
    for record in all_records:
        cat_value = record.category.value if hasattr(record.category, 'value') else str(record.category)
        category_counter[cat_value] += 1
        if cat_value not in examples_by_category:
            examples_by_category[cat_value] = []
        if len(examples_by_category[cat_value]) < 3:
            warning = db.query(Warning).filter(Warning.id == record.warning_id).first()
            examples_by_category[cat_value].append({
                "record_id": record.id,
                "target_name": warning.target_name if warning else "未知",
                "target_type": warning.target_type if warning else "unknown",
                "description": record.description[:100] + "..." if len(record.description) > 100 else record.description,
            })

    distribution = []
    for cat in AttributionCategory:
        count = category_counter.get(cat.value, 0)
        percentage = round((count / total_records * 100), 2) if total_records > 0 else 0
        distribution.append(AttributionDistributionItem(
            category=cat.value,
            count=count,
            percentage=percentage,
            examples=examples_by_category.get(cat.value, []),
        ))

    target_counter = Counter()
    for record in all_records:
        warning = db.query(Warning).filter(Warning.id == record.warning_id).first()
        if warning:
            key = f"{warning.target_type}:{warning.target_id}:{warning.target_name}"
            target_counter[key] += 1

    top_targets = []
    for key, count in target_counter.most_common(5):
        t_type, t_id, t_name = key.split(":", 2)
        top_targets.append({
            "target_type": t_type,
            "target_id": int(t_id),
            "target_name": t_name,
            "attribution_count": count,
        })

    return AttributionDistributionResponse(
        total_records=total_records,
        distribution=distribution,
        top_targets=top_targets,
    )
