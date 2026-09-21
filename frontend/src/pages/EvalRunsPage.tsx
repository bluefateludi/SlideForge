import { FlaskConical } from 'lucide-react'
import { Link } from 'react-router'
import { useEvalRuns } from '@/features/eval/api'
import { formatDuration, formatPercent, formatScore, RUN_STATUS_LABEL, type EvalRun } from '@/features/eval/types'
import { errorMessage } from '@/lib/errors'

/** 运行时间列用固定格式：评测记录是审计性质的列表，相对时间反而看不出批次间隔 */
function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (value: number) => `${value}`.padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export default function EvalRunsPage() {
  const runs = useEvalRuns()

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">评测记录</h1>
        <p className="mt-1.5 text-sm text-ink-muted">
          在命令行运行 <code className="rounded bg-surface-soft px-1.5 py-0.5 text-xs">make eval</code> 后，一条记录自动出现在这里；点击查看单次详情
        </p>
      </div>

      {runs.isPending && <TableSkeleton />}

      {runs.isError && (
        <p role="alert" className="rounded-2xl bg-negative/8 px-5 py-4 text-sm text-negative">
          {errorMessage(runs.error, '评测记录加载失败，请稍后重试')}
        </p>
      )}

      {runs.data?.length === 0 && <EmptyState />}

      {runs.data && runs.data.length > 0 && (
        <div className="overflow-x-auto rounded-2xl border border-line bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium text-ink-muted">
                <th className="px-4 py-3">运行时间</th>
                <th className="px-4 py-3">题集</th>
                <th className="px-4 py-3 text-right">用例数</th>
                <th className="px-4 py-3 text-right">成功率</th>
                <th className="px-4 py-3 text-right">需求覆盖</th>
                <th className="px-4 py-3 text-right">平均耗时</th>
                <th className="px-4 py-3 text-right">Token</th>
                <th className="px-4 py-3">状态</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function RunRow({ run }: { run: EvalRun }) {
  const status = RUN_STATUS_LABEL[run.status]

  return (
    <tr className="cursor-pointer border-b border-line transition-colors last:border-b-0 hover:bg-surface-soft">
      <td className="px-4 py-3.5 tabular-nums">
        <Link to={`/eval/${run.id}`} className="block text-ink">
          {formatDateTime(run.created_at)}
        </Link>
      </td>
      <td className="max-w-52 truncate px-4 py-3.5 text-ink-muted">
        <Link to={`/eval/${run.id}`} className="block truncate">
          {run.note || run.cases_version}
        </Link>
      </td>
      <td className="px-4 py-3.5 text-right tabular-nums">{run.total_cases}</td>
      <td className="px-4 py-3.5 text-right tabular-nums">{formatPercent(run.success_rate)}</td>
      <td className="px-4 py-3.5 text-right tabular-nums">
        {run.avg_judge_coverage === null ? '—' : `${formatScore(run.avg_judge_coverage)} / 10`}
      </td>
      <td className="px-4 py-3.5 text-right tabular-nums">
        {formatDuration(run.avg_elapsed_seconds)}
      </td>
      <td className="px-4 py-3.5 text-right tabular-nums">
        {Math.round(run.avg_prompt_tokens + run.avg_completion_tokens).toLocaleString()}
      </td>
      <td className="px-4 py-3.5">
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${status.className}`}
        >
          {status.label}
        </span>
      </td>
    </tr>
  )
}

function EmptyState() {
  return (
    <div className="rounded-3xl border border-dashed border-line-strong bg-aurora px-8 py-20 text-center">
      <span className="mx-auto mb-5 grid size-12 place-items-center rounded-2xl bg-surface text-accent shadow-card">
        <FlaskConical className="size-5" />
      </span>
      <h2 className="text-xl font-semibold tracking-tight">还没有评测记录</h2>
      <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-muted">
        评测由命令行发起：在仓库根目录运行{' '}
        <code className="rounded bg-surface-soft px-1.5 py-0.5 text-xs">make eval</code>{' '}
        跑完固定题集后，报告会自动入库并出现在这里。
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
