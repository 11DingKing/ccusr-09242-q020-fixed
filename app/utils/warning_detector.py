import json
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from datetime import datetime

from app.models import (
    Graduate,
    College,
    MicroMajor,
    ProvinceReferenceLine,
    Warning,
    DestinationStatus,
    DestinationType,
    WarningType,
    WarningLevel,
    WarningStatus,
    OPEN_WARNING_STATUSES,
)
from app.utils.stats_calculator import calculate_group_stats


DECLINE_THRESHOLD_YELLOW = 2
DECLINE_THRESHOLD_ORANGE = 3
DECLINE_THRESHOLD_RED = 4


GAP_THRESHOLD_YELLOW = 5.0
GAP_THRESHOLD_ORANGE = 10.0
GAP_THRESHOLD_RED = 15.0


def calculate_yearly_indicators(
    db: Session,
    target_type: str,
    target_id: int,
) -> List[Dict]:
    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]

    yearly_data = []
    for year in years:
        query = db.query(Graduate).filter(Graduate.graduation_year == year)

        if target_type == "micro_major":
            query = query.filter(
                Graduate.has_micro_major == True,
                Graduate.micro_major_id == target_id,
            )
        elif target_type == "college":
            query = query.filter(Graduate.college_id == target_id)

        graduates = query.all()
        if not graduates:
            continue

        stats = calculate_group_stats(graduates)

        yearly_data.append({
            "year": year,
            "confirmed_rate": stats.confirmed_rate,
            "aligned_rate": stats.aligned_rate,
            "total_count": stats.total_count,
            "confirmed_count": stats.confirmed_count,
            "aligned_count": stats.aligned_count,
        })

    return yearly_data


def get_province_reference_line(db: Session, year: int, indicator: str) -> Optional[ProvinceReferenceLine]:
    return db.query(ProvinceReferenceLine).filter(
        ProvinceReferenceLine.graduation_year == year,
        ProvinceReferenceLine.indicator == indicator,
    ).first()


def detect_continuous_decline(
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, int, int, List[Dict]]]:
    if len(yearly_data) < 2:
        return []

    declines = []
    current_start = 0
    current_decline_count = 0
    current_sequence = []

    for i in range(1, len(yearly_data)):
        prev_val = yearly_data[i - 1][indicator]
        curr_val = yearly_data[i][indicator]

        if curr_val < prev_val:
            if current_decline_count == 0:
                current_start = i - 1
            current_decline_count += 1
            current_sequence.append(yearly_data[i])
        else:
            if current_decline_count >= DECLINE_THRESHOLD_YELLOW:
                declines.append((
                    yearly_data[current_start]["year"],
                    yearly_data[i - 1]["year"],
                    current_decline_count,
                    yearly_data[current_start:i],
                ))
            current_decline_count = 0
            current_sequence = []

    if current_decline_count >= DECLINE_THRESHOLD_YELLOW:
        declines.append((
            yearly_data[current_start]["year"],
            yearly_data[-1]["year"],
            current_decline_count,
            yearly_data[current_start:],
        ))

    return declines


def determine_decline_level(decline_count: int) -> WarningLevel:
    if decline_count >= DECLINE_THRESHOLD_RED:
        return WarningLevel.RED
    elif decline_count >= DECLINE_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def determine_gap_level(gap: float) -> WarningLevel:
    if gap >= GAP_THRESHOLD_RED:
        return WarningLevel.RED
    elif gap >= GAP_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def detect_below_province_line(
    db: Session,
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, float, float, float]]:
    below_entries = []

    for data in yearly_data:
        reference_line = get_province_reference_line(db, data["year"], indicator)
        if not reference_line:
            continue

        current_value = data[indicator]
        if current_value < reference_line.threshold:
            gap = reference_line.threshold - current_value
            below_entries.append((
                data["year"],
                current_value,
                reference_line.threshold,
                gap,
            ))

    return below_entries


def get_target_name(db: Session, target_type: str, target_id: int) -> str:
    if target_type == "micro_major":
        obj = db.query(MicroMajor).filter(MicroMajor.id == target_id).first()
        return obj.name if obj else "未知微专业"
    elif target_type == "college":
        obj = db.query(College).filter(College.id == target_id).first()
        return obj.name if obj else "未知学院"
    return "未知"


def check_existing_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    start_year: int,
    end_year: int,
) -> Optional[Warning]:
    return db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status.in_(OPEN_WARNING_STATUSES),
        Warning.start_year <= start_year,
        Warning.end_year >= end_year,
    ).first()


def _find_closed_match(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
) -> Optional[Warning]:
    """找到同一风险上一次复核闭环的预警，用于复发关联。"""
    return db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status == WarningStatus.RESOLVED,
    ).order_by(Warning.id.desc()).first()


def _reopen_if_recurred(db: Session, warning: Warning, reason: str) -> bool:
    """已闭环预警再次命中 -> 复发重开并关联上一轮处置单；处理中的预警不重复开单。"""
    # 延迟导入避免模块循环依赖。
    from app.services.disposal_service import reopen_resolved_warning
    return reopen_resolved_warning(db, warning, reason)


def _reopen_when_new(db: Session, warning: Warning, reason: str,
                     new_end_year: int, new_value: Optional[float] = None) -> bool:
    """仅当命中信号相对闭环基线构成新复发时才重开。"""
    from app.services.disposal_service import is_new_recurrence
    if not is_new_recurrence(db, warning, new_end_year, new_value):
        return False
    return _reopen_if_recurred(db, warning, reason)


def create_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    warning_level: WarningLevel,
    indicator: str,
    current_value: float,
    start_year: int,
    end_year: int,
    decline_count: int,
    decline_details: List[Dict],
    province_value: Optional[float] = None,
    gap: Optional[float] = None,
    description: Optional[str] = None,
) -> Warning:
    existing = check_existing_warning(
        db, target_type, target_id, warning_type, indicator, start_year, end_year
    )
    if existing:
        # 进行中（含处置/待复核/复发）的预警只更新指标，状态由处置闭环决定，
        # 检测任务无权把它改为已解决。
        existing.current_value = current_value
        existing.end_year = end_year
        existing.decline_count = decline_count
        existing.decline_details = json.dumps(decline_details, ensure_ascii=False)
        existing.warning_level = warning_level
        existing.province_value = province_value
        existing.gap = gap
        existing.description = description
        db.flush()
        return existing

    closed_match = _find_closed_match(db, target_type, target_id, warning_type, indicator)
    if closed_match and _reopen_when_new(
        db,
        closed_match,
        f"指标再次触发（{start_year}-{end_year}届连续下降），上一轮闭环后风险复发",
        new_end_year=end_year,
        new_value=current_value,
    ):
        closed_match.warning_level = warning_level
        closed_match.current_value = current_value
        closed_match.province_value = province_value
        closed_match.gap = gap
        closed_match.start_year = start_year
        closed_match.end_year = end_year
        closed_match.decline_count = decline_count
        closed_match.decline_details = json.dumps(decline_details, ensure_ascii=False)
        closed_match.description = description
        db.flush()
        return closed_match
    if closed_match:
        # 同一批数据重复命中且未出现新恶化：保持已闭环，不新建重复预警，
        # 系统不能在复核通过后自行把风险重新标记为未解决。
        return closed_match

    target_name = get_target_name(db, target_type, target_id)

    warning = Warning(
        warning_type=warning_type,
        warning_level=warning_level,
        status=WarningStatus.ACTIVE,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        indicator=indicator,
        current_value=current_value,
        province_value=province_value,
        gap=gap,
        start_year=start_year,
        end_year=end_year,
        decline_count=decline_count,
        decline_details=json.dumps(decline_details, ensure_ascii=False),
        description=description,
    )
    db.add(warning)
    db.flush()
    return warning


def run_warning_detection_for_target(
    db: Session,
    target_type: str,
    target_id: int,
) -> List[Warning]:
    yearly_data = calculate_yearly_indicators(db, target_type, target_id)
    if not yearly_data:
        return []

    created_warnings = []

    for indicator, warning_type, label in [
        ("confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE, "去向落实率"),
        ("aligned_rate", WarningType.ALIGNED_RATE_DECLINE, "对口就业率"),
    ]:
        declines = detect_continuous_decline(yearly_data, indicator)
        for start_year, end_year, decline_count, sequence in declines:
            level = determine_decline_level(decline_count)
            current_value = sequence[-1][indicator]
            description = (
                f"{label}自{start_year}届至{end_year}届连续{decline_count}届下降，"
                f"从{sequence[0][indicator]:.2f}%降至{current_value:.2f}%，"
                f"累计下降{sequence[0][indicator] - current_value:.2f}个百分点。"
            )

            warning = create_warning(
                db=db,
                target_type=target_type,
                target_id=target_id,
                warning_type=warning_type,
                warning_level=level,
                indicator=indicator,
                current_value=current_value,
                start_year=start_year,
                end_year=end_year,
                decline_count=decline_count,
                decline_details=sequence,
                description=description,
            )
            created_warnings.append(warning)

    for indicator, label in [
        ("confirmed_rate", "去向落实率"),
        ("aligned_rate", "对口就业率"),
    ]:
        below_entries = detect_below_province_line(db, yearly_data, indicator)
        for year, current_value, threshold, gap in below_entries:
            level = determine_gap_level(gap)
            description = (
                f"{year}届{label}为{current_value:.2f}%，"
                f"低于全省预警阈值{threshold:.2f}%，"
                f"差距为{gap:.2f}个百分点。"
            )

            existing = db.query(Warning).filter(
                Warning.target_type == target_type,
                Warning.target_id == target_id,
                Warning.warning_type == WarningType.BELOW_PROVINCE_LINE,
                Warning.indicator == indicator,
                Warning.status.in_(OPEN_WARNING_STATUSES),
                Warning.end_year == year,
            ).first()

            if existing:
                existing.current_value = current_value
                existing.warning_level = level
                existing.province_value = threshold
                existing.gap = gap
                existing.description = description
                created_warnings.append(existing)
                continue

            closed_existing = (
                db.query(Warning)
                .filter(
                    Warning.target_type == target_type,
                    Warning.target_id == target_id,
                    Warning.warning_type == WarningType.BELOW_PROVINCE_LINE,
                    Warning.indicator == indicator,
                    Warning.status == WarningStatus.RESOLVED,
                )
                .order_by(Warning.id.desc())
                .first()
            )
            if closed_existing and _reopen_when_new(
                db,
                closed_existing,
                f"{year}届指标再次低于全省对照线，上一轮闭环后风险复发",
                new_end_year=year,
                new_value=current_value,
            ):
                closed_existing.current_value = current_value
                closed_existing.warning_level = level
                closed_existing.province_value = threshold
                closed_existing.gap = gap
                closed_existing.start_year = year
                closed_existing.end_year = year
                closed_existing.decline_count = 1
                closed_existing.decline_details = json.dumps(
                    [d for d in yearly_data if d["year"] == year], ensure_ascii=False
                )
                closed_existing.description = description
                created_warnings.append(closed_existing)
                continue
            if closed_existing:
                # 同届数据重复检测且未继续恶化：保持已闭环，不重复开单。
                continue

            target_name = get_target_name(db, target_type, target_id)
            warning = Warning(
                warning_type=WarningType.BELOW_PROVINCE_LINE,
                warning_level=level,
                status=WarningStatus.ACTIVE,
                target_type=target_type,
                target_id=target_id,
                target_name=target_name,
                indicator=indicator,
                current_value=current_value,
                province_value=threshold,
                gap=gap,
                start_year=year,
                end_year=year,
                decline_count=1,
                decline_details=json.dumps([
                    d for d in yearly_data if d["year"] == year
                ], ensure_ascii=False),
                description=description,
            )
            db.add(warning)
            db.flush()
            created_warnings.append(warning)

    return created_warnings


def run_full_warning_detection(db: Session) -> Dict:
    all_warnings = []

    micro_majors = db.query(MicroMajor).all()
    for mm in micro_majors:
        warnings = run_warning_detection_for_target(db, "micro_major", mm.id)
        all_warnings.extend(warnings)

    colleges = db.query(College).all()
    for college in colleges:
        warnings = run_warning_detection_for_target(db, "college", college.id)
        all_warnings.extend(warnings)

    db.commit()

    return {
        "total_warnings": len(all_warnings),
        "micro_major_warnings": sum(1 for w in all_warnings if w.target_type == "micro_major"),
        "college_warnings": sum(1 for w in all_warnings if w.target_type == "college"),
    }


def get_target_warnings(
    db: Session,
    target_type: str,
    target_id: int,
    status: Optional[str] = None,
) -> List[Warning]:
    query = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
    )

    if status:
        status_enum = next(
            (s for s in WarningStatus if s.value == status or s.name == status),
            None,
        )
        if status_enum is not None:
            query = query.filter(Warning.status == status_enum)

    return query.order_by(Warning.created_at.desc()).all()
