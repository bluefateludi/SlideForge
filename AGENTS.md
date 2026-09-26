# SlideForge

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`bluefateludi/SlideForge`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root, created lazily by `/domain-modeling`. See `docs/agents/domain.md`.

## 成本约束

- 生图（百炼 z-image-turbo）按张计费且额度有限：非必要不触发付费生图
- 验证布局/校验/导出类改动走离线路径（读 slides 重建 deck → run_export_check），不整 deck 重生成
- 评测优先只跑受影响题集；确需生图的运行，先报备预计张数与费用
- 任何改到图片链路的代码不得破坏「已有 URL 的图块不重新拉图」这一不变量
