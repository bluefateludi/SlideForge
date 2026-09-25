import type { components } from '@/api/schema'

type Schemas = components['schemas']

export type Outline = Schemas['OutlinePublic']
export type OutlinePage = Schemas['OutlinePage']
export type OutlineUpdate = Schemas['OutlineUpdate']
export type OutlineGenerateAccepted = Schemas['OutlineGenerateAccepted']

export interface OutlineProgressEvent {
  type: 'snapshot' | 'progress' | 'completed' | 'failed'
  status: Outline['status']
  progress: number
  message: string
  revision?: number | null
  /** 观测链路锚点（obs#7）：旧事件/快照缺省，前端取最新非空值 */
  trace_id?: string | null
}
