# ADR-0001: stuck generating 状态的惰性对账

- 状态：已接受
- 日期：2026-09-25
- 关联：#33

## 背景

ARQ worker 在生成途中死亡（OOM、容器重启、本机休眠后进程挂掉）后，`slides` 行与
`project_outlines.status` 会永久停留在 `generating`。此时：

- 单页重试与整份再生成分别被 `_ensure_idle` / retry 的 409 守卫挡死（守卫的语义
  本身是对的：不能打断活任务）；
- 取消只写 Redis 标记，依赖活任务在页边界轮询——任务已死，标记永远无人消费；
- ARQ 作业被 worker 领走后进程死亡，队列不保证重新投递。

结果是一个只能手改数据库解开的死锁。`services/deck.py` 的取消注释早已自证：
「强杀会留下 generating 状态的孤儿行，反而更难恢复」。

## 决策

**惰性对账（lazy reconciliation）**：不新增后台组件，在读/写入口顺带判定并复位。

1. `slides` 增加 `started_at`、`error_code` 列，`project_outlines` 增加 `started_at`
   列；进入 generating 时由写入方记录时间戳。
2. 在 GET deck、GET outline、retry、regenerate、cancel 五个入口先执行对账：
   `generating 且 now - started_at > 10 分钟` → 置 `failed`、
   `error_code=worker_dead`、错误文案「生成中断，请重试」，并归位
   `project.status`。对账后原有 409 守卫逻辑不变——真正的活任务仍受保护。
3. cancel 在对账后若已无 generating 页，直接返回（不再设无效标记）。
4. 顺带落页级错误分类（`worker_dead` / `llm_not_configured` / `llm_output_invalid`
   / `llm_timeout` / `internal_error`），机器可读，替代仅一句「请重试」的黑话。
   错误码常量独立定义在 `services/deck.py`，刻意不复用 observability 侧
   `codes.py`，避免与观测路线的演进互相耦合。

阈值 10 分钟的依据：单页合法最长 ≈ 5 分钟（LLM 60s 超时 × 含修复最多 2 次 +
配图管线 60s），10 分钟有 2 倍裕量且远小于 job_timeout 15 分钟；DB 时间戳在
「休眠后进程挂」场景天然正确——醒来时墙上时钟早已超额。

## 放弃的备选

- **定时 reaper / 启动扫库**：单机部署下引入常驻组件与调度复杂度，惰性判定
  已覆盖全部用户可见路径；上集群时可作为增量再评估。
- **心跳（页任务周期性 Touch）**：worker 已死时无人心跳，等价于要求更重的
  基础设施；且会与在途的 observability 埋点耦合。
- **查 ARQ 作业存活**：把 API 绑死到队列内部语义，且 arq 对「被领走后 worker
  死亡」的作业查询结果本身不可靠。
- **判死后自动重新入队**：违背「重试是用户决策」的产品语义，且可能重复图片
  生成的副作用与费用。
- **退回 pending 而非 failed**：会造成「刷新页面就自动开跑」的意外行为；
  failed 复用前端既有失败态 UI 与重试按钮，零前端改动。

## 后果

- GET 读路径开始带副作用（对账写库）：换取用户打开页面即见真终态。SSE 快照
  不主动推送对账结果，前端下一次 GET/轮询自然拿到新状态，v1 接受。
- 历史上已卡住的行（升级前产生）`started_at` 为 NULL：视为不可判定、不自动
  复位，避免升级瞬间误杀仍在跑的任务；存量脏数据一次性手工清理。
- 误杀窗口 = 阈值减单页最长合法耗时（≈5 分钟），回归测试锁定「进行中任务
  不被判死」。
