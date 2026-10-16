# 固定页面编辑评测流程

范围：#42 / #57 / #58 / #59 的固定输入、编辑提案、程序评分与独立报告。
[交互图](flow.html) 可下载后本地打开；[源文件](flow.arch.json) 可编辑。
GitHub 文件页展示源码，不是在线交互预览。

代码依据：
- backend/app/eval/edit_cases.py：固定夹具和稳定版本
- backend/app/eval/edit_runner.py：独立页面准备与 ai-edit
- backend/app/llm/tool_execution.py：统一工具轨迹入口
- backend/app/eval/edit_scorer.py：提案结果、目标和保护规则
- backend/app/eval/edit_persistence.py：独立报告
- backend/tests/test_fixed_edit_integration.py：API / PostgreSQL / 脚本模型

主线箭头未标注重复动作：节点名称已表达读取固定输入、执行编辑、评分和展示。
虚线是观测旁路，不改变工具副本的业务行为。没有自动 apply，也没有 checkpoint。

验收：
- diagram_type: workflow
- output: flow.html
- specification_sha256: 807356aa4e5d3276006ed271427e6f32e7ddc06a77d1268285023b9c879b954b
- artifact_sha256: 9871a4c867badbbaa72457f77ae33e65e87c24a85287e6980f4b4df7f88090dc
- validation: 9/9 showcase, 0 errors, 0 warnings
- browser_evidence: passed
- visual_review: passed
- correction_rounds: 2

确定性交付见 delivery.json，完整校验见 validation.json。
浏览器证据见 flow.visual-check.json / flow.visual-check.html；检查四档桌面视口、
明暗主题截图，人工图像检查以最终 HTML 哈希为准。浏览器检查与视觉审查分别记录。
生成和验证没有触发真实模型或付费生图。
