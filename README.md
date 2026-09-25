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

预警状态不再只有“预警中/已解决”。新增处置单（`DisposalCase`）承接责任分派与复核闭环：

- 状态：`待分派 → 处理中 → 待复核 → 已闭环`；预警主状态同步为 `预警中/处理中/待复核/已解决/已复发`。
- 分派时指定**责任学院、被分派人、处理期限（sla_days 自然日）**；期限 = 分派时刻 + sla_days。
- 学院经办人可**认领**（并发条件更新，只有一人成功）、补充**根因与改进措施**、登记**证据附件摘要**，齐备后提交复核。
- 复核人可**通过**（闭环，预警转为已解决）或**退回**（带复核意见，回到处理中）；经办人与复核人不能为同一人。
- 已闭环后风险再现（检测到新届次恶化）会自动**复发重开**：新建下一轮处置单并通过 `parent_case_id` 关联上一轮，上一轮闭环与动作记录原样保留；同一批数据重复检测不会误判复发。
- 每次动作写入 `disposal_action_logs`（只追加，无修改/删除接口），含动作、前后状态、操作人角色与明细，序号在单内递增。
- 逾期不依赖任何后台常驻进程：`GET /disposals/todo` 在查询时按 `deadline` 实时计算，服务重启后结果一致。

### 身份与权限

由网关注入请求头：`X-User-Id`、`X-User-Name`、`X-User-Role`（manager/operator/reviewer）、
学院经办人另需 `X-College-Id` / `X-College-Name`。学院经办人只能访问本学院处置单；
分派/改派/登记复发仅管理处可用；复核通过/退回仅复核人可用。预警状态禁止通过 `PUT /warnings/{id}` 直接改写。

### 主要接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/warnings/{id}/disposal/dispatch` | manager | 分派责任学院与期限 |
| POST | `/disposals/{id}/claim` | operator | 并发认领 |
| PUT | `/disposals/{id}/progress` | operator/manager | 记录根因、改进措施 |
| POST | `/disposals/{id}/evidences` | operator/manager | 证据附件摘要 |
| POST | `/disposals/{id}/submit-review` | operator/manager | 提交复核（需根因/措施/证据） |
| POST | `/disposals/{id}/approve` `/return` | reviewer | 复核通过/退回（意见必填） |
| POST | `/disposals/{id}/reassign` | manager | 改派（撤回复核、期限重算） |
| POST | `/warnings/{id}/disposal/recur` | manager | 手动登记复发并关联上轮闭环 |
| GET | `/disposals/todo` | 全部 | 查询期待办，支持 `overdue_only` |
| GET | `/disposals` `/disposals/{id}` | 全部 | 处置单列表/详情（含动作记录） |

历史库升级执行 `python scripts/migrate_db.py`（只增建表，不改动既有数据）。
