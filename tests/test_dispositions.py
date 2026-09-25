"""预警处置闭环接口验收测试。

覆盖：并发认领、期限计算、复核退回、复发关联、权限隔离、
不可变动作记录、重新打开保留上一次闭环、重启后的待办查询。
"""

import os
import tempfile
import threading
import unittest
from datetime import date, timedelta

# 在导入应用前指定临时数据库
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from main import app  # noqa: E402
from app.core import init_db, SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    College,
    Warning,
    WarningType,
    WarningLevel,
    WarningStatus,
    WarningDisposition,
    DispositionActionLog,
    WarningRecurrence,
)
from app.services.workflow_rules import CaseState  # noqa: E402
from app.services.disposition_service import maybe_link_recurrence  # noqa: E402


init_db()

API = "/api/v1"

MANAGER = {"X-User-Id": "m1", "X-User-Name": "Manager-Zhang", "X-User-Role": "manager"}
OP1 = {"X-User-Id": "op1", "X-User-Name": "C1-Wang", "X-User-Role": "operator", "X-College-Id": "1"}
OP1B = {"X-User-Id": "op1b", "X-User-Name": "C1-Li", "X-User-Role": "operator", "X-College-Id": "1"}
OP2 = {"X-User-Id": "op2", "X-User-Name": "C2-Zhao", "X-User-Role": "operator", "X-College-Id": "2"}
REV1 = {"X-User-Id": "rev1", "X-User-Name": "C1-Reviewer", "X-User-Role": "reviewer", "X-College-Id": "1"}
REV2 = {"X-User-Id": "rev2", "X-User-Name": "C2-Reviewer", "X-User-Role": "reviewer", "X-College-Id": "2"}


def make_warning(wid: int, college_id: int = 1, level=WarningLevel.RED,
                indicator: str = "confirmed_rate") -> Warning:
    db = SessionLocal()
    try:
        if db.query(College).filter(College.id == college_id).first() is None:
            db.add(College(id=college_id, name=f"学院{college_id}", code=f"C{college_id}"))
        w = Warning(
            id=wid,
            warning_type=WarningType.CONFIRMED_RATE_DECLINE,
            warning_level=level,
            status=WarningStatus.ACTIVE,
            target_type="college",
            target_id=college_id,
            target_name=f"学院{college_id}",
            indicator=indicator,
            current_value=60.0,
            start_year=2023,
            end_year=2025,
            decline_count=4,
            description="测试预警",
        )
        db.add(w)
        db.commit()
    finally:
        db.close()
    return w


class DispositionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        # 每用例使用全新的预警ID，互不干扰
        self.wid = int(f"1{id(self) % 100000}")
        while True:
            db = SessionLocal()
            exists = db.query(Warning).filter(Warning.id == self.wid).first()
            db.close()
            if not exists:
                break
            self.wid += 1
        make_warning(self.wid, college_id=1)

    def _dispatch(self, headers=MANAGER, duration_days=None, due=None, scope="教务部门"):
        body = {"warning_id": self.wid, "scope": scope}
        if duration_days is not None:
            body["duration_days"] = duration_days
        if due is not None:
            body["due_date"] = due.isoformat()
        resp = self.client.post(f"{API}/dispositions/dispatch", json=body, headers=headers)
        return resp

    def _full_close(self, warning_id=None, op=OP1, rev=REV1):
        """分派 -> 认领 -> 提交复核 -> 复核通过，返回复核通过响应。"""
        wid = warning_id or self.wid
        r = self.client.post(f"{API}/dispositions/dispatch",
                             json={"warning_id": wid, "scope": "教务部门"}, headers=MANAGER)
        assert r.status_code == 200, r.text
        did = r.json()["disposition"]["id"]
        assert self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=op).status_code == 200
        r = self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "已完成整改", "root_cause": "课程与岗位脱节", "improvement": "重构实训模块"},
            headers=op,
        )
        assert r.status_code == 200, r.text
        r = self.client.post(
            f"{API}/dispositions/{did}/approve",
            json={"review_comment": "整改到位"}, headers=rev,
        )
        assert r.status_code == 200, r.text
        return did, r

    # ---------- 分派与期限 ----------

    def test_dispatch_default_sla_by_red_level(self):
        # 红色预警默认 7 个自然日
        r = self._dispatch(duration_days=None)
        self.assertEqual(r.status_code, 200, r.text)
        due = date.fromisoformat(r.json()["disposition"]["due_date"])
        self.assertEqual(due, date.today() + timedelta(days=7))
        self.assertEqual(r.json()["disposition"]["state"], CaseState.NEW.value)

    def test_dispatch_explicit_duration_and_due_date(self):
        r = self._dispatch(duration_days=3)
        self.assertEqual(date.fromisoformat(r.json()["disposition"]["due_date"]),
                         date.today() + timedelta(days=3))
        # 显式 due_date 优先
        r2 = self.client.post(
            f"{API}/dispositions/dispatch",
            json={"warning_id": self.wid, "scope": "学工部门",
                  "duration_days": 30, "due_date": (date.today() + timedelta(days=5)).isoformat()},
            headers=MANAGER,
        )
        # 仍处于 new，可改派；显式期限生效
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(
            date.fromisoformat(r2.json()["disposition"]["due_date"]),
            date.today() + timedelta(days=5),
        )

    def test_dispatch_yellow_level_sla_is_15_days(self):
        wid = self.wid + 700000
        make_warning(wid, college_id=1, level=WarningLevel.YELLOW)
        r = self.client.post(f"{API}/dispositions/dispatch",
                             json={"warning_id": wid, "scope": "学院领导"}, headers=MANAGER)
        self.assertEqual(date.fromisoformat(r.json()["disposition"]["due_date"]),
                         date.today() + timedelta(days=15))

    def test_dispatch_requires_manager(self):
        r = self._dispatch(headers=OP1)
        self.assertEqual(r.status_code, 403)
        r = self._dispatch(headers=REV1)
        self.assertEqual(r.status_code, 403)

    def test_dispatch_invalid_scope(self):
        r = self.client.post(
            f"{API}/dispositions/dispatch",
            json={"warning_id": self.wid, "scope": "不存在的部门"}, headers=MANAGER,
        )
        self.assertEqual(r.status_code, 400)

    def test_identity_headers_required(self):
        r = self.client.post(f"{API}/dispositions/dispatch",
                             json={"warning_id": self.wid, "scope": "教务部门"})
        self.assertEqual(r.status_code, 401)

    # ---------- 并发认领 ----------

    def test_concurrent_claim_only_one_wins(self):
        did = self._dispatch().json()["disposition"]["id"]
        results = []
        barrier = threading.Barrier(2)

        def claim(headers):
            barrier.wait()
            r = self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=headers)
            results.append((headers["X-User-Id"], r.status_code,
                            r.json().get("disposition", {}).get("assignee_id") if r.status_code == 200 else None))

        t1 = threading.Thread(target=claim, args=(OP1,))
        t2 = threading.Thread(target=claim, args=(OP1B,))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [r for r in results if r[1] == 200]
        losers = [r for r in results if r[1] == 409]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(winners[0][1], 200)
        self.assertEqual(winners[0][2], winners[0][0])

        # 落库后处理人唯一
        db = SessionLocal()
        d = db.query(WarningDisposition).filter_by(id=did).first()
        self.assertIn(d.assignee_id, {"op1", "op1b"})
        self.assertEqual(d.state, CaseState.CLAIMED.value)
        db.close()

    def test_reviewer_cannot_claim(self):
        did = self._dispatch().json()["disposition"]["id"]
        r = self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=REV1)
        self.assertEqual(r.status_code, 403)

    # ---------- 状态推进 / 复核 ----------

    def test_submit_requires_root_cause_and_improvement(self):
        did = self._dispatch().json()["disposition"]["id"]
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        r = self.client.post(f"{API}/dispositions/{did}/submit-review",
                             json={"summary": "做完了"}, headers=OP1)
        self.assertEqual(r.status_code, 400)

    def test_review_return_goes_back_to_processing(self):
        did = self._dispatch().json()["disposition"]["id"]
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "提交", "root_cause": "原因", "improvement": "措施"}, headers=OP1,
        )
        r = self.client.post(f"{API}/dispositions/{did}/return",
                             json={"review_comment": "证据不足，补充企业访谈"}, headers=REV1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["disposition"]["state"], CaseState.PROCESSING.value)
        self.assertIn("证据不足", r.json()["disposition"]["review_comment"])

        # 退回后预警仍未解决，且不能直接复核通过（必须重新提交）
        db = SessionLocal()
        self.assertEqual(db.get(Warning, self.wid).status, WarningStatus.ACTIVE)
        db.close()
        r2 = self.client.post(f"{API}/dispositions/{did}/approve",
                              json={"review_comment": "通过"}, headers=REV1)
        self.assertEqual(r2.status_code, 409)

    def test_approve_blocks_same_person_review(self):
        did = self._dispatch().json()["disposition"]["id"]
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "s", "root_cause": "c", "improvement": "i"}, headers=OP1,
        )
        # 处理人自己复核（即便角色提升为 manager）也不行
        same_person_manager = {"X-User-Id": "op1", "X-User-Role": "manager"}
        r = self.client.post(f"{API}/dispositions/{did}/approve",
                             json={"review_comment": "自批"}, headers=same_person_manager)
        self.assertEqual(r.status_code, 409)

    def test_only_approval_resolves_warning(self):
        did, r = self._full_close()
        self.assertEqual(r.json()["disposition"]["state"], CaseState.CLOSED.value)
        db = SessionLocal()
        self.assertEqual(db.get(Warning, self.wid).status, WarningStatus.RESOLVED)
        db.close()
        # 通用编辑接口不得改写状态
        r = self.client.put(f"{API}/warnings/{self.wid}",
                            json={"status": "已解决"}, headers=MANAGER)
        self.assertEqual(r.status_code, 403)

    # ---------- 不可变动作记录 ----------

    def test_action_logs_are_append_only(self):
        did, _ = self._full_close()
        r = self.client.get(f"{API}/dispositions/{did}/logs", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        actions = [row["action"] for row in r.json()]
        self.assertEqual(actions, ["dispatch", "claim", "submit_review", "approve"])
        # 序号严格递增且记录了轮次
        self.assertEqual([row["seq"] for row in r.json()], [1, 2, 3, 4])
        self.assertTrue(all(row["cycle_no"] == 1 for row in r.json()))

        db = SessionLocal()
        log = db.query(DispositionActionLog).first()
        with self.assertRaises(RuntimeError):
            log.note = "篡改"
            db.flush()
        db.rollback()

        # 数据库触发器拒绝裸 SQL 的 UPDATE/DELETE
        from sqlalchemy.exc import IntegrityError, OperationalError
        with self.assertRaises((IntegrityError, OperationalError)):
            db.execute(text("UPDATE disposition_action_logs SET note='x' WHERE id = :i"),
                       {"i": log.id})
        with self.assertRaises((IntegrityError, OperationalError)):
            db.execute(text("DELETE FROM disposition_action_logs WHERE id = :i"),
                       {"i": log.id})
        db.rollback()
        db.close()

    # ---------- 重新打开保留上一次闭环 ----------

    def test_reopen_keeps_previous_cycle_archive(self):
        did, _ = self._full_close()
        r = self.client.post(
            f"{API}/dispositions/{did}/reopen",
            json={"reason": "指标再次下滑", "duration_days": 10}, headers=MANAGER,
        )
        self.assertEqual(r.status_code, 200, r.text)
        detail = r.json()["disposition"]
        self.assertEqual(detail["cycle_no"], 2)
        self.assertEqual(detail["state"], CaseState.PROCESSING.value)
        # 上一轮归档仍在，根因/措施保留
        self.assertEqual(len(detail["cycles"]), 1)
        self.assertEqual(detail["cycles"][0]["root_cause"], "课程与岗位脱节")
        self.assertEqual(detail["cycles"][0]["improvement"], "重构实训模块")

        db = SessionLocal()
        self.assertEqual(db.get(Warning, self.wid).status, WarningStatus.ACTIVE)
        db.close()

        # 第二轮再次复核通过 -> 两条闭环归档，动作日志跨轮次连续
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "二次整改", "root_cause": "行业需求变化", "improvement": "新增企业导师"},
            headers=OP1,
        )
        r2 = self.client.post(f"{API}/dispositions/{did}/approve",
                              json={"review_comment": "通过"}, headers=REV1)
        self.assertEqual(r2.status_code, 200, r2.text)
        detail2 = r2.json()["disposition"]
        self.assertEqual(len(detail2["cycles"]), 2)
        self.assertEqual(detail2["cycles"][0]["cycle_no"], 1)
        self.assertEqual(detail2["cycles"][1]["root_cause"], "行业需求变化")

        logs = self.client.get(f"{API}/dispositions/{did}/logs", headers=MANAGER).json()
        self.assertEqual([row["action"] for row in logs][-2:], ["submit_review", "approve"])
        self.assertEqual(logs[-1]["cycle_no"], 2)
        self.assertEqual(len(logs), 7)
        self.assertEqual([row["action"] for row in logs],
                         ["dispatch", "claim", "submit_review", "approve",
                          "reopen", "submit_review", "approve"])

    # ---------- 复发关联 ----------

    def test_recurrence_link_and_origin_snapshot(self):
        # 第一张预警完整闭环
        origin_wid = self.wid
        self._full_close(warning_id=origin_wid)

        # 同对象同指标的新预警
        new_wid = self.wid + 500001
        make_warning(new_wid, college_id=1)
        r = self.client.post(
            f"{API}/recurrences",
            json={"warning_id": new_wid, "origin_warning_id": origin_wid,
                  "reason": "2026届落实率再次跌破"},
            headers=MANAGER,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["origin"]["closed_cycles"], 1)
        self.assertEqual(r.json()["origin"]["last_root_cause"], "课程与岗位脱节")

        # 新预警处置单携带复发来源与上一次闭环摘要
        r = self.client.get(f"{API}/dispositions", params={"warning_id": new_wid}, headers=MANAGER)
        self.assertEqual(r.status_code == 200, True)
        disp = r.json()["data"][0]
        self.assertEqual(disp["recurrence_of_id"], origin_wid)
        self.assertIsNotNone(disp["recurrence_origin"])
        self.assertEqual(disp["recurrence_origin"]["last_improvement"], "重构实训模块")

    def test_recurrence_rejects_unclosed_or_different_risk(self):
        # origin 未闭环
        origin = self.wid
        self._dispatch()
        other = self.wid + 500002
        make_warning(other, college_id=1)
        r = self.client.post(
            f"{API}/recurrences",
            json={"warning_id": other, "origin_warning_id": origin, "reason": "x"},
            headers=MANAGER,
        )
        self.assertEqual(r.status_code, 400)

        # 不同风险（指标不同）
        other_indicator = self.wid + 500003
        make_warning(other_indicator, college_id=1, indicator="aligned_rate")
        self._full_close(warning_id=origin)
        r = self.client.post(
            f"{API}/recurrences",
            json={"warning_id": other_indicator, "origin_warning_id": origin, "reason": "x"},
            headers=MANAGER,
        )
        self.assertEqual(r.status_code, 400)

        # 非管理处不能登记
        r = self.client.post(
            f"{API}/recurrences",
            json={"warning_id": other, "origin_warning_id": origin, "reason": "x"},
            headers=OP1,
        )
        self.assertEqual(r.status_code, 403)

    def test_auto_recurrence_on_detection(self):
        """闭环后同一风险再次触发检测，自动建立复发关联与新一轮处置单。"""
        origin = self.wid
        self._full_close(warning_id=origin)

        db = SessionLocal()
        new_w = Warning(
            warning_type=WarningType.CONFIRMED_RATE_DECLINE,
            warning_level=WarningLevel.ORANGE,
            status=WarningStatus.ACTIVE,
            target_type="college", target_id=1, target_name="学院1",
            indicator="confirmed_rate", current_value=55.0,
            start_year=2024, end_year=2026, decline_count=3,
        )
        db.add(new_w); db.flush()
        maybe_link_recurrence(db, new_w)
        db.commit()
        link = db.query(WarningRecurrence).filter_by(warning_id=new_w.id).first()
        self.assertIsNotNone(link)
        self.assertEqual(link.origin_warning_id, origin)
        disp = db.query(WarningDisposition).filter_by(warning_id=new_w.id).first()
        self.assertEqual(disp.recurrence_of_id, origin)
        db.close()

    # ---------- 权限隔离 ----------

    def test_college_scope_isolation(self):
        # 学院1 的预警
        did = self._dispatch().json()["disposition"]["id"]
        # 学院2 处理人不可见、不可认领
        r = self.client.get(f"{API}/dispositions/{did}", headers=OP2)
        self.assertEqual(r.status_code, 403)
        r = self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP2)
        self.assertEqual(r.status_code, 403)
        # 学院2 复核人不可退回/通过
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "s", "root_cause": "c", "improvement": "i"}, headers=OP1,
        )
        r = self.client.post(f"{API}/dispositions/{did}/approve",
                             json={"review_comment": "越权"}, headers=REV2)
        self.assertEqual(r.status_code, 403)
        # 待办列表不泄漏其他学院
        todos = self.client.get(f"{API}/todos", headers=OP2).json()["data"]
        self.assertFalse(any(t["warning_id"] == self.wid for t in todos))
        # 管理处可见全部
        r = self.client.get(f"{API}/dispositions/{did}", headers=MANAGER)
        self.assertEqual(r.status_code, 200)

    def test_operator_without_college_header_rejected(self):
        did = self._dispatch().json()["disposition"]["id"]
        bare = {"X-User-Id": "opx", "X-User-Role": "operator"}
        r = self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=bare)
        self.assertEqual(r.status_code, 403)

    # ---------- 逾期 / 待办（无后台进程，重启可查） ----------

    def test_overdue_todo_without_background_process(self):
        # 分派时期限直接落在昨天
        self._dispatch(due=date.today() - timedelta(days=1))
        r = self.client.get(f"{API}/todos", headers=MANAGER)
        self.assertEqual(r.status_code, 200)
        todo = next(t for t in r.json()["data"] if t["warning_id"] == self.wid)
        self.assertIn("overdue", todo["reasons"])
        self.assertEqual(todo["days_overdue"], 1)
        self.assertGreaterEqual(r.json()["overdue_count"], 1)

        # 已闭环/已撤销的不进待办
        did = next(
            d["id"] for d in self.client.get(f"{API}/dispositions", headers=MANAGER).json()["data"]
            if d["warning_id"] == self.wid
        )
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "s", "root_cause": "c", "improvement": "i"}, headers=OP1,
        )
        self.client.post(f"{API}/dispositions/{did}/approve",
                         json={"review_comment": "ok"}, headers=REV1)
        todos_after = self.client.get(f"{API}/todos", headers=MANAGER).json()["data"]
        self.assertFalse(any(t["warning_id"] == self.wid for t in todos_after))

    def test_todos_survive_restart(self):
        self._dispatch(due=date.today() - timedelta(days=2))
        # 模拟服务重启：全新进程风格的独立 engine 直接查库，且重新实例化客户端
        fresh_engine = create_engine(f"sqlite:///{_db_path}")
        FreshSession = sessionmaker(bind=fresh_engine)
        s = FreshSession()
        row = s.query(WarningDisposition).join(
            Warning, Warning.id == WarningDisposition.warning_id
        ).filter(
            Warning.id == self.wid
        ).first()
        self.assertEqual(row.state, CaseState.NEW.value)
        self.assertTrue((date.today() - row.due_date).days == 2)
        s.close(); fresh_engine.dispose()

        new_client = TestClient(app)
        todos = new_client.get(f"{API}/todos", headers=MANAGER).json()["data"]
        self.assertTrue(any(t["warning_id"] == self.wid for t in todos))

    def test_waiting_review_enters_reviewer_todo(self):
        did = self._dispatch().json()["disposition"]["id"]
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.client.post(
            f"{API}/dispositions/{did}/submit-review",
            json={"summary": "s", "root_cause": "c", "improvement": "i"}, headers=OP1,
        )
        # 复核人待办出现待复核事项
        todos = self.client.get(f"{API}/todos", headers=REV1).json()["data"]
        todo = next(t for t in todos if t["warning_id"] == self.wid)
        self.assertEqual(todo["state"], CaseState.WAITING_REVIEW.value)
        self.assertIn("waiting_review", todo["reasons"])
        # 其他学院复核人看不到
        self.assertFalse(any(
            t["warning_id"] == self.wid
            for t in self.client.get(f"{API}/todos", headers=REV2).json()["data"]
        ))

    def test_lease_expired_allows_takeover_and_renewal(self):
        did = self._dispatch().json()["disposition"]["id"]
        # 负天数租约：立即过期
        self.client.post(f"{API}/dispositions/{did}/claim",
                         json={"lease_days": -1}, headers=OP1)
        todos = self.client.get(f"{API}/todos", headers=OP1).json()["data"]
        todo = next(t for t in todos if t["warning_id"] == self.wid)
        self.assertIn("lease_expired", todo["reasons"])

        # 租约过期后其他处理人可接手，状态保持 processing/claimed，仅换人
        r = self.client.post(f"{API}/dispositions/{did}/claim", json={"lease_days": 3}, headers=OP1B)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["disposition"]["assignee_id"], "op1b")
        self.assertEqual(r.json()["disposition"]["state"], CaseState.CLAIMED.value)
        # 本人租约未过期时不能被重复接手（新租约 3 天）
        r = self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        self.assertEqual(r.status_code, 409)

    # ---------- 证据摘要 ----------

    def test_evidence_summary_and_role_rule(self):
        did = self._dispatch().json()["disposition"]["id"]
        self.client.post(f"{API}/dispositions/{did}/claim", json={}, headers=OP1)
        r = self.client.post(
            f"{API}/dispositions/{did}/evidences",
            json={"file_name": "整改报告.pdf", "content_summary": "含3家企业访谈记录",
                  "file_digest": "abc123", "digest_alg": "sha-256", "file_size": 2048},
            headers=OP1,
        )
        self.assertEqual(r.status_code, 200, r.text)
        detail = self.client.get(f"{API}/dispositions/{did}", headers=OP1).json()
        self.assertEqual(detail["evidences"][0]["content_summary"], "含3家企业访谈记录")
        # 复核人不能上传证据
        r = self.client.post(
            f"{API}/dispositions/{did}/evidences",
            json={"file_name": "x", "content_summary": "y"}, headers=REV1,
        )
        self.assertEqual(r.status_code, 403)
        # 证据动作也进不可变日志
        actions = [row["action"] for row in
                   self.client.get(f"{API}/dispositions/{did}/logs", headers=OP1).json()]
        self.assertIn("upload_evidence", actions)


if __name__ == "__main__":
    unittest.main()
