# eval: 固定初始页面，移除编辑评测的生成前置调用

Part of #42

## What to build
以固定 blocks/layout_tree/locked 夹具准备编辑题，只让待测的 ai-edit 调用真实模型。提供假模型验证方式，本次不跑真实评测。

## Acceptance criteria
- [x] 夹具与布局引用校验，哈希覆盖夹具及期望并稳定排序。
- [x] 执行器不调用大纲、正文或图片生成；数据库准备检查项目所有权。
- [x] 真实 HTTP/数据库接口与假模型的离线集成通过。

## Blocked by
None — can start immediately

## 流程补充
- [x] 固定大纲随样本版本化，直接写入并与页面关联；不生成大纲。
- [x] AI 调用前完成样本结构、主题和 API 大纲/页面一致性校验。
