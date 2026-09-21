import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type TracePublic = Schemas['TracePublic']
export type TracePage = Schemas['TracePage']
export type TraceDetail = Schemas['TraceDetail']
export type SpanPublic = Schemas['SpanPublic']
export type TraceMetricsSummary = Schemas['TraceMetricsSummary']
export type KindSuccessRate = Schemas['KindSuccessRate']
export type FailureBreakdownItem = Schemas['FailureBreakdownItem']
export type NodeStatItem = Schemas['NodeStatItem']

/** 列表页一次拉 100 条（后端上限），页面上没有翻页控件 */
export const TRACE_LIST_LIMIT = 100

export const KIND_LABEL: Record<TracePublic['kind'], string> = {
  outline: '大纲',
  deck: '整册',
}

export const TRACE_STATUS_LABEL: Record<
  TracePublic['status'],
  { label: string; className: string }
> = {
  running: { label: '运行中', className: 'bg-accent/10 text-accent' },
  succeeded: { label: '成功', className: 'bg-positive/10 text-positive' },
  failed: { label: '失败', className: 'bg-negative/10 text-negative' },
  cancelled: { label: '已取消', className: 'bg-surface-soft text-ink-muted' },
}

/**
 * span 行状态色（瀑布图例）：
 * ✅ succeeded / ❌ failed / ⚪ running。
 * 语义注意（obs#2）：页失败时外层 slide[N] task span 可能 succeeded 收口，
 * 失败明细在节点级 span——必须以每个 span 自身 status 渲染，不从父 span 推断。
 */
export const SPAN_STATUS_META = {
  succeeded: { icon: '✅', label: '成功', bar: 'bg-positive/70' },
  failed: { icon: '❌', label: '失败', bar: 'bg-negative/70' },
  running: { icon: '⚪', label: '运行中', bar: 'bg-ink-muted/30' },
} as const

export const SPAN_KIND_LABEL: Record<SpanPublic['span_kind'], string> = {
  task: '任务',
  node: '节点',
  llm: 'LLM',
  export: '导出',
}

/** 毫秒 → "1 分 23 秒" / "32 秒" / "850 ms"；空值与 0 展示占位符 */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest > 0 ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
}

/** 0-1 小数 → 百分比文案；空值展示占位符 */
export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return '—'
  return `${Math.round(rate * 100)}%`
}

/** Token 数 → 千位缩写（12,400 → 12.4k） */
export function formatTokens(tokens: number | null | undefined): string {
  if (tokens === null || tokens === undefined) return '—'
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`
  return `${Math.round(tokens)}`
}

/** ISO 时间 → 本地 "MM-DD HH:mm"（trace 列表是审计性质，用固定格式） */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (value: number) => `${value}`.padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
