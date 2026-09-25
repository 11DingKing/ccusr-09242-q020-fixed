# 微专业就业成效追踪系统

维护毕业生、学院、微专业、就业去向、企业跟进和预警记录，支持按届次与组织维度追踪就业成效并保留分析依据。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件。配置通过环境变量提供，导入演示数据前请确认数据库位置可写。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn main:app --host 127.0.0.1 --port 8000`，根路径与 `/health` 返回服务状态，接口文档位于 `/docs`。

## 预警处置闭环

预警在检测产生后进入“分派 → 认领/处置 → 提交复核 → 复核通过/退回”的闭环，状态取值见
`app/services/workflow_rules.py`（`new/claimed/waiting_material/processing/waiting_review/closed/cancelled`）。

- 分派由就业管理处（`X-User-Role: manager`）执行，指定责任范围与处理期限；期限按预警级别
  默认红 7 / 橙 10 / 黄 15 个自然日，也可显式指定天数或到期日。
- 学院处理人（`operator`）认领、补料、记录根因与改进措施、提交复核；复核人（`reviewer`）
  只能通过或退回，且处理人与复核人不能相同。认领采用条件更新，并发下只有一方成功，
  租约过期后他人可接手。
- 只有复核通过才会把预警置为“已解决”并归档一个闭环周期（`disposition_cycles`）；
  重新打开开启新一轮，历史闭环保留；复发预警通过 `warning_recurrences` 关联历史闭环，
  检测再次触发同风险时会自动建立关联。
- 每次状态动作向 `disposition_action_logs` 追加不可变记录（ORM 事件 + SQLite 触发器双重保护）。
- 逾期、待复核、租约过期均由 `GET /api/v1/todos` 基于库中期限实时计算，
  不依赖后台常驻进程，服务重启后结果一致。

身份通过请求头传递：`X-User-Id`、`X-User-Name`、`X-User-Role`（operator/reviewer/manager）、
`X-College-Id`（学院角色必填，用于责任范围隔离）。

