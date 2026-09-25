"""处置闭环接口验收：

覆盖并发认领、处理期限计算、复核退回、复发关联、权限隔离、重启后待办查询。
"""

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

# 必须在导入应用前指向临时数据库。
_tmp_dir = tempfile.mkdtemp(prefix="gradtrack-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from app.core import engine, SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    College,
    Warning,
    WarningType,
    WarningLevel,
    WarningStatus,
    DisposalCase,
)
from app.services import disposal_service as svc  # noqa: E402
from app.services.workflow_rules import CaseState  # noqa: E402


HEADERS_MANAGER = {
    "X-User-Id": "mgr1", "X-User-Name": "Manager-Zhang", "X-User-Role": "manager",
}
HEADERS_OP1 = {
    "X-User-Id": "op1", "X-User-Name": "Operator-Li",
    "X-User-Role": "operator", "X-College-Id": "1", "X-College-Name": "CS-College",
}
HEADERS_OP2 = {
    "X-User-Id": "op2", "X-User-Name": "Operator-Wang",
    "X-User-Role": "operator", "X-College-Id": "2", "X-College-Name": "SE-College",
}
HEADERS_REVIEWER = {
    "X-User-Id": "rev1", "X-User-Name": "Reviewer-Zhao", "X-User-Role": "reviewer",
}


class DisposalFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def setUp(self):
        db = SessionLocal()
        # 每个用例使用全新的学院与预警。
        self.college_id = int(datetime.now().timestamp() * 1000) % 100000
        college = College(id=self.college_id, code=f"C{self.college_id}", name=f"学院{self.college_id}")
        db.add(college)
        db.flush()
        warning = Warning(
            warning_type=WarningType.CONFIRMED_RATE_DECLINE,
            warning_level=WarningLevel.YELLOW,
            status=WarningStatus.ACTIVE,
            target_type="college", target_id=self.college_id,
            target_name=college.name, indicator="confirmed_rate",
            current_value=70.0, province_value=80.0, gap=10.0,
            start_year=2023, end_year=2025, decline_count=2,
            description="测试预警",
        )
        db.add(warning)
        db.commit()
        self.warning_id = warning.id
        self.college_headers = dict(HEADERS_OP1)
        self.college_headers["X-College-Id"] = str(self.college_id)
        self.college_headers["X-College-Name"] = f"College-{self.college_id}"
        db.close()

    # ------------------------------------------------------------------
    def test_deadline_is_computed_from_dispatch_time_and_sla(self):
        before = datetime.now()
        r = self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 5},
            headers=HEADERS_MANAGER,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["warning_status"], "处理中")
        deadline = datetime.fromisoformat(body["case"]["deadline"])
        expected = before + timedelta(days=5)
        self.assertAlmostEqual((deadline - expected).total_seconds(), 0, delta=5)
        self.assertEqual(body["case"]["case_no"], f"W{self.warning_id:06d}-C01")

    def test_concurrent_claim_only_one_wins(self):
        self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 7},
            headers=HEADERS_MANAGER,
        )
        case_id = self._latest_case_id()

        barrier = threading.Barrier(2)

        def claim(headers):
            barrier.wait()
            return self.client.post(f"/api/v1/disposals/{case_id}/claim", headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(claim, self.college_headers),
                pool.submit(claim, self.college_headers),
            ]
            statuses = sorted(f.result().status_code for f in futures)
        self.assertEqual(statuses, [200, 409], statuses)

        # 重复认领仍然失败。
        again = self.client.post(f"/api/v1/disposals/{case_id}/claim",
                                 headers=self.college_headers)
        self.assertEqual(again.status_code, 409)

    def test_review_return_then_resubmit_and_approve(self):
        case_id = self._dispatch_claim_and_submit(sla_days=7)

        # 复核退回必须给出意见。
        missing = self.client.post(
            f"/api/v1/disposals/{case_id}/return", json={"review_comment": ""},
            headers=HEADERS_REVIEWER,
        )
        self.assertEqual(missing.status_code, 409)

        r = self.client.post(
            f"/api/v1/disposals/{case_id}/return",
            json={"review_comment": "佐证不充分，请补充用人单位回访记录"},
            headers=HEADERS_REVIEWER,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["case"]["state"], "处理中")
        self.assertEqual(r.json()["case"]["review_result"], "returned")
        self.assertEqual(r.json()["warning_status"], "处理中")

        # 退回后经办人待办应出现“复核退回”。
        todo = self.client.get("/api/v1/disposals/todo", headers=self.college_headers).json()
        reasons = {reason for item in todo["data"] if item["id"] == case_id
                   for reason in item["todo_reasons"]}
        self.assertIn("复核退回，需补充整改", reasons)

        # 补充证据后重新提交，复核通过闭环。
        self.client.post(
            f"/api/v1/disposals/{case_id}/evidences",
            json={"file_name": "回访记录.pdf", "summary": "5家用人单位回访确认"},
            headers=self.college_headers,
        )
        again = self.client.post(
            f"/api/v1/disposals/{case_id}/submit-review",
            json={"root_cause": "课程体系滞后", "improvement_action": "重构实训模块",
                  "summary": "已补充回访材料"},
            headers=self.college_headers,
        )
        self.assertEqual(again.status_code, 200, again.text)
        approved = self.client.post(
            f"/api/v1/disposals/{case_id}/approve",
            json={"review_comment": "整改到位，同意闭环"},
            headers=HEADERS_REVIEWER,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["case"]["state"], "已闭环")
        self.assertEqual(approved.json()["warning_status"], "已解决")

        # 动作记录不可变：完整记录了退回前后的全部动作且按序号排列。
        detail = self.client.get(f"/api/v1/disposals/{case_id}", headers=HEADERS_MANAGER).json()
        actions = [log["action"] for log in detail["action_logs"]]
        self.assertEqual(actions, ["分派", "认领", "补充处置信息", "登记证据摘要",
                                   "提交复核", "复核退回", "登记证据摘要",
                                   "提交复核", "复核通过"])
        self.assertEqual([log["seq"] for log in detail["action_logs"]],
                         list(range(1, len(actions) + 1)))

    def test_recurrence_links_to_previous_closed_cycle_and_preserves_history(self):
        case_id = self._dispatch_claim_and_submit(sla_days=7)
        self.client.post(
            f"/api/v1/disposals/{case_id}/approve",
            json={"review_comment": "同意闭环"}, headers=HEADERS_REVIEWER,
        )

        # 已闭环期间检测侧不得把同一风险当作已解决而静默处理：
        # 再次命中 -> 系统复发重开。
        db = SessionLocal()
        warning = db.query(Warning).get(self.warning_id)
        reopened = svc.reopen_resolved_warning(db, warning, "2026届指标再度下滑")
        db.close()
        self.assertTrue(reopened)

        cases = self.client.get(
            f"/api/v1/warnings/{self.warning_id}/disposal", headers=HEADERS_MANAGER
        ).json()
        self.assertEqual([c["cycle_no"] for c in cases], [1, 2])
        second = cases[1]
        self.assertEqual(second["parent_case_id"], case_id)
        self.assertEqual(second["state"], "待分派")
        self.assertEqual(second["recur_reason"], "2026届指标再度下滑")

        # 第一轮闭环与动作记录原样保留。
        first = self.client.get(f"/api/v1/disposals/{case_id}", headers=HEADERS_MANAGER).json()
        self.assertEqual(first["state"], "已闭环")
        self.assertTrue(any(log["action"] == "复核通过" for log in first["action_logs"]))

        # 预警处于已复发，未重新分派前不能算解决；管理处待办有“复发预警待分派”。
        warning_now = self.client.get(f"/api/v1/warnings/{self.warning_id}").json()
        self.assertEqual(warning_now["status"], "已复发")
        mgr_todo = self.client.get("/api/v1/disposals/todo", headers=HEADERS_MANAGER).json()
        self.assertTrue(any(
            item["id"] == second["id"] and "复发预警待分派" in item["todo_reasons"]
            for item in mgr_todo["data"]
        ))

        # 管理处分派第二轮后进入处理中，期限重新起算。
        r = self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 3},
            headers=HEADERS_MANAGER,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["case"]["cycle_no"], 2)
        self.assertEqual(r.json()["warning_status"], "处理中")

    def test_role_isolation_and_state_transition_guards(self):
        # 学院经办人无权分派。
        r = self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 7},
            headers=self.college_headers,
        )
        self.assertEqual(r.status_code, 403)

        self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 7},
            headers=HEADERS_MANAGER,
        )
        case_id = self._latest_case_id()

        # 其他学院看不到也不能操作该处置单。
        other = self.client.get(f"/api/v1/disposals/{case_id}", headers=HEADERS_OP2)
        self.assertEqual(other.status_code, 403)
        other_claim = self.client.post(f"/api/v1/disposals/{case_id}/claim",
                                       headers=HEADERS_OP2)
        self.assertEqual(other_claim.status_code, 403)

        # 被分派人之外的本院账号不能认领。
        wrong_person = dict(self.college_headers)
        wrong_person["X-User-Id"] = "op9"
        forbidden_claim = self.client.post(
            f"/api/v1/disposals/{case_id}/claim", headers=wrong_person
        )
        self.assertEqual(forbidden_claim.status_code, 409)

        # 复核人不能在处理中阶段复核。
        bad_review = self.client.post(
            f"/api/v1/disposals/{case_id}/approve",
            json={"review_comment": "同意"}, headers=HEADERS_REVIEWER,
        )
        self.assertEqual(bad_review.status_code, 409)

        # 没有证据不能提交复核。
        self.client.post(f"/api/v1/disposals/{case_id}/claim",
                         headers=self.college_headers)
        no_evidence = self.client.post(
            f"/api/v1/disposals/{case_id}/submit-review",
            json={"root_cause": "原因", "improvement_action": "措施", "summary": "提交"},
            headers=self.college_headers,
        )
        self.assertEqual(no_evidence.status_code, 409)

        # 经办人与复核人不能是同一人（职责分离）。
        self.client.post(
            f"/api/v1/disposals/{case_id}/evidences",
            json={"file_name": "a.png", "summary": "证据"},
            headers=self.college_headers,
        )
        self.client.post(
            f"/api/v1/disposals/{case_id}/submit-review",
            json={"root_cause": "原因", "improvement_action": "措施", "summary": "提交"},
            headers=self.college_headers,
        )
        same_person = {
            "X-User-Id": "op1", "X-User-Name": "Operator-Li", "X-User-Role": "reviewer",
        }
        self_approve = self.client.post(
            f"/api/v1/disposals/{case_id}/approve",
            json={"review_comment": "自批"}, headers=same_person,
        )
        self.assertEqual(self_approve.status_code, 409)

        # 直接改预警状态被拒绝，防止绕过复核误判已解决。
        direct = self.client.put(
            f"/api/v1/warnings/{self.warning_id}",
            json={"status": "已解决"}, headers=HEADERS_MANAGER,
        )
        self.assertEqual(direct.status_code, 409)

    def test_overdue_todo_survives_restart_without_background_process(self):
        # sla_days=0 -> 截止时间即分派时刻，立刻逾期。
        self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": 0},
            headers=HEADERS_MANAGER,
        )
        case_id = self._latest_case_id()

        todo = self.client.get(
            "/api/v1/disposals/todo?overdue_only=true", headers=self.college_headers
        ).json()
        self.assertTrue(any(item["id"] == case_id for item in todo["data"]))
        self.assertGreaterEqual(todo["overdue_count"], 1)

        # 模拟服务重启：释放全部连接后用新进程式会话查询，
        # 逾期完全由数据库中的 deadline 推导，不依赖内存状态或常驻进程。
        engine.dispose()
        fresh_client = TestClient(app)
        after = fresh_client.get(
            "/api/v1/disposals/todo?overdue_only=true", headers=self.college_headers
        ).json()
        self.assertTrue(any(item["id"] == case_id for item in after["data"]))

        db = SessionLocal()
        case = db.query(DisposalCase).get(case_id)
        self.assertTrue(svc.is_overdue(
            deadline=case.deadline, state=CaseState(case.state.value),
            now=datetime.now() + timedelta(days=1),
        ))
        # 已闭环的处置单不再计逾期。
        self.assertFalse(svc.is_overdue(
            deadline=case.deadline, state=CaseState.CLOSED,
            now=datetime.now() + timedelta(days=1),
        ))
        db.close()

    # ------------------------------------------------------------------
    def _latest_case_id(self) -> int:
        db = SessionLocal()
        case = (
            db.query(DisposalCase)
            .filter(DisposalCase.warning_id == self.warning_id)
            .order_by(DisposalCase.cycle_no.desc())
            .first()
        )
        case_id = case.id
        db.close()
        return case_id

    def _dispatch_claim_and_submit(self, sla_days: int) -> int:
        self.client.post(
            f"/api/v1/warnings/{self.warning_id}/disposal/dispatch",
            json={"college_id": self.college_id, "assignee_id": "op1",
                  "assignee_name": "李经办", "sla_days": sla_days},
            headers=HEADERS_MANAGER,
        )
        case_id = self._latest_case_id()
        self.client.post(f"/api/v1/disposals/{case_id}/claim",
                         headers=self.college_headers)
        self.client.put(
            f"/api/v1/disposals/{case_id}/progress",
            json={"root_cause": "课程更新滞后", "improvement_action": "新增企业实训"},
            headers=self.college_headers,
        )
        self.client.post(
            f"/api/v1/disposals/{case_id}/evidences",
            json={"file_name": "整改方案.docx", "file_key": "oss://key-1",
                  "summary": "学院已制定课程整改方案"},
            headers=self.college_headers,
        )
        r = self.client.post(
            f"/api/v1/disposals/{case_id}/submit-review",
            json={"root_cause": "课程更新滞后", "improvement_action": "新增企业实训",
                  "summary": "已完成整改，请复核"},
            headers=self.college_headers,
        )
        assert r.status_code == 200, r.text
        return case_id


if __name__ == "__main__":
    unittest.main()
