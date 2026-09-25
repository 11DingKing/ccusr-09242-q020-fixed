from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from app.models import (
    Graduate,
    College,
    MicroMajor,
    Warning,
    AttributionRecord,
    ProvinceReferenceLine,
    WarningStatus,
    OPEN_WARNING_STATUSES,
    WarningLevel,
    WarningType,
    AttributionCategory,
)
from app.schemas import (
    ProfileStats,
    WarningSummary,
    AttributionSummary,
    MicroMajorProfile,
    CollegeProfile,
    YearlyIndicatorData,
)
from app.utils.stats_calculator import calculate_group_stats, _eager_load_follow_ups
from app.utils.warning_detector import calculate_yearly_indicators


def build_warning_summary(
    db: Session,
    target_type: str,
    target_id: int,
) -> WarningSummary:
    all_warnings = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
    ).order_by(Warning.created_at.desc()).all()

    active_count = sum(1 for w in all_warnings if w.status in OPEN_WARNING_STATUSES)
    resolved_count = sum(1 for w in all_warnings if w.status == WarningStatus.RESOLVED)

    by_level = {}
    for level in WarningLevel:
        by_level[level.value] = sum(
            1 for w in all_warnings if w.warning_level == level
        )

    by_type = {}
    for wtype in WarningType:
        by_type[wtype.value] = sum(
            1 for w in all_warnings if w.warning_type == wtype
        )

    recent_warnings = all_warnings[:5]

    return WarningSummary(
        active_count=active_count,
        resolved_count=resolved_count,
        total_count=len(all_warnings),
        by_level=by_level,
        by_type=by_type,
        recent_warnings=recent_warnings,
    )


def build_attribution_summary(
    db: Session,
    target_type: str,
    target_id: int,
) -> AttributionSummary:
    warnings = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
    ).all()
    warning_ids = [w.id for w in warnings]

    all_records: List[AttributionRecord] = []
    if warning_ids:
        all_records = db.query(AttributionRecord).filter(
            AttributionRecord.warning_id.in_(warning_ids),
        ).order_by(AttributionRecord.created_at.desc()).all()

    by_category = {}
    for cat in AttributionCategory:
        by_category[cat.value] = sum(
            1 for r in all_records if r.category == cat
        )

    recent_records = all_records[:5]

    return AttributionSummary(
        total_count=len(all_records),
        by_category=by_category,
        recent_records=recent_records,
    )


def build_profile_stats(
    db: Session,
    target_type: str,
    target_id: int,
) -> ProfileStats:
    query = db.query(Graduate)
    if target_type == "micro_major":
        query = query.filter(
            Graduate.has_micro_major == True,
            Graduate.micro_major_id == target_id,
        )
    elif target_type == "college":
        query = query.filter(Graduate.college_id == target_id)

    all_graduates = query.all()
    _eager_load_follow_ups(db, all_graduates)
    overall_stats = calculate_group_stats(all_graduates)

    yearly_data = calculate_yearly_indicators(db, target_type, target_id)

    active_warnings = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.status.in_(OPEN_WARNING_STATUSES),
    ).all()

    yearly_trend = []
    for data in yearly_data:
        year_warnings = [
            w for w in active_warnings
            if w.start_year <= data["year"] <= w.end_year
        ]
        has_warning = len(year_warnings) > 0
        warning_types = [w.warning_type.value for w in year_warnings]

        yearly_trend.append(YearlyIndicatorData(
            year=data["year"],
            confirmed_rate=data["confirmed_rate"],
            aligned_rate=data["aligned_rate"],
            total_count=data["total_count"],
            confirmed_count=data["confirmed_count"],
            aligned_count=data["aligned_count"],
            has_warning=has_warning,
            warning_types=warning_types,
        ))

    province_comparison = None
    if yearly_data:
        latest_year = max(d["year"] for d in yearly_data)
        latest_data = [d for d in yearly_data if d["year"] == latest_year][0]

        confirmed_reference_line = db.query(ProvinceReferenceLine).filter(
            ProvinceReferenceLine.graduation_year == latest_year,
            ProvinceReferenceLine.indicator == "confirmed_rate",
        ).first()

        aligned_reference_line = db.query(ProvinceReferenceLine).filter(
            ProvinceReferenceLine.graduation_year == latest_year,
            ProvinceReferenceLine.indicator == "aligned_rate",
        ).first()

        if confirmed_reference_line or aligned_reference_line:
            province_comparison = {
                "year": latest_year,
                "confirmed_rate": {
                    "current": latest_data["confirmed_rate"],
                    "province_average": confirmed_reference_line.province_average if confirmed_reference_line else None,
                    "threshold": confirmed_reference_line.threshold if confirmed_reference_line else None,
                    "gap": confirmed_reference_line.threshold - latest_data["confirmed_rate"] if confirmed_reference_line else None,
                },
                "aligned_rate": {
                    "current": latest_data["aligned_rate"],
                    "province_average": aligned_reference_line.province_average if aligned_reference_line else None,
                    "threshold": aligned_reference_line.threshold if aligned_reference_line else None,
                    "gap": aligned_reference_line.threshold - latest_data["aligned_rate"] if aligned_reference_line else None,
                },
            }

    return ProfileStats(
        overall=overall_stats,
        yearly_trend=yearly_trend,
        province_comparison=province_comparison,
    )


def build_key_indicators_comparison(
    db: Session,
    target_type: str,
    target_id: int,
) -> "KeyIndicatorsComparison":
    from app.schemas import KeyIndicatorsComparison

    yearly_data = calculate_yearly_indicators(db, target_type, target_id)
    if not yearly_data:
        return KeyIndicatorsComparison(
            confirmed_rate={},
            aligned_rate={},
            total_count={},
            yearly_details=[],
        )

    sorted_data = sorted(yearly_data, key=lambda x: x["year"])

    def calc_trend(values: List[float]) -> str:
        if len(values) < 2:
            return "stable"
        if values[-1] > values[0]:
            return "rising"
        elif values[-1] < values[0]:
            return "falling"
        else:
            return "stable"

    confirmed_rates = [d["confirmed_rate"] for d in sorted_data]
    aligned_rates = [d["aligned_rate"] for d in sorted_data]
    counts = [d["total_count"] for d in sorted_data]

    return KeyIndicatorsComparison(
        confirmed_rate={
            "latest": confirmed_rates[-1] if confirmed_rates else 0,
            "average": sum(confirmed_rates) / len(confirmed_rates) if confirmed_rates else 0,
            "max": max(confirmed_rates) if confirmed_rates else 0,
            "min": min(confirmed_rates) if confirmed_rates else 0,
            "trend": calc_trend(confirmed_rates),
            "change_from_first": (confirmed_rates[-1] - confirmed_rates[0]) if len(confirmed_rates) >= 2 else 0,
        },
        aligned_rate={
            "latest": aligned_rates[-1] if aligned_rates else 0,
            "average": sum(aligned_rates) / len(aligned_rates) if aligned_rates else 0,
            "max": max(aligned_rates) if aligned_rates else 0,
            "min": min(aligned_rates) if aligned_rates else 0,
            "trend": calc_trend(aligned_rates),
            "change_from_first": (aligned_rates[-1] - aligned_rates[0]) if len(aligned_rates) >= 2 else 0,
        },
        total_count={
            "latest": counts[-1] if counts else 0,
            "total": sum(counts),
            "average": sum(counts) / len(counts) if counts else 0,
            "trend": calc_trend([float(c) for c in counts]),
        },
        yearly_details=sorted_data,
    )


def build_micro_major_profile(
    db: Session,
    micro_major_id: int,
) -> Optional[MicroMajorProfile]:
    micro_major = db.query(MicroMajor).filter(MicroMajor.id == micro_major_id).first()
    if not micro_major:
        return None

    stats = build_profile_stats(db, "micro_major", micro_major_id)
    warnings = build_warning_summary(db, "micro_major", micro_major_id)
    attributions = build_attribution_summary(db, "micro_major", micro_major_id)
    key_indicators = build_key_indicators_comparison(db, "micro_major", micro_major_id)

    return MicroMajorProfile(
        id=micro_major.id,
        name=micro_major.name,
        code=micro_major.code,
        description=micro_major.description,
        college_id=micro_major.college_id,
        college_name=micro_major.college.name if micro_major.college else "未知学院",
        stats=stats,
        warnings=warnings,
        attributions=attributions,
        key_indicators_comparison=key_indicators,
    )


def build_college_profile(
    db: Session,
    college_id: int,
) -> Optional[CollegeProfile]:
    college = db.query(College).filter(College.id == college_id).first()
    if not college:
        return None

    stats = build_profile_stats(db, "college", college_id)
    warnings = build_warning_summary(db, "college", college_id)
    attributions = build_attribution_summary(db, "college", college_id)
    key_indicators = build_key_indicators_comparison(db, "college", college_id)

    micro_major_count = len(college.micro_majors) if college.micro_majors else 0

    return CollegeProfile(
        id=college.id,
        name=college.name,
        code=college.code,
        stats=stats,
        micro_major_count=micro_major_count,
        warnings=warnings,
        attributions=attributions,
        key_indicators_comparison=key_indicators,
    )
