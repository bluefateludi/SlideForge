# 评测题集、报告与可信基线

规格：[#51](https://github.com/bluefateludi/SlideForge/issues/51)。
实现 tickets：[#52](https://github.com/bluefateludi/SlideForge/issues/52)、
[#53](https://github.com/bluefateludi/SlideForge/issues/53)、
[#54](https://github.com/bluefateludi/SlideForge/issues/54)、
[#55](https://github.com/bluefateludi/SlideForge/issues/55)。

## 三种身份

- **题集版本**：输入、要求、页数、类别、材料路径及材料原始字节 SHA-256 的稳定指纹，
  按题号排序，前缀为 cases-v2-。同一内容重复运行不会换版本；材料正文变化会换版本。
  原始字节变化（含换行）也视为材料版本变化。Git 提交记录保存具体修改历史。
- **评测记录**：一次执行的结果，带独立 run ID。重复执行新增记录，不自动修改基线。
- **基线**：维护者明确选择的、来源和样本完整的报告。只有显式建立基线才写入文件。

## 完整性先于数值比较

普通报告保留历史的降级显示：查不到 Trace 可显示零，但逐题同时记录
measurement_complete、measurement_issues、cost_configured、tokens_source。
正式门禁与立基线拒绝这种降级数据。

两段 Trace 均须有完整 LLM 用量；缺一段、某次调用缺输入/输出 Token、
查询失败、非法用量、缺失有效成本单价均标为不完整。真实零 Token 只有在确实
返回完整零值且单价明确配置时才有效。不能把「查不到」当成免费。

结构、页数、内容评审和正耗时样本也必须完整。所有成功率、成本及耗时必须是
有限数值，不能用 NaN、Infinity、缺失指标或缺失样本绕过门禁。文档题须记录数字核对样本。

## 基线格式 v2

只有 format_version=2、status=ready 的完整文件可用：

| 内容 | 保存的信息 |
| --- | --- |
| 身份 | cases_version、format_version、created_at |
| 来源 | source_run_id、代码提交、Trace 中实际出现的生成模型、评审模型 |
| 运行条件 | 当次 harness API 地址、生成相关配置快照、Token/图片单价 |
| 样本 | 总题数、结构/页数/评审/Trace 有效题数、文档题数、数字数 |
| 指标 | 成功率、Schema 合法率、导出成功题数、页数达成率、内容评分、需求覆盖率、输入/输出 Token 均值、平均/总成本、P95 耗时 |
| 诊断 | 文档数字幻觉率、重试率、失败页数 |
| 规则 | direction 与数值 tolerance，比较时实际读取 |

harness 配置不能证明远端 API/Worker 的全部配置；字段明确标为 harness，
实际生成模型从 Trace 的 model 字段取得。不保存密钥或 Token。
单价变化时拒绝直接比较，避免把计价变化误判为生成算法退步。
当前算法不做语义数字/单位换算，因此幻觉率首版只记录，不作硬门禁。

默认规则：成功率、Schema、导出、页数、需求覆盖不下降；评分允许下降 0.5；
Token、成本、P95 允许上涨 30%。文件内数值规则参与实际判定。
有效零基线不跳过：Token 基线为 0 而新值大于 0 时仍拦截。

## 命令与顺序

- make eval：普通评测，记录结果，不自动建立基线。
- make eval-gate：先检查基线格式和题集版本，再运行真实评测，比较并返回 0/1。
  无基线、旧格式、待重建或版本不匹配时，在生成之前结束。
- make eval-baseline：只从数据库最近一次同版本报告读取数据；
  当次报告的来源和完整性须合格，不拿当前配置填补历史缺失。
- run_eval.py --baseline：实际评测后显式建立基线。该命令会调用模型。
- --cases 保留按题筛选；子集有独立版本，不能直接与全量基线比较。
- --baseline-file 指定独立的基线文件，适合不同题集/子集。

--gate、--baseline、--baseline-from-db 互斥，EVAL_GATE 环境变量也参与检查。
写入前校验完整报告，通过临时文件原子替换，失败保留原文件。
评测期间材料变化时拒绝保存、比较或立基线。

## 本次交付的状态

用户明确要求不提供 Token、不运行真实评测。本次只做离线测试和代码合并。
仓库历史 baseline.json 的原数值保留在 historical_reference，
状态为 needs_rebuild，不能用于正式门禁。没有编造任何新的测量值。

未来维护者有额度并明确决定建立新基线时，先运行评测、检查完整报告，再显式
建立和提交基线。不要在看到退步后直接重设标准。

