<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in createFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="supportsDetail" class="detail-panel" data-testid="detail-panel">
      <header class="detail-head">
        <h3>{{ meta.key === 'rollup_window' ? '窗口实际起止与观测出处' : '交付对账与排除明细' }}</h3>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="detailRowId = null">
          收起
        </button>
      </header>
      <p v-if="detailLoading" class="detail-hint">正在读取…</p>
      <p v-else-if="detailError" class="error-text">{{ detailError }}</p>
      <template v-else-if="windowDetail">
        <p class="detail-hint">
          测点 {{ windowDetail.point }} · {{ windowDetail.window_count }} 个窗口 ·
          口径 {{ windowDetail.split_basis }} · 出处 {{ windowDetail.source_files.join('、') || '—' }}
        </p>
        <table class="data-table">
          <thead>
            <tr><th>窗口</th><th>实际起止</th><th>观测数</th><th>归并值</th><th>拆分依据</th><th>出处</th></tr>
          </thead>
          <tbody>
            <tr v-for="(window, index) in windowDetail.windows" :key="String(window.window_ref ?? index)">
              <td>{{ window.window_ref }}</td>
              <td>{{ window.actual_start ?? '—' }} ~ {{ window.actual_end ?? '—' }}</td>
              <td>{{ window.record_count }}</td>
              <td>{{ window.rollup_value }}</td>
              <td>{{ window.split_basis }}</td>
              <td>{{ window.source_file }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <template v-else-if="deliveryDetail">
        <p class="detail-hint">
          申请 {{ deliveryDetail.request.points.join('、') }}
          · {{ deliveryDetail.request.start_iso ?? '不限' }} ~ {{ deliveryDetail.request.end_iso ?? '不限' }}
          · 源窗口 {{ deliveryDetail.summary.source_windows }}
          · 交付 {{ deliveryDetail.summary.delivered_windows }}
          · 拒收 {{ deliveryDetail.summary.excluded_windows }}
          · 对平 {{ deliveryDetail.summary.reconciled ? '是' : '否' }}
        </p>
        <p class="status-legend">
          <span v-for="(count, reason) in deliveryDetail.summary.excluded_by_reason"
                :key="reason" class="legend-item">{{ reason }}：{{ count }}</span>
        </p>
        <table class="data-table">
          <thead>
            <tr><th>窗口</th><th>测点</th><th>实际起止</th><th>交付/排除</th><th>值或原因</th><th>观测数</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in detailRows" :key="`${row.window_ref}-${index}`">
              <td>{{ row.window_ref }}</td>
              <td>{{ row.point }}</td>
              <td>{{ row.window_actual_start ?? '—' }} ~ {{ row.window_actual_end ?? '—' }}</td>
              <td>{{ row.reason ? '排除' : '交付' }}</td>
              <td>{{ row.reason ?? row.value }}<span v-if="row.partial">（跨界拆分）</span></td>
              <td>{{ row.delivered_count ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadDeliveryDetail,
  loadWindowDetail,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { DeliveryReconciliation, WindowDetail } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const supportsDetail = meta.key === 'rollup_window' || meta.key === 'series_export'
const detailRowId = ref<number | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const windowDetail = ref<WindowDetail | null>(null)
const deliveryDetail = ref<DeliveryReconciliation | null>(null)
const detailRows = computed(() => {
  if (!deliveryDetail.value) return []
  return [...deliveryDetail.value.deliveries, ...deliveryDetail.value.exclusions]
})
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

const readonlyFields = new Set(meta.readonlyFields ?? [])
const createFields = meta.fields.filter((field) => !readonlyFields.has(field))

function openCreate() {
  createValues.value = Object.fromEntries(createFields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (supportsDetail) await openDetail(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function openDetail(row: EntryRow) {
  detailRowId.value = Number(row.id)
  detailLoading.value = true
  detailError.value = ''
  windowDetail.value = null
  deliveryDetail.value = null
  try {
    if (meta.key === 'rollup_window') {
      windowDetail.value = await loadWindowDetail(Number(row.id))
    } else {
      deliveryDetail.value = await loadDeliveryDetail(Number(row.id))
    }
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '明细读取失败'
  } finally {
    detailLoading.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
