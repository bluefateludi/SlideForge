# eval: 题集版本纳入材料正文并保持重复运行稳定

## Parent
Part of #51（#41 修正）

## What to build
修改题面、验收要求或材料正文时题集版本变化；相同内容在不同路径、不同加载顺序和重复运行中版本一致。

## Acceptance criteria
- [x] 原始材料字节的 SHA-256 纳入稳定题集版本，升级算法前缀。
- [x] 材料文件缺失时明确报错，不计算伪版本。
- [x] 临时文件回归测试覆盖正文变化、顺序稳定、路径无关、重复运行。

## Blocked by
None — can start immediately
