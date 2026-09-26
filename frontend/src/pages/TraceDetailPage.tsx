import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { useTrace } from '@/features/trace/api'
import {
  formatDateTime,
  formatMs,
  formatTokens,
  KIND_LABEL,
  SPAN_KIND_LABEL,
  SPAN_STATUS_META,
  TRACE_STATUS_LABEL,
  type SpanPublic,
  type TraceDetail,
} from '@/features/trace/types'
import { errorMessage } from '@/lib/errors'
import { cn, formatCost } from '@/lib/utils'

export default function TraceDetailPage() {
  const { traceId } = useParams()
  const trace = useTrace(traceId ?? '')

  if (trace.isPending) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <div className="h-6 w-48 animate-pulse rounded bg-surface-soft" />
        <div className="mt-8 h-64 animate-pulse rounded-2xl bg-surface-soft" />
      </div>
    )
  }

  if (trace.isError || !trace.data) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <p role="alert" className="rounded-2xl bg-negative/8 px-5 py-4 text-sm text-negative">
          {errorMessage(trace.error, 'trace 详情加载失败，请稍后重试')}
        </p>
      </div>
    )
  }

  const data = trace.data

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <Link
        to="/trace"
        className="inline-flex items-center gap-1.5 text-[13px] text-accent transition-colors hover:brightness-110"
      >
        <ArrowLeft className="size-3.5" />
        返回执行追踪
      </Link>

      <TraceHeader data={data} />

      {data.spans.length === 0 ? (
        <div className="mt-8 rounded-3xl border border-dashed border-line-strong bg-aurora px-8 py-16 text-center">
          <h2 className="text-lg font-semibold tracking-tight">这条 trace 还没有 span</h2>
          <p className="mt-2 text-sm text-ink-muted">任务刚入队或埋点未落库，稍后再来看看。</p>
        </div>
      ) : (
        <Waterfall data={data} />
      )}
    </div>
  )
}

/** 头部：kind/status/时长/起止 + trace 级错误 + linked trace 互跳 */
function TraceHeader({ data }: { data: TraceDetail }) {
  const { trace } = data
  const status = TRACE_STATUS_LABEL[trace.status]

  return (
    <div className="mt-3 rounded-2xl border border-line bg-surface px-5 py-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="text-xl font-semibold tracking-tight">
          {KIND_LABEL[trace.kind]} trace
        </h1>
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${status.className}`}
        >
          {status.label}
        </span>
        <span className="text-sm text-ink-muted">时长 {formatMs(trace.duration_ms)}</span>
        {data.cost?.configured && (
          <span
            className="text-sm tabular-nums text-ink-muted"
            title={`LLM ¥${data.cost.llm_cost}（↑${data.cost.prompt_tokens} ↓${data.cost.completion_tokens}）＋ 生图 ¥${data.cost.image_cost}（${data.cost.ai_image_count} 张）`}
          >
            成本 {formatCost(data.cost.total_cost)}
          </span>
        )}
        <span className="text-sm tabular-nums text-ink-muted">
          {formatDateTime(trace.started_at)} →{' '}
          {trace.finished_at ? formatDateTime(trace.finished_at) : '…'}
        </span>
        {trace.linked_trace_id && (
          <Link
            to={`/trace/${trace.linked_trace_id}`}
            className="text-[13px] text-accent transition-colors hover:brightness-110"
          >
            {trace.kind === 'deck' ? '← 查看大纲 trace' : '→ 查看整册 trace'}
          </Link>
        )}
      </div>
      {trace.error_code && (
        <p className="mt-2 text-sm">
          <span className="rounded-md bg-negative/10 px-2 py-0.5 font-medium text-negative">
            {trace.error_code}
          </span>
          {trace.error_message && (
            <span className="ml-2 text-ink-muted">{trace.error_message}</span>
          )}
        </p>
      )}
    </div>
  )
}

interface WaterfallRow {
  span: SpanPublic
  depth: number
  offsetMs: number
}

/** 按 parent_span_id 建树并展平；时间零点取 trace.started_at */
function buildRows(data: TraceDetail): { rows: WaterfallRow[]; totalMs: number } {
  const zero = new Date(data.trace.started_at).getTime()
  const byParent = new Map<number | null, SpanPublic[]>()
  for (const span of data.spans) {
    const key = span.parent_span_id
    const list = byParent.get(key) ?? []
    list.push(span)
    byParent.set(key, list)
  }

  const rows: WaterfallRow[] = []
  // 兜底：父 span 不在列表（理论上不发生）时挂到根，保证每行都渲染
  const orphans = data.spans.filter(
    (span) => span.parent_span_id !== null && !data.spans.some((s) => s.id === span.parent_span_id),
  )
  const walk = (parentId: number | null, depth: number) => {
    for (const span of byParent.get(parentId) ?? []) {
      const offsetMs = Math.max(0, new Date(span.started_at).getTime() - zero)
      rows.push({ span, depth, offsetMs })
      walk(span.id, depth + 1)
    }
  }
  walk(null, 0)
  for (const span of orphans) {
    rows.push({ span, depth: 0, offsetMs: Math.max(0, new Date(span.started_at).getTime() - zero) })
  }

  const lastEnd = data.spans.reduce((max, span) => {
    const end = new Date(span.started_at).getTime() + (span.duration_ms ?? 0)
    return Math.max(max, end)
  }, zero)
  return { rows, totalMs: Math.max(lastEnd - zero, 1) }
}

function Waterfall({ data }: { data: TraceDetail }) {
  const { rows, totalMs } = buildRows(data)

  return (
    <div className="mt-6 rounded-2xl border border-line bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 px-5 pt-4">
        <p className="text-[13px] font-semibold">执行时间线（{data.spans.length} 个 span）</p>
        <div className="flex items-center gap-3 text-[11px] text-ink-muted">
          {Object.values(SPAN_STATUS_META).map((meta) => (
            <span key={meta.label} className="inline-flex items-center gap-1">
              <span>{meta.icon}</span>
              {meta.label}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-3 flex flex-col gap-1 px-5 pb-5">
        {rows.map(({ span, depth, offsetMs }) => (
          <SpanRow key={span.id} span={span} depth={depth} offsetMs={offsetMs} totalMs={totalMs} />
        ))}
      </div>
    </div>
  )
}

function SpanRow({
  span,
  depth,
  offsetMs,
  totalMs,
}: {
  span: SpanPublic
  depth: number
  offsetMs: number
  totalMs: number
}) {
  const status = SPAN_STATUS_META[span.status]
  const left = Math.min((offsetMs / totalMs) * 100, 100)
  const width = Math.max(((span.duration_ms ?? 0) / totalMs) * 100, span.duration_ms === null ? 0 : 0.5)
  const repairRound = span.attributes?.repair_round

  return (
    <div className="group grid grid-cols-[minmax(13rem,auto)_1fr] items-center gap-4 rounded-lg px-1 py-1 transition-colors hover:bg-surface-soft">
      {/* 左列：缩进 + 名称 + 元信息 */}
      <div className="flex min-w-0 items-center gap-1.5" style={{ paddingLeft: `${depth * 14}px` }}>
        <span className="text-[11px]">{status.icon}</span>
        <span className="truncate text-[13px] font-medium" title={span.name}>
          {span.name}
        </span>
        <span className="flex-none rounded-md bg-surface-soft px-1.5 py-0.5 text-[10px] text-ink-muted">
          {SPAN_KIND_LABEL[span.span_kind]}
        </span>
        {typeof repairRound === 'number' && (
          <span className="flex-none rounded-md bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium text-warning">
            返工 R{repairRound}
          </span>
        )}
        {span.span_kind === 'llm' && (
          <span className="flex-none truncate text-[10px] text-ink-muted">
            {span.model ?? '—'} · ↑{formatTokens(span.prompt_tokens)} ↓
            {formatTokens(span.completion_tokens)}
          </span>
        )}
        {span.status === 'failed' && (
          <span
            className="flex-none rounded-md bg-negative/10 px-1.5 py-0.5 text-[10px] font-medium text-negative"
            title={span.error_message ?? span.error_code ?? undefined}
          >
            {span.error_code ?? 'unknown'}
          </span>
        )}
      </div>
      {/* 右列：时间条 */}
      <div className="relative h-5 min-w-0">
        <div
          className={cn('absolute top-1/2 h-3 -translate-y-1/2 rounded-full', status.bar)}
          style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
          title={`${span.name} · ${formatMs(span.duration_ms)}${
            span.status === 'failed' && span.error_message ? ` · ${span.error_message}` : ''
          }`}
        />
      </div>
    </div>
  )
}
