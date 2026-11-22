<template>
  <section class="page" data-testid="collection-gap-page">
    <header class="page-head">
      <div>
        <h2>采集异常组复核</h2>
        <p class="page-desc">
          按管道复核记录与采集证据关联检索待核对异常组：重复上报折叠为同一事实，已实采区间从缺口里扣减，
          派发结果回写复核清单，中断后按原检索条件续派。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="gap-verify" type="button" @click="verifyAll">核对全链路</button>
        <button class="btn ghost" data-testid="gap-reset" type="button" @click="resetAndReload">重置条件</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">待核对异常组</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已选中</span>
        <strong class="stat-value">{{ selectedIds.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">折叠的重复上报</span>
        <strong class="stat-value">{{ duplicatesHidden }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">补采任务</span>
        <strong class="stat-value">{{ tasks.length }}</strong>
      </article>
    </div>

    <form class="analysis-form" data-testid="gap-search-form" @submit.prevent="runSearch">
      <label>
        复核记录编号
        <input v-model="form.reviewNo" data-testid="gap-filter-review" placeholder="如 PIPE-1001" />
      </label>
      <label>
        管道名称
        <input v-model="form.pipeline" data-testid="gap-filter-pipeline" placeholder="按管道名称检索" />
      </label>
      <label>
        异常区间起
        <input v-model.number="form.start" data-testid="gap-filter-start" type="number" min="0" />
      </label>
      <label>
        异常区间止
        <input v-model.number="form.end" data-testid="gap-filter-end" type="number" min="0" />
      </label>
      <label class="inline-check">
        <input v-model="form.includeDuplicates" data-testid="gap-filter-duplicates" type="checkbox" />
        显示重复上报
      </label>
      <button class="btn primary" data-testid="gap-search" type="submit">检索异常组</button>
    </form>

    <p class="status-legend" data-testid="gap-search-summary">
      检索条件：{{ searchSummary }}　共 {{ groups.length }} 个待核对异常组，折叠 {{ duplicatesHidden }} 条重复上报。
      <span v-if="duplicates.length">重复上报仍可核对：{{ duplicates.map((item) => item.组编号).join('、') }}</span>
    </p>

    <table class="data-table" data-testid="gap-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>组编号</th>
          <th>管道名称</th>
          <th>复核记录</th>
          <th>上报区间</th>
          <th>真实缺口</th>
          <th>关联证据（来源）</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="group in groups"
          :key="group.组编号"
          :data-testid="`gap-row-${group.组编号}`"
          :data-status="group.status"
          :class="{ 'row-selected': selectedIds.includes(group.id), 'row-dup': group.是否重复 }"
        >
          <td>
            <input
              type="checkbox"
              :data-testid="`gap-select-${group.组编号}`"
              :checked="selectedIds.includes(group.id)"
              :disabled="group.是否重复"
              @change="toggleSelect(group)"
            />
          </td>
          <td>{{ group.组编号 }}<span v-if="group.重复上报组数" class="tag">折叠{{ group.重复上报组数 }}条</span></td>
          <td>{{ group.管道名称 }}</td>
          <td>{{ group.复核记录编号 || '—' }}</td>
          <td>{{ group.上报区间 || '—' }}</td>
          <td data-testid="gap-missing">{{ group.真实缺口 }}</td>
          <td class="evidence-cell">
            <span v-for="item in group.关联证据" :key="item.证据编号" class="evidence-chip" :class="`evidence-${item.结论}`">
              {{ item.证据编号 }}·{{ item.来源 }}·{{ item.区间 }}·{{ item.结论 }}
              <template v-if="item.重复次数 > 1"> ×{{ item.重复次数 }}</template>
            </span>
            <span v-if="!group.关联证据.length" class="muted">无</span>
          </td>
          <td>{{ group.status }}</td>
        </tr>
        <tr v-if="!groups.length">
          <td colspan="8" class="empty-state">没有符合检索条件的待核对采集异常组</td>
        </tr>
      </tbody>
    </table>

    <section class="panel-grid">
      <article class="panel">
        <h3>提交前标定实采</h3>
        <p class="panel-desc">把一段区间标成已经实采，缺口会自动重新判定，同一管道同一区间只计一次。</p>
        <form class="analysis-form" data-testid="gap-mark-form" @submit.prevent="submitMark">
          <label>
            管道名称
            <input v-model="markForm.pipeline" data-testid="gap-mark-pipeline" required />
          </label>
          <label>
            起点
            <input v-model.number="markForm.start" data-testid="gap-mark-start" type="number" required />
          </label>
          <label>
            终点
            <input v-model.number="markForm.end" data-testid="gap-mark-end" type="number" required />
          </label>
          <button class="btn" data-testid="gap-mark-submit" type="submit">标为已实采</button>
        </form>
        <p class="hint">{{ markMessage }}</p>
      </article>

      <article class="panel">
        <h3>范围调整重新判定</h3>
        <p class="panel-desc">范围调整后按采集证据重新判定仍缺的数据；未选中的组沿用首个选中组。</p>
        <form class="analysis-form" data-testid="gap-adjust-form" @submit.prevent="submitAdjust">
          <label>
            起点
            <input v-model.number="adjustForm.start" data-testid="gap-adjust-start" type="number" required />
          </label>
          <label>
            终点
            <input v-model.number="adjustForm.end" data-testid="gap-adjust-end" type="number" required />
          </label>
          <button class="btn" data-testid="gap-adjust-submit" type="submit">调整并重判</button>
        </form>
        <p class="hint">{{ adjustMessage }}</p>
      </article>

      <article class="panel">
        <h3>派发补采</h3>
        <p class="panel-desc">按选中的异常组派发。可模拟派发中断，再按原检索条件重试，不会重复生成补采。</p>
        <div class="analysis-form">
          <button class="btn primary" data-testid="gap-dispatch" type="button" :disabled="!selectedIds.length" @click="submitDispatch(false)">
            派发选中异常组
          </button>
          <button class="btn" data-testid="gap-dispatch-interrupt" type="button" :disabled="!selectedIds.length" @click="submitDispatch(true)">
            模拟派发中断
          </button>
        </div>
        <p class="hint" data-testid="gap-dispatch-message">{{ dispatchMessage }}</p>
        <p v-if="interruptedDispatch" class="hint">
          中断记录 {{ interruptedDispatch.派发编号 }} 保留了检索条件：{{ criteriaText(interruptedDispatch.检索条件) }}
          <button class="link" data-testid="gap-retry-dispatch" type="button" @click="retryInterrupted">
            按原检索条件重试
          </button>
        </p>
      </article>
    </section>

    <section class="panel" data-testid="gap-review-panel">
      <h3>复核清单回写</h3>
      <p class="panel-desc">派发结果回写到复核清单，便于核对实际证据影响了哪次重放。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th>记录编号</th>
            <th>管道名称</th>
            <th>关联异常组</th>
            <th>待派发区间</th>
            <th>派发结果</th>
            <th>重放任务</th>
            <th>治理报告</th>
            <th>影响证据</th>
            <th>派发次数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in reviewRows" :key="row.记录编号" :data-testid="`review-row-${row.记录编号}`">
            <td>{{ row.记录编号 }}</td>
            <td>{{ row.管道名称 }}</td>
            <td>{{ row.关联异常组 || '—' }}</td>
            <td data-testid="review-range">{{ row.待派发区间 || '—' }}</td>
            <td>{{ row.派发结果 || '未派发' }}</td>
            <td>{{ row.重放任务编号 || '—' }}</td>
            <td>{{ row.治理报告编号 || '—' }}</td>
            <td>{{ row.影响证据 || '—' }}</td>
            <td>{{ row.派发次数 }}</td>
          </tr>
          <tr v-if="!reviewRows.length">
            <td colspan="9" class="empty-state">复核清单暂无关联异常组的记录</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="verifyPayload" class="panel" data-testid="gap-verify-panel">
      <h3>核对：检索结果 / 治理可执行区间 / 重放采集任务</h3>
      <div class="verify-grid">
        <div>
          <h4>检索结果</h4>
          <ul>
            <li v-for="item in verifyPayload.检索结果.items" :key="item.组编号">
              {{ item.组编号 }}（{{ item.管道名称 }}）：上报 {{ item.上报区间 }} → 仍缺 {{ item.真实缺口 }}
            </li>
          </ul>
        </div>
        <div>
          <h4>治理记录可执行区间</h4>
          <ul>
            <li v-for="item in verifyPayload.治理记录" :key="item.报告编号">
              {{ item.报告编号 }}：{{ item.可执行区间 }}（{{ item.关联异常组 }}）
            </li>
          </ul>
        </div>
        <div>
          <h4>重放队列采集任务</h4>
          <ul>
            <li v-for="item in verifyPayload.采集任务" :key="item.任务编号">
              {{ item.任务编号 }}：{{ item.失败端点 }} {{ item.采集区间 }}
            </li>
          </ul>
        </div>
      </div>
    </section>

    <footer class="page-foot">
      <span>采集异常组复核工作台</span>
      <span v-if="errorMessage" class="error-text" data-testid="gap-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  adjustGroupRange,
  dispatchGroups,
  loadReviewList,
  loadVerify,
  markCollected,
  retryDispatch,
  searchGapGroups,
  selectGroups,
} from '@/api/collection-gap'
import type { DispatchRow, GapGroup, ReviewRow, VerifyPayload } from '@/api/collection-gap'

const form = ref({
  reviewNo: '',
  pipeline: '',
  start: null as number | null,
  end: null as number | null,
  includeDuplicates: false,
})
const groups = ref<GapGroup[]>([])
const duplicates = ref<GapGroup[]>([])
const duplicatesHidden = ref(0)
const selectedIds = ref<number[]>([])
const reviewRows = ref<ReviewRow[]>([])
const tasks = ref<VerifyPayload['采集任务']>([])
const verifyPayload = ref<VerifyPayload | null>(null)
const interruptedDispatch = ref<DispatchRow | null>(null)
const errorMessage = ref('')
const markMessage = ref('')
const adjustMessage = ref('')
const dispatchMessage = ref('')
const markForm = ref({ pipeline: '', start: null as number | null, end: null as number | null })
const adjustForm = ref({ start: null as number | null, end: null as number | null })

const pendingCount = computed(() => groups.value.filter((item) => item.status === '待核对').length)
const searchSummary = computed(() => {
  const parts = []
  if (form.value.reviewNo) parts.push(`复核记录 ${form.value.reviewNo}`)
  if (form.value.pipeline) parts.push(`管道 ${form.value.pipeline}`)
  if (form.value.start !== null && form.value.end !== null) parts.push(`区间 ${form.value.start}-${form.value.end}`)
  return parts.length ? parts.join('，') : '全部待核对异常组'
})

function reportError(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

function criteriaText(criteria: Record<string, unknown>) {
  const range = criteria['区间']
  const pipelines = (criteria['管道名称'] as string[] | undefined)?.join('、') ?? ''
  const reviews = (criteria['复核记录编号'] as string[] | undefined)?.join('、') ?? ''
  const ids = (criteria['选中异常组'] as string[] | undefined)?.join('、') ?? ''
  return `异常组 ${ids}；复核记录 ${reviews}；管道 ${pipelines}；区间 ${String(range)}`
}

async function runSearch() {
  errorMessage.value = ''
  try {
    const payload = await searchGapGroups({
      reviewNo: form.value.reviewNo,
      pipeline: form.value.pipeline,
      start: form.value.start,
      end: form.value.end,
      includeDuplicates: form.value.includeDuplicates,
    })
    groups.value = payload.items
    duplicates.value = payload.duplicates
    duplicatesHidden.value = payload.duplicates_hidden
    selectedIds.value = payload.items.filter((item) => item.已选中).map((item) => item.id)
    if (payload.items.length && !markForm.value.pipeline) {
      markForm.value.pipeline = payload.items[0].管道名称
      adjustForm.value.start = payload.items[0].真实缺口区间[0]?.[0] ?? payload.items[0].区间起点
      adjustForm.value.end = payload.items[0].真实缺口区间[0]?.[1] ?? payload.items[0].区间终点
    }
  } catch (error) {
    reportError(error, '异常组检索失败')
  }
}

async function toggleSelect(group: GapGroup) {
  const next = selectedIds.value.includes(group.id)
    ? selectedIds.value.filter((id) => id !== group.id)
    : [...selectedIds.value, group.id]
  selectedIds.value = next
  errorMessage.value = ''
  try {
    await selectGroups([group.id], next.includes(group.id))
    if (next.includes(group.id) && !markForm.value.pipeline) {
      markForm.value.pipeline = group.管道名称
      adjustForm.value.start = group.真实缺口区间[0]?.[0] ?? group.区间起点
      adjustForm.value.end = group.真实缺口区间[0]?.[1] ?? group.区间终点
    }
  } catch (error) {
    reportError(error, '选中状态保存失败')
  }
}

async function submitMark() {
  markMessage.value = ''
  errorMessage.value = ''
  try {
    const result = await markCollected({
      pipeline: markForm.value.pipeline,
      start: Number(markForm.value.start),
      end: Number(markForm.value.end),
      group_id: selectedIds.value[0],
    })
    markMessage.value = String(result.message)
    await runSearch()
  } catch (error) {
    reportError(error, '标定实采失败')
  }
}

async function submitAdjust() {
  adjustMessage.value = ''
  errorMessage.value = ''
  const target = selectedIds.value[0] ?? groups.value[0]?.id
  if (!target) {
    adjustMessage.value = '请先检索并选中一个异常组'
    return
  }
  try {
    const result = await adjustGroupRange(target, Number(adjustForm.value.start), Number(adjustForm.value.end))
    adjustMessage.value = String(result.message)
    await runSearch()
  } catch (error) {
    reportError(error, '范围调整失败')
  }
}

async function submitDispatch(interrupt: boolean) {
  dispatchMessage.value = ''
  errorMessage.value = ''
  try {
    const result = await dispatchGroups({
      group_ids: selectedIds.value,
      start: form.value.start,
      end: form.value.end,
      interrupt,
    })
    dispatchMessage.value = String(result.message)
    const dispatch = result.dispatch as DispatchRow | undefined
    interruptedDispatch.value = interrupt && dispatch ? dispatch : null
    await refreshSidePanels()
    await runSearch()
  } catch (error) {
    reportError(error, '补采派发失败')
  }
}

async function retryInterrupted() {
  if (!interruptedDispatch.value) return
  errorMessage.value = ''
  try {
    const result = await retryDispatch(interruptedDispatch.value.id)
    dispatchMessage.value = `按原检索条件重试：${String(result.message)}`
    interruptedDispatch.value = null
    await refreshSidePanels()
    await runSearch()
  } catch (error) {
    reportError(error, '派发重试失败')
  }
}

async function refreshSidePanels() {
  const [review, verify] = await Promise.all([loadReviewList(), loadVerify()])
  reviewRows.value = review.items
  tasks.value = verify.采集任务
  verifyPayload.value = verify
}

async function verifyAll() {
  errorMessage.value = ''
  try {
    await refreshSidePanels()
  } catch (error) {
    reportError(error, '全链路核对失败')
  }
}

async function resetAndReload() {
  form.value = { reviewNo: '', pipeline: '', start: null, end: null, includeDuplicates: false }
  markMessage.value = ''
  adjustMessage.value = ''
  dispatchMessage.value = ''
  interruptedDispatch.value = null
  await runSearch()
}

onMounted(async () => {
  await runSearch()
  try {
    await refreshSidePanels()
  } catch (error) {
    reportError(error, '复核清单读取失败')
  }
})
</script>

<style scoped>
.panel-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin: 12px 0; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.panel h3 { margin: 0 0 6px; font-size: 14px; }
.panel h4 { margin: 0 0 6px; font-size: 13px; }
.panel-desc { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.hint { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
.inline-check { flex-direction: row !important; align-items: center; gap: 6px !important; }
.row-selected td { background: #eff6ff; }
.row-dup td { background: #f8fafc; color: var(--muted); }
.tag { margin-left: 6px; padding: 1px 6px; border-radius: 10px; background: #e0e7ff; color: #3730a3; font-size: 11px; }
.evidence-cell { max-width: 360px; }
.evidence-chip { display: inline-block; margin: 2px 4px 2px 0; padding: 1px 6px; border-radius: 4px; font-size: 11px; background: #eef2ff; }
.evidence-成功 { background: #dcfce7; color: #166534; }
.evidence-失败 { background: #fee2e2; color: #991b1b; }
.muted { color: var(--muted); }
.verify-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.verify-grid ul { margin: 0; padding-left: 18px; font-size: 12px; }
</style>
