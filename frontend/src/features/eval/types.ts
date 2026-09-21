import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type EvalRun = Schemas['EvalRunPublic']
export type EvalRunDetail = Schemas['EvalRunDetail']
export type EvalCaseRow = Schemas['EvalCaseRow']
export type EvalCategoryScore = Schemas['EvalCategoryScore']

/** 题集固定四类；顺序即页面展示顺序 */
export const CATEGORY_ORDER = ['technology', 'business', 'education', 'documents'] as const

export type EvalCategory = (typeof CATEGORY_ORDER)[number]

export const CATEGORY_LABEL: Record<EvalCategory, string> = {
  technology: '技术类',
  business: '商业类',
  education: '教育类',
  documents: '文档类',
}

export const RUN_STATUS_LABEL: Record<EvalRun['status'], { label: string; className: string }> = {
  completed: { label: '完成', className: 'bg-positive/10 text-positive' },
  failed: { label: '失败', className: 'bg-negative/10 text-negative' },
}

/** 0-1 小数 → 百分比文案；空值展示占位符 */
export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return '—'
  return `${Math.round(rate * 100)}%`
}

/** 秒数 → "1 分 23 秒" / "32 秒"；空值与 0 秒展示占位符 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 1) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest > 0 ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
}

/** Token 数 → 千位缩写（12,400 → 12.4k） */
export function formatTokens(tokens: number): string {
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`
  return `${Math.round(tokens)}`
}

/** 覆盖分是 0-10 的一位小数 */
export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return '—'
  return score.toFixed(1)
}
