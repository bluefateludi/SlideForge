# trace: 统一工具记录入口并按校验结果统计修复收敛

Part of #42

## What to build
集中记录工具调用 ID/轮次/顺序/目标与结果，修复收敛使用真实校验结果，缺失数据不可误报成功。

## Acceptance criteria
- [x] 成功、拒绝、未知工具、异常均有统一 Span，保留父子关系，不记录参数正文。
- [x] 首过率不把失败页计成功，修复页去重，历史缺少证据时显示不可判定。
- [x] Trace API 与评测报告统计一致，保留 #56 的数据完整性规则。
- [x] 假模型、数据库测试及前端检查通过，Archify 图验证交付。

## Blocked by
None — can start immediately
