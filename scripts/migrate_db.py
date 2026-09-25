import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import init_db, SessionLocal
from app.models import (
    Base,
    ProvinceReferenceLine,
    Warning,
    AttributionRecord,
    DisposalCase,
    DisposalEvidence,
    DisposalActionLog,
    WarningStatus,
)


def migrate():
    print("=" * 60)
    print("正在执行数据库迁移...")
    print("=" * 60)

    print("\n1. 创建新表结构...")
    init_db()
    print("   表结构创建完成（含处置闭环相关表）")

    db = SessionLocal()
    try:
        print("\n2. 检查新表数据...")
        bench_count = db.query(ProvinceReferenceLine).count()
        warning_count = db.query(Warning).count()
        attribution_count = db.query(AttributionRecord).count()
        case_count = db.query(DisposalCase).count()
        evidence_count = db.query(DisposalEvidence).count()
        log_count = db.query(DisposalActionLog).count()

        print(f"   省基准线表: {bench_count} 条记录")
        print(f"   预警表: {warning_count} 条记录")
        print(f"   归因记录表: {attribution_count} 条记录")
        print(f"   处置单表: {case_count} 条记录")
        print(f"   证据摘要表: {evidence_count} 条记录")
        print(f"   动作记录表: {log_count} 条记录")

        # 旧数据兼容性校验：预警 status 列存枚举名（ACTIVE 等），
        # 新增状态无需回填历史数据；旧 ACTIVE 仍属于未闭环状态。
        valid_names = [s.name for s in WarningStatus]
        stale = db.query(Warning).filter(Warning.status.notin_(valid_names)).count()
        if stale:
            print(f"\n   注意: {stale} 条预警状态无法识别，请人工核对")

        if bench_count == 0:
            print("\n3. 省基准线表为空，请运行 python scripts/init_data.py 初始化数据")
        else:
            print("\n3. 数据库迁移完成！")

    except Exception as e:
        print(f"\n迁移失败: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()

    print("\n" + "=" * 60)


if __name__ == "__main__":
    migrate()
