import { useQuery } from '@tanstack/react-query'
import { request } from '@/api/client'
import type { EvalRun, EvalRunDetail } from '@/features/eval/types'

/** 列表默认条数与后端 Query 默认一致；页面上没有翻页，取上限附近即可 */
const LIST_LIMIT = 50

const listKey = ['eval-runs'] as const
const detailKey = (runId: string) => ['eval-runs', runId] as const

export function useEvalRuns() {
  return useQuery({
    queryKey: listKey,
    queryFn: () => request<EvalRun[]>(`/eval/runs?limit=${LIST_LIMIT}`),
  })
}

export function useEvalRun(runId: string) {
  return useQuery({
    queryKey: detailKey(runId),
    queryFn: () => request<EvalRunDetail>(`/eval/runs/${runId}`),
  })
}
