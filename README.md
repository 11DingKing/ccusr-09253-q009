# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 学期结算关账窗口

学期结束后仍会有导师补确认和请假修正，关账不是一次性冻结某个瞬间，而是依次经过五个可控阶段：

1. **数据截止（data_cutoff）**——数据责任人固化事件边界（`data_cutoff_event_id`），生成异常清单；
2. **异常清单（anomaly_review）**——合规审计人消化未确认的导师打卡等异常；
3. **补录宽限（grace_period）**——项目协调人主持，接纳期限内的补录并在结束时更新最终事件边界；
4. **复核签署（review_signoff）**——教务负责人（dean）签署，剩余异常须显式 `force` 带例外签署并留痕；
5. **正式冻结（frozen）**——生成不可变快照（freeze_id 为 `{window_id}-r{revision}`）。

每个阶段都有责任角色、具体责任人与 UTC 期限；期限可用培养方案时区的本地日历日指定（自动处理 DST）。窗口可暂停/恢复，暂停期间计时停止并顺延期限。

**重开与版本**：已冻结窗口只能凭 `academic_senate`（不同于签署角色 dean、且不是原签署人）批准重开，生成 revision+1 的新版本；旧版本标记 `superseded`，旧冻结材料永久保留、不可覆盖。

**后台超时任务**：任务状态持久化在 `settlement_tasks` 表，worker 线程只是执行器——进程重启后新 worker 拾取到期任务（含持锁进程崩溃后租约过期的任务）继续执行；失败按指数退避重试，超上限进入死信。

### API

| 操作 | 方法与路径 |
| --- | --- |
| 启动 | `POST /api/settlements` |
| 推进当前阶段 | `POST /api/settlements/{window_id}/advance` |
| 确认异常清零 | `POST /api/settlements/{window_id}/anomalies/resolve` |
| 暂停 / 恢复 | `POST /api/settlements/{window_id}/pause` · `/resume` |
| 重开（新版本） | `POST /api/settlements/{window_id}/reopen` |
| 状态查询 | `GET /api/settlements/{window_id}`（`?revision=N` 查旧版本）· `GET /api/settlements` |
| 后台任务查询 | `GET /api/settlements/{window_id}/tasks` |
| 手动驱动超时 | `POST /api/settlements/run-timeouts` |

并发推进由窗口行的乐观锁裁决（只有一个请求成功）；冻结材料与窗口状态在同一事务内原子落库。常驻 worker 由应用 lifespan 启动，可用 `SETTLEMENT_WORKER_ENABLED=0` 关闭（测试即如此）。

## 测试（关账窗口部分）

```bash
python3 -m pytest tests/test_settlement_api.py tests/test_settlement_core.py -q
```

覆盖并发推进 / 并发冻结不覆盖、并发任务认领互斥、跨时区（上海、纽约 DST 回拨）本地日历日期限换算与到期触发、暂停顺延、版本化重开旧材料不变，以及后台任务的崩溃租约回收、指数退避重试、死信和重启后继续。
