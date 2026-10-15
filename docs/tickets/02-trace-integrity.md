# eval: Trace 缺失与部分缺失不得误判为零成本

## Parent
Part of #51（#41 修正）

## What to build
普通报告保留可用降级显示，逐题记录缺失原因与完整性；正式门禁和立基线遇缺失/不完整/非有限数据必须失败，不能把缺失计为真实零。

## Acceptance criteria
- [x] 无 Trace、查询失败、缺一段或 LLM span 缺 Token 可区分并拒绝正式比较。
- [x] 正常返回的真实零值有效；数值为 NaN/Infinity 或负用量拒绝。
- [x] 数据完整性与原因进入逐题 JSONB，历史缺字段报告不能成为正式基线。
- [x] 离线测试验证丢数据时不能误放行。

## Blocked by
None — can start immediately
