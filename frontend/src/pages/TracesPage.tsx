import { Activity, Filter } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { useTraceMetricsSummary, useTraces } from '@/features/trace/api'
import {
  formatDateTime,
  formatMs,
  formatPercent,
  formatTokens,
  KIND_LABEL,
  TRACE_STATUS_LABEL,
  type TraceMetricsSummary,
  type TracePublic,
} from '@/features/trace/types'
import { errorMessage } from '@/lib/errors'

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '运行中' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const KIND_OPTIONS = [
  { value: '', label: '全部类型' },
  { value: 'outline', label: '大纲' },
  { value: 'deck', label: '整册' },
]

export default function TracesPage() {
  const [status, setStatus] = useState('')
  const [kind, setKind] = useState('')
  const traces = useTraces({ status, kind })
  const metrics = useTraceMetricsSummary(7)

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">执行追踪</h1>
        <p className="mt-1.5 text-sm text-ink-muted">
          每次大纲 / 整册生成的全程执行记录；点击查看单条瀑布时间线
        </p>
      </div>

      <MetricsSection metrics={metrics.data} failed={metrics.isError} />

      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Filter className="size-4 text-ink-muted" />
          <select
            aria-label="按状态筛选"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[13px] text-ink outline-none focus:border-accent"
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select
            aria-label="按类型筛选"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[13px] text-ink outline-none focus:border-accent"
          >
            {KIND_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        {traces.data && (
          <p className="text-xs text-ink-muted">共 {traces.data.total} 条</p>
        )}
      </div>

      {traces.isPending && <TableSkeleton />}

      {traces.isError && (
        <p role="alert" className="rounded-2xl bg-negative/8 px-5 py-4 text-sm text-negative">
          {errorMessage(traces.error, '执行记录加载失败，请稍后重试')}
        </p>
      )}

      {traces.data?.items.length === 0 && <EmptyState />}

      {traces.data && traces.data.items.length > 0 && (
        <div className="overflow-x-auto rounded-2xl border border-line bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium text-ink-muted">
                <th className="px-4 py-3">开始时间</th>
                <th className="px-4 py-3">类型</th>
                <th className="px-4 py-3">状态</th>
                <th className="px-4 py-3">关联 trace</th>
                <th className="px-4 py-3 text-right">时长</th>
                <th className="px-4 py-3">错误码</th>
              </tr>
            </thead>
            <tbody>
              {traces.data.items.map((trace) => (
                <TraceRow key={trace.id} trace={trace} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function TraceRow({ trace }: { trace: TracePublic }) {
  const status = TRACE_STATUS_LABEL[trace.status]

  return (
    <tr className="border-b border-line transition-colors last:border-b-0 hover:bg-surface-soft">
      <td className="px-4 py-3.5 tabular-nums">
        <Link to={`/trace/${trace.id}`} className="block text-ink">
          {formatDateTime(trace.started_at)}
        </Link>
      </td>
      <td className="px-4 py-3.5">
        <Link to={`/trace/${trace.id}`} className="block">
          {KIND_LABEL[trace.kind]}
        </Link>
      </td>
      <td className="px-4 py-3.5">
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${status.className}`}
        >
          {status.label}
        </span>
      </td>
      <td className="px-4 py-3.5">
        {trace.linked_trace_id ? (
          <Link
            to={`/trace/${trace.linked_trace_id}`}
            className="text-accent transition-colors hover:brightness-110"
            title={trace.kind === 'deck' ? '跳到依据的大纲 trace' : '跳到生成的整册 trace'}
          >
            {trace.kind === 'deck' ? '← 大纲' : '→ 整册'}
          </Link>
        ) : (
          <span className="text-ink-muted">—</span>
        )}
      </td>
      <td className="px-4 py-3.5 text-right tabular-nums">{formatMs(trace.duration_ms)}</td>
      <td className="max-w-44 truncate px-4 py-3.5">
        {trace.error_code ? (
          <span className="rounded-md bg-negative/10 px-2 py-0.5 text-[11px] text-negative" title={trace.error_code}>
            {trace.error_code}
          </span>
        ) : (
          <span className="text-ink-muted">—</span>
        )}
      </td>
    </tr>
  )
}

/** 顶部指标卡：近 7 天窗口的 P50/P95、成功率、平均 token 与失败来源 top */
function MetricsSection({ metrics, failed }: { metrics?: TraceMetricsSummary; failed: boolean }) {
  if (failed) return null
  if (!metrics) {
    return (
      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((key) => (
          <div key={key} className="h-24 animate-pulse rounded-2xl bg-surface-soft" />
        ))}
      </div>
    )
  }

  const deckRate = metrics.trace_success.find((item) => item.kind === 'deck')
  const outlineRate = metrics.trace_success.find((item) => item.kind === 'outline')
  const topFailure = metrics.failure_breakdown[0]

  return (
    <section className="mb-8" aria-label="近 7 天指标">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="整册时延 P50 / P95"
          value={`${formatMs(metrics.deck_duration_p50_ms)} / ${formatMs(metrics.deck_duration_p95_ms)}`}
        />
        <MetricCard
          label="trace 成功率"
          value={
            deckRate || outlineRate
              ? `${outlineRate ? `大纲 ${formatPercent(outlineRate.success_rate)}` : ''}${
                  outlineRate && deckRate ? ' · ' : ''
                }${deckRate ? `整册 ${formatPercent(deckRate.success_rate)}` : ''}`
              : '—'
          }
        />
        <MetricCard
          label="页成功率"
          value={formatPercent(metrics.slide_success_rate)}
          unit={`${metrics.slide_succeeded}/${metrics.slide_total} 页`}
        />
        <MetricCard
          label="首过率 / 修复成功率"
          value={`${formatPercent(metrics.slide_first_pass_rate)} / ${formatPercent(
            metrics.slide_repair_success_rate,
          )}`}
          unit={`修复 ${metrics.slide_repair_total} 页`}
        />
        <MetricCard
          label="平均 Token（llm）"
          value={`${formatTokens(metrics.avg_prompt_tokens)} + ${formatTokens(
            metrics.avg_completion_tokens,
          )}`}
          unit="输入 + 输出"
        />
      </div>
      {topFailure && (
        <p className="mt-3 text-xs text-ink-muted">
          近 {metrics.days} 天失败来源 top：
          <span className="font-medium text-negative">{topFailure.error_code}</span>
          （{topFailure.count} 次，占 {formatPercent(topFailure.ratio)}）
        </p>
      )}
    </section>
  )
}

function MetricCard({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="rounded-2xl border border-line bg-surface px-5 py-4">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className="mt-1 text-xl font-bold tabular-nums tracking-tight">
        {value}
        {unit && <span className="ml-1.5 text-[12px] font-normal text-ink-muted">{unit}</span>}
      </p>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="rounded-3xl border border-dashed border-line-strong bg-aurora px-8 py-20 text-center">
      <span className="mx-auto mb-5 grid size-12 place-items-center rounded-2xl bg-surface text-accent shadow-card">
        <Activity className="size-5" />
      </span>
      <h2 className="text-xl font-semibold tracking-tight">还没有执行记录</h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
        新建一份 PPT 后，大纲与整册生成的全程执行故事会自动出现在这里。
      </p>
    </div>
  )
}

function TableSkeleton() {
  return (
    <div className="overflow-hidden rounded-2xl border border-line bg-surface">
      {[0, 1, 2].map((key) => (
        <div key={key} className="border-b border-line px-4 py-4 last:border-b-0">
          <div className="h-4 w-2/3 animate-pulse rounded bg-surface-soft" />
        </div>
      ))}
    </div>
  )
}
