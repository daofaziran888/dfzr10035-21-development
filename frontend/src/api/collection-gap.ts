/** 采集缺口复核接口：检索、选中、标定实采、派发/续派与核对。 */
import { request } from './client'

export type EvidenceSummary = {
  证据编号: string
  来源: string
  来源标识: string
  区间: string
  区间起点: number
  区间终点: number
  结论: string
  证据类型: string
  重复次数: number
  证据编号列表: string[]
  说明: string
}

export type GapGroup = {
  id: number
  status: string
  组编号: string
  管道名称: string
  复核记录编号: string
  区间起点: number
  区间终点: number
  上报区间: string
  真实缺口: string
  真实缺口区间: [number, number][]
  仍缺数量: number
  关联证据: EvidenceSummary[]
  关联证据编号: string[]
  成功证据编号: string[]
  关联证据来源: string[]
  重复上报组数: number
  重复组编号: string[]
  已选中: boolean
  是否重复?: boolean
  重复于?: string
  上报次数: number
  上报来源: string
  最近上报: string
}

export type SearchResult = {
  items: GapGroup[]
  total: number
  duplicates: GapGroup[]
  duplicates_hidden: number
  query: Record<string, string | number | boolean | null>
}

export type ReviewRow = {
  id: number
  记录编号: string
  管道名称: string
  操作人: string
  审计结论: string
  关联异常组: string
  待派发区间: string
  派发结果: string
  重放任务编号: string
  治理报告编号: string
  影响证据: string
  派发次数: number
  最近派发: string
  status: string
}

export type DispatchRow = {
  id: number
  派发编号: string
  状态: string
  选中异常组: string[]
  复核记录编号: string[]
  管道名称: string[]
  区间: string
  检索条件: Record<string, unknown>
  重放任务编号: string[]
  治理报告编号: string[]
  证据编号: string[]
  结果摘要: string
  派发时间: string
}

export type CollectionTask = {
  id: number
  任务编号: string
  失败端点: string
  失败原因: string
  采集区间: string
  关联异常组: string
  关联复核记录: string
  队列状态: string
  status: string
}

export type GovernItem = {
  id: number
  报告编号: string
  评估范围: string
  可执行区间: string
  关联异常组: string
  关联复核记录: string
  关联重放任务: string
  证据编号: string
  status: string
}

export type VerifyPayload = {
  检索结果: SearchResult
  复核清单: ReviewRow[]
  治理记录: GovernItem[]
  采集任务: CollectionTask[]
  派发记录: DispatchRow[]
}

export type ActionResult<T = unknown> = {
  ok: boolean
  message: string
  [key: string]: T | unknown
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

export function searchGapGroups(params: {
  reviewNo?: string
  pipeline?: string
  start?: number | null
  end?: number | null
  keyword?: string
  includeDuplicates?: boolean
  onlyPending?: boolean
}): Promise<SearchResult> {
  const query = new URLSearchParams()
  if (params.reviewNo) query.set('review_no', params.reviewNo)
  if (params.pipeline) query.set('pipeline', params.pipeline)
  if (params.start !== null && params.start !== undefined) query.set('start', String(params.start))
  if (params.end !== null && params.end !== undefined) query.set('end', String(params.end))
  if (params.keyword) query.set('keyword', params.keyword)
  query.set('include_duplicates', params.includeDuplicates ? 'true' : 'false')
  query.set('only_pending', params.onlyPending ? 'true' : 'false')
  return getJson<SearchResult>(`/api/collection-gap/search?${query.toString()}`)
}

export function selectGroups(groupIds: number[], selected = true): Promise<ActionResult> {
  return postJson('/api/collection-gap/select', { group_ids: groupIds, selected })
}

export function markCollected(payload: {
  pipeline: string
  start: number
  end: number
  group_id?: number
  remark?: string
}): Promise<ActionResult> {
  return postJson('/api/collection-gap/mark-collected', payload)
}

export function addEvidence(payload: {
  pipeline: string
  start: number
  end: number
  conclusion: string
  source: string
  source_id?: string
  group_id?: number
  remark?: string
}): Promise<ActionResult> {
  return postJson('/api/collection-gap/evidence', payload)
}

export function adjustGroupRange(groupId: number, start: number, end: number): Promise<ActionResult> {
  return postJson(`/api/collection-gap/${groupId}/range-adjust`, { start, end })
}

export function dispatchGroups(payload: {
  group_ids: number[]
  start?: number | null
  end?: number | null
  interrupt?: boolean
  operator?: string
}): Promise<ActionResult<DispatchRow>> {
  return postJson('/api/collection-gap/dispatch', {
    group_ids: payload.group_ids,
    start: payload.start ?? null,
    end: payload.end ?? null,
    interrupt: payload.interrupt ?? false,
    operator: payload.operator ?? '治理员',
  })
}

export function retryDispatch(dispatchId: number): Promise<ActionResult<DispatchRow>> {
  return postJson(`/api/collection-gap/dispatch/${dispatchId}/retry`, {})
}

export function loadVerify(): Promise<VerifyPayload> {
  return getJson<VerifyPayload>('/api/collection-gap/verify')
}

export function loadReviewList(): Promise<{ items: ReviewRow[]; total: number }> {
  return getJson('/api/collection-gap/review-list')
}
