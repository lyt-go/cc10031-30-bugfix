import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type DeliveryReconciliation = {
  export_id: string
  status?: string
  request: { points: string[]; start_iso: string | null; end_iso: string | null }
  summary: {
    source_windows: number
    delivered_windows: number
    excluded_windows: number
    delivered_observations: number
    delivered_points: string[]
    excluded_by_reason: Record<string, number>
    reconciled: boolean
  }
  deliveries: Record<string, unknown>[]
  exclusions: Record<string, unknown>[]
  counts?: Record<string, number>
}

export type WindowDetail = {
  rollup_id: string
  point: string
  window_type: string
  split_basis: string
  window_count: number
  source_files: string[]
  windows: Record<string, unknown>[]
}

async function readJson<T>(path: string, failure: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(failure)
  return (await response.json()) as T
}

export function loadWindowDetail(id: number): Promise<WindowDetail> {
  return readJson<WindowDetail>(`/api/rollup_window/${id}/windows`, '窗口实际起止读取失败')
}

export function loadDeliveryDetail(id: number): Promise<DeliveryReconciliation> {
  return readJson<DeliveryReconciliation>(`/api/series_export/${id}/delivery`, '交付对账读取失败')
}

export function loadDeliveryExclusions(id: number): Promise<DeliveryReconciliation> {
  return readJson<DeliveryReconciliation>(`/api/series_export/${id}/exclusions`, '排除明细读取失败')
}
