# 固定页面编辑评测与工具轨迹

关联 #42、#57、#58、#59；PR #50。

每道题固定保存 `initial_slide`（blocks、layout_tree、locked）、编辑指令和明确预期。
完整题目参与 `edit-v2-<hash>`；修改任一内容、布局、锁定状态或预期会改变版本，
加载顺序不会改变版本。图片使用 placeholder。

执行器注册独立测试用户，在同一个 PostgreSQL 数据库写入独立项目/固定页，
通过 GET deck 核对页面一致，然后只请求 ai-edit。不会请求大纲生成、正文生成或生图。
工具仅修改 EditSession 的内存副本；评测验证返回的操作提案，不调用 apply。
这些测试项目与报告会保留在数据库中，建议使用独立评测数据库。

程序评分在初始页副本上模拟提案，检查操作数量、类型、目标、明确结果、锁定和无关块不变，
并运行页面结构校验。评分不调用 judge。编辑报告单独展示断言与 Trace；
未采集 Token 或未配置价格以 null 表示，真实零值保持为零，不进入生成基线。

内部编辑工具统一经 execute_edit_tool 记录 round、call_index、tool_call_id、目标 ID、
状态、错误分类和耗时，继承现有父 span。参数正文不进入工具 Span，异常先去除参数内容。
Trace/Span 由现有 recorder 持久化到本地部署的 PostgreSQL，前端按父子关系和时间显示瀑布树。
此处记录的是轨迹，尚不具备 checkpoint、完整页面快照或断点恢复。

生成侧 slide.check 记录明确的 quality_passed。首过与修复成功都依据终态任务的最终校验，
修复页按父任务去重，失败页面保留在分母；历史缺少校验证据时通过率为未知。
Trace API 和评测报告使用同一个聚合函数。

## 验证与真实评测的区别

- pytest 单元测试验证题集、版本、评分与聚合；集成测试使用真实 API/数据库及脚本模型，
  验证工具循环、持久化与报告读取。这些测试不能证明真实模型的编辑质量。
- 真实编辑评测需要启动 API、迁移同库 PostgreSQL，并为 API 配置模型；无需 Worker。
  以后有额度时才运行 `make eval-edit`，或在 backend 运行
  `uv run python scripts/run_edit_eval.py --cases replace/retitle-page`。
  编辑 API 内部可能有多轮工具调用及修正，所以一题不保证仅请求一次模型。
- EVAL_API_BASE 指向 API；CLI 与 API 的 DATABASE_URL 必须相同。
  EVAL_SKIP_DB=1 可只输出控制台报告；断言失败退出 1，环境错误退出 2。
- 本次没有运行真实模型评测，没有调用付费生图，也没有建立真实编辑质量基线。
