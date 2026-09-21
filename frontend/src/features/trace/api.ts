import { useQuery } from '@tanstack/react-query'
import { request } from '@/api/client'
import { TRACE_LIST_LIMIT, type TraceDetail, type TracePage, type TraceMetricsSummary } from '@/features/trace/types'

/** 列表筛选条件：与后端 Query 一一对应；空串表示不过滤 */
export interface TraceListFilters {
  status: string
  kind: string
}

const listKey = (filters: TraceListFilters) => ['traces', filters] as const
const detailKey = (traceId: string) => ['traces', traceId] as const
const metricsKey = (days: number) => ['trace-metrics', days] as const

export function useTraces(filters: TraceListFilters) {
  const params = new URLSearchParams({ limit: `${TRACE_LIST_LIMIT}` })
  if (filters.status) params.set('status', filters.status)
  if (filters.kind) params.set('kind', filters.kind)
  return useQuery({
    queryKey: listKey(filters),
    queryFn: () => request<TracePage>(`/trace?${params.toString()}`),
  })
}

export function useTrace(traceId: string) {
  return useQuery({
    queryKey: detailKey(traceId),
    queryFn: () => request<TraceDetail>(`/trace/${traceId}`),
    enabled: traceId !== '',
  })
}

export function useTraceMetricsSummary(days = 7) {
  return useQuery({
    queryKey: metricsKey(days),
    queryFn: () => request<TraceMetricsSummary>(`/trace/metrics/summary?days=${days}`),
  })
}
