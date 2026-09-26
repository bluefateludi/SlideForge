import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { useEvalRun } from '@/features/eval/api'
import {
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  formatDuration,
  formatPercent,
  formatScore,
  formatTokens,
  type EvalCaseRow,
  type EvalRunDetail,
} from '@/features/eval/types'
import { errorMessage } from '@/lib/errors'
import { cn, formatCost } from '@/lib/utils'

/** 综合分：直接用 avg_judge_score（0-10）归一化到百分制展示，权重仅作说明性文案 */
function overallScore(run: EvalRunDetail): number | null {
  if (run.avg_judge_score === null) return null
  return Math.round(run.avg_judge_score * 10)
}

export default function EvalRunDetailPage() {
  const { runId } = useParams()
  const run = useEvalRun(runId ?? '')

  if (run.isPending) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <div className="h-6 w-48 animate-pulse rounded bg-surface-soft" />
        <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((key) => (
            <div key={key} className="h-24 animate-pulse rounded-2xl bg-surface-soft" />
          ))}
        </div>
      </div>
    )
  }

  if (run.isError || !run.data) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <p role="alert" className="rounded-2xl bg-negative/8 px-5 py-4 text-sm text-negative">
          {errorMessage(run.error, '评测详情加载失败，请稍后重试')}
        </p>
      </div>
    )
  }

  const data = run.data
  const score = overallScore(data)
  const avgTokens = Math.round(data.avg_prompt_tokens + data.avg_completion_tokens)

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <Link
        to="/eval"
        className="inline-flex items-center gap-1.5 text-[13px] text-accent transition-colors hover:brightness-110"
      >
        <ArrowLeft className="size-3.5" />
        返回评测记录
      </Link>

      <div className="mt-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          {data.note || '评测运行'}
          <span className="ml-2 text-base font-normal text-ink-muted">{data.cases_version}</span>
        </h1>
        <p className="mt-1.5 text-sm text-ink-muted">
          {data.total_cases} 个用例 · {data.ok_cases} 个成功 · 平均耗时{' '}
          {formatDuration(data.avg_elapsed_seconds)}
        </p>
      </div>

      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard label="生成成功率" value={formatPercent(data.success_rate)} />
        <MetricCard label="Schema 合法率" value={formatPercent(data.schema_valid_rate)} />
        <MetricCard
          label="需求覆盖分"
          value={formatScore(data.avg_judge_coverage)}
          unit="/ 10"
        />
        <MetricCard label="幻觉率（仅文档题）" value={formatPercent(data.hallucination_rate)} />
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard label="平均耗时" value={formatDuration(data.avg_elapsed_seconds)} unit="/题" />
        <MetricCard label="平均 Token" value={avgTokens.toLocaleString()} unit="/题" />
        <MetricCard
          label="平均成本"
          value={formatCost(data.avg_cost)}
          unit="/题"
        />
        <MetricCard label="失败率" value={formatPercent(data.failure_rate)} />
        <MetricCard label="重试率" value={formatPercent(data.retry_slide_rate)} />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <CategoryBars run={data} />
        <ScoreRing score={score} />
      </div>

      <CaseTable run={data} />
    </div>
  )
}

function MetricCard({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="rounded-2xl border border-line bg-surface px-5 py-4">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums tracking-tight">
        {value}
        {unit && <span className="ml-1 text-[13px] font-normal text-ink-muted">{unit}</span>}
      </p>
    </div>
  )
}

/** 分类得分：纯 CSS 条形，宽度即均分占满分的比例 */
function CategoryBars({ run }: { run: EvalRunDetail }) {
  return (
    <div className="rounded-2xl border border-line bg-surface px-5 py-4">
      <p className="text-[13px] font-semibold">分类得分（均分）</p>
      <div className="mt-3 flex flex-col gap-3">
        {CATEGORY_ORDER.map((category) => {
          const item = run.category_scores[category]
          const score = item?.avg_judge_score
          return (
            <div key={category} className="flex items-center gap-3">
              <span className="w-14 flex-none text-right text-[13px] text-ink-muted">
                {CATEGORY_LABEL[category]}
              </span>
              <div className="h-3.5 flex-1 overflow-hidden rounded-full bg-surface-soft">
                <div
                  className="h-full rounded-full bg-accent"
                  style={{ width: score === null ? 0 : `${Math.min(score * 10, 100)}%` }}
                />
              </div>
              <span className="w-8 flex-none text-right text-[13px] tabular-nums">
                {formatScore(score)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 综合分圆环：SVG circle stroke-dasharray，周长按 r=46 计算 */
function ScoreRing({ score }: { score: number | null }) {
  const radius = 46
  const circumference = 2 * Math.PI * radius
  const ratio = score === null ? 0 : Math.min(score / 100, 1)
  const dash = circumference * ratio

  return (
    <div className="flex items-center gap-6 rounded-2xl border border-line bg-surface px-5 py-4">
      <div className="relative size-28 flex-none">
        <svg viewBox="0 0 110 110" className="size-full -rotate-90">
          <circle cx="55" cy="55" r={radius} fill="none" stroke="#eef0f3" strokeWidth="11" />
          <circle
            cx="55"
            cy="55"
            r={radius}
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="11"
            strokeLinecap="round"
            strokeDasharray={`${dash} ${circumference}`}
          />
        </svg>
        <div className="absolute inset-0 grid place-items-center text-center">
          <div>
            <p className="text-xl font-bold tabular-nums">{score === null ? '—' : score}</p>
            <p className="text-[11px] text-ink-muted">综合分</p>
          </div>
        </div>
      </div>
      <p className="text-[13px] leading-relaxed text-ink-muted">
        综合分 = 评审均分归一化到百分制
        <br />
        口径参考：结构 40% + 内容 40% + 工程表现 20%（权重仅为展示说明）
      </p>
    </div>
  )
}

const ROW_STATUS = {
  ok: { label: '通过', className: 'bg-positive/10 text-positive' },
  failed: { label: '失败', className: 'bg-negative/10 text-negative' },
} as const

function CaseTable({ run }: { run: EvalRunDetail }) {
  return (
    <div className="mt-6 overflow-x-auto rounded-2xl border border-line bg-surface">
      <div className="flex items-center justify-between px-4 pt-4">
        <p className="text-[13px] font-semibold">用例明细</p>
        <div className="flex gap-1.5 text-[11px] text-ink-muted">
          <span className="rounded-md bg-surface-soft px-2 py-0.5">{run.total_cases} 题</span>
          <span className="rounded-md bg-surface-soft px-2 py-0.5">{run.ok_cases} 成功</span>
          <span className="rounded-md bg-surface-soft px-2 py-0.5">
            {run.total_cases - run.ok_cases} 失败
          </span>
        </div>
      </div>
      <table className="mt-2 w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs font-medium text-ink-muted">
            <th className="px-4 py-3">#</th>
            <th className="px-4 py-3">题目 ID</th>
            <th className="px-4 py-3">类别</th>
            <th className="px-4 py-3 text-right">页数达成</th>
            <th className="px-4 py-3 text-right">覆盖分</th>
            <th className="px-4 py-3 text-right">幻觉率</th>
            <th className="px-4 py-3 text-right">Token</th>
            <th className="px-4 py-3 text-right">成本</th>
            <th className="px-4 py-3 text-right">分段耗时</th>
            <th className="px-4 py-3 text-right">耗时</th>
            <th className="px-4 py-3">状态</th>
          </tr>
        </thead>
        <tbody>
          {run.rows.map((row, index) => (
            <CaseTableRow key={row.case_id} row={row} index={index} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CaseTableRow({ row, index }: { row: EvalCaseRow; index: number }) {
  const status = row.ok ? ROW_STATUS.ok : ROW_STATUS.failed
  const pages = row.expected_pages > 0 ? `${row.ready_pages}/${row.expected_pages}` : '—'
  const tokens = row.prompt_tokens + row.completion_tokens
  const tokenText =
    row.tokens_source === 'trace' ? formatTokens(tokens) : '—'
  const stages = [
    row.outline_duration_ms > 0 ? `大纲 ${formatDuration(row.outline_duration_ms / 1000)}` : null,
    row.slide_durations_ms.length > 0
      ? `${row.slide_durations_ms.length} 页 × ${formatDuration(
          row.slide_durations_ms.reduce((sum, ms) => sum + ms, 0) /
            1000 /
            row.slide_durations_ms.length,
        )}`
      : null,
    row.export_duration_ms > 0 ? `导出 ${formatDuration(row.export_duration_ms / 1000)}` : null,
  ].filter(Boolean)
  const stageText = stages.length > 0 ? stages.join(' · ') : '—'
  const stageTitle =
    row.tokens_source === 'trace'
      ? undefined
      : 'token 查不到（无 trace 或观测数据缺失），按 0 展示'

  return (
    <tr className="border-b border-line last:border-b-0 hover:bg-surface-soft">
      <td className="px-4 py-3 tabular-nums text-ink-muted">{`${index + 1}`.padStart(2, '0')}</td>
      <td className="max-w-64 truncate px-4 py-3" title={row.error ?? row.case_id}>
        {row.case_id}
      </td>
      <td className="px-4 py-3">
        <span className="rounded-md bg-surface-soft px-2 py-0.5 text-[11px] text-ink-muted">
          {CATEGORY_LABEL[row.category]}
        </span>
      </td>
      <td className={cn('px-4 py-3 text-right tabular-nums', !row.pages_met && 'text-warning')}>
        {pages}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">{formatScore(row.judge_coverage)}</td>
      <td className="px-4 py-3 text-right tabular-nums">
        {formatPercent(row.hallucination_rate)}
      </td>
      <td className="px-4 py-3 text-right tabular-nums" title={stageTitle}>
        {tokenText}
      </td>
      <td
        className="px-4 py-3 text-right tabular-nums"
        title={
          row.ai_image_count > 0
            ? `含 ${row.ai_image_count} 张 AI 生图；单价未配置或 trace 不可用时成本记 0`
            : '单价未配置或 trace 不可用时成本记 0'
        }
      >
        {formatCost(row.cost)}
      </td>
      <td className="px-4 py-3 text-right tabular-nums whitespace-nowrap" title={stageTitle}>
        {stageText}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">{formatDuration(row.elapsed_seconds)}</td>
      <td className="px-4 py-3">
        <span
          title={row.error ?? undefined}
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${status.className}`}
        >
          {status.label}
        </span>
      </td>
    </tr>
  )
}
