import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** 人民币成本 → "¥0.3" / "¥4"；尾零裁剪，0 或空值给占位符 */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value === 0) return '—'
  const fixed = value.toFixed(4).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')
  return `¥${fixed}`
}
