# eval: 基线与门禁模式互斥并在生成前预检

## Parent
Part of #51（#41 修正）

## What to build
CLI 阻止建立基线与门禁同时启用（含环境变量），并在调用模型之前检查基线有效性与题集版本；保留主线 --cases 功能。

## Acceptance criteria
- [x] --gate/--baseline/--baseline-from-db 互斥，EVAL_GATE 同样参与冲突检查。
- [x] 基线缺失、旧格式、待重建、版本不匹配在网络/模型调用前退出。
- [x] 无效立基线不覆盖已有文件；普通评测不自动更新基线。
- [x] CLI 离线回归和文档通过；提交并合并，不运行真实评测。

## Blocked by
#54
