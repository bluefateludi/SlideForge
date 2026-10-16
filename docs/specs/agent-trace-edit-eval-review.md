# 双轴评审记录

固定基点：main 711fd74b4f6ebe019b44eb66543a0063d6f29d5f。
范围包含 PR #50 原型、main 合并和本次工作区改造。

## Standards

0 项发现。检查了明确仓库规范和 Fowler smell baseline：
工具 Trace 的内容保护、共享收敛统计、固定夹具与离线集成测试均有对应实现。
未发现硬性规范违反或有实质影响的代码异味。

## Spec

初审 2 项，已修复并定向复核：
1. [P1] 新增 callout 题把整个块 JSON 搜索当正文验证，关键词位于 icon 时会误过。
   改为 expected_after.$new.text 精确匹配，并增加原始错误输入的回归反例。
2. [P2] 一题已生成页面但完全缺少 Trace 时，收敛汇总会忽略该题而显示 100%。
   改为核对 API 已知页面数和任务证据数；证据不足时两项收敛率为 None。

复核两项修复闭合，无新增发现。
最终未解决发现：Standards 0；Spec 0。

## 验证

- 全量离线 pytest：628 passed / 3 skipped（缺少字体）。
- 评审修复后相关回归：33 passed，包含新增的 2 个反例。
- Ruff check：通过；前端类型检查和 production build：通过。
- 前端 lint：退出 0，6 条已有组件警告；production build 保留已有大包提示。
- 临时本地 PostgreSQL 迁移和实际 API/数据库/脚本模型测试通过。
- Archify：9/9 showcase，浏览器检查与最终截图视觉审查通过。

没有运行 make eval-edit、真实模型评测、judge 或付费生图；不创建质量基线。


## 用户流程补充的定向复核

固定点：上次 PR head f80b6c979a2db05a14e8cca05f36f7da858c8ed3。

Standards：0 项发现。固定大纲与页面在同一事务保存且正确关联，
评分继续使用副本，完整输入/操作/判定页与编辑请求耗时具备读取路径。

Spec：0 项发现。固定大纲进入稳定哈希，结构/主题与 API 一致性校验先于 AI，
逐题结果贯通 JSONB、公共 API 与详情页。不新增大纲/正文生成或运行时恢复。
保存的提案指实际用于评分的 operations，不包含原始 HTTP 返回的 discarded/warnings 全文。

本轮最终验证：全量离线 pytest 634 passed / 3 skipped（字体）；Ruff、前端类型/build 通过，lint 无新增警告。实际 API、固定大纲关联、模型/工具耗时与逐题报告读回均由脚本模型集成测试覆盖；未运行真实评测。
