// This application intentionally uses an inline template to keep the frontend build
// dependency-light, so it needs Vue's build that includes the runtime template compiler.
import { computed, createApp, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue/dist/vue.esm-bundler.js'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { eventLabels, formatBeijing, locationFilterLabel, percent, queryString, statusLabels } from './utils.js'
import './styles.css'

const API = '/api/v1'

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(payload?.error?.message || payload?.detail || `请求失败 (${response.status})`)
    error.status = response.status
    throw error
  }
  return payload
}

const App = {
  setup() {
    const user = ref(null)
    const active = ref('dashboard')
    const loading = ref(false)
    const error = ref('')
    const notice = ref('')
    const data = reactive({
      dashboard: null, persons: [], sources: [], tasks: [], runs: [], events: [], eventTotal: 0,
      selectedEvent: null, users: [], allPages: [], config: null, audit: [], search: [], documents: [], mapPeople: [], locations: [],
      notificationConfig: null, notificationRules: [], notificationOptions: { tasks: [], event_types: [] },
      deliveries: [], deliveryTotal: 0, selectedDelivery: null
    })
    const filters = reactive({
      q: '', person_id: '', event_type: '', confirmation_status: '', review_status: '',
      start_date: '', end_date: '', sort_order: 'desc', location: []
    })
    const loginForm = reactive({ username: 'admin', password: '' })
    const personForm = reactive({ name: '', aliases: '', organization: '', title: '', country_region: '', language: 'zh-CN' })
    const editingPersonId = ref(null)
    const sourceForm = reactive({ name: '', type: 'website', entry_url: '', trust_level: 3, schedule_seconds: 3600, person_ids: [], discovery_enabled: true, discovery_max_pages: 12, discovery_max_depth: 1 })
    const editingSourceId = ref(null)
    const documentForm = reactive({ source_id: '', title: '', content_text: '', canonical_url: '', published_at: '' })
    const maintenance = reactive({
      attribution: { person_id: '', source_id: '', result: null },
      chinadaily: { source_id: '', result: null }
    })
    const emailForm = reactive({
      enabled: false, smtp_host: '', smtp_port: 587, security: 'starttls', username: '',
      password_env: 'PFTS_SMTP_PASSWORD', credential_key_env: 'PFTS_NOTIFICATION_CREDENTIAL_KEY',
      from_address: '', from_name: '', to_addresses: '', subject_prefix: '[PFTS]',
      max_events_per_message: 25, worker_poll_seconds: 15, max_attempts: 5,
      retry_base_seconds: 60, timeout_seconds: 15, password: '', clear_password: false
    })
    const ruleForm = reactive({ name: '', task_ids: [], event_types: [], enabled: true })
    const editingRuleId = ref(null)
    const deliveryFilters = reactive({ task_id: '', delivery_status: '' })
    const searchTerm = ref('')
    const mapEl = ref(null)
    const mapPersonId = ref('')
    let leafletMap = null

    const nav = [
      ['dashboard', '总览'], ['timeline', '时间线'], ['persons', '人物'], ['map', '地图'], ['search', '搜索'],
      ['review', '审核中心'], ['sources', '信息源'], ['tasks', '任务中心'], ['notifications', '推送管理'], ['users', '用户权限'],
      ['config', '系统配置'], ['audit', '审计日志']
    ]
    const visibleNav = computed(() => nav.filter(([key]) => user.value?.pages?.includes(key)))
    const manualSources = computed(() => data.sources.filter(source => source.type === 'manual'))

    function flash(message) {
      notice.value = message
      window.setTimeout(() => { notice.value = '' }, 3000)
    }

    async function perform(action) {
      loading.value = true
      error.value = ''
      try { return await action() }
      catch (err) {
        if (err.status === 401) user.value = null
        error.value = err.message
        throw err
      } finally { loading.value = false }
    }

    async function checkSession() {
      try {
        user.value = await api('/auth/me')
        if (!user.value.pages.includes(active.value)) active.value = user.value.pages[0] || 'dashboard'
        await loadPage()
        const linkedEvent = new URLSearchParams(window.location.search).get('event_id')
        if (linkedEvent && user.value.pages.includes('timeline')) await openEvent(Number(linkedEvent))
      } catch { user.value = null }
    }

    async function login() {
      await perform(async () => {
        const result = await api('/auth/login', { method: 'POST', body: JSON.stringify(loginForm) })
        user.value = result.user
        active.value = user.value.pages[0] || 'dashboard'
        loginForm.password = ''
        await loadPage()
      }).catch(() => {})
    }

    async function logout() {
      try { await api('/auth/logout', { method: 'POST' }) } finally { user.value = null }
    }

    async function loadCommon() {
      if (user.value?.pages.includes('persons')) data.persons = (await api('/persons')).items
      if (user.value?.pages.includes('sources')) data.sources = (await api('/sources')).items
    }

    async function loadPage() {
      if (!user.value) return
      await perform(async () => {
        if (active.value === 'dashboard') data.dashboard = await api('/dashboard/summary')
        else if (['timeline', 'review', 'map'].includes(active.value)) {
          if (!data.persons.length && user.value.pages.includes('persons')) data.persons = (await api('/persons')).items
          if (active.value === 'timeline' && !data.locations.length) data.locations = (await api('/events/locations')).items
          const params = active.value === 'map' ? { page_size: 100, event_type: 'itinerary', person_id: mapPersonId.value } : { page_size: 100, ...filters }
          if (active.value === 'review') params.review_status = 'needs_review'
          const result = await api(`/events?${queryString(params)}`)
          data.events = result.items; data.eventTotal = result.total
        } else if (active.value === 'persons') data.persons = (await api(`/persons?${queryString({ q: filters.q })}`)).items
        else if (active.value === 'sources') { await loadCommon() }
        else if (active.value === 'tasks') {
          data.tasks = (await api('/tasks')).items
          data.runs = (await api('/task-runs?page_size=30')).items
          if (user.value.role === 'admin') await loadCommon()
        } else if (active.value === 'notifications') {
          const [configResult, rulesResult, optionsResult, deliveriesResult] = await Promise.all([
            api('/notifications/email/config'), api('/notifications/rules'), api('/notifications/options'),
            api(`/notifications/deliveries?${queryString({ page_size: 50, ...deliveryFilters })}`)
          ])
          data.notificationConfig = configResult
          data.notificationRules = rulesResult.items
          data.notificationOptions = optionsResult
          data.deliveries = deliveriesResult.items
          data.deliveryTotal = deliveriesResult.total
          const config = configResult.config
          Object.assign(emailForm, {
            enabled: Boolean(config.enabled), smtp_host: config.smtp_host || '', smtp_port: config.smtp_port || 587,
            security: config.security || 'starttls', username: config.username || '',
            password_env: config.password_env?.environment_variable || 'PFTS_SMTP_PASSWORD',
            credential_key_env: config.credential_key_env?.environment_variable || 'PFTS_NOTIFICATION_CREDENTIAL_KEY',
            from_address: config.from_address || '', from_name: config.from_name || '',
            to_addresses: (config.to_addresses || []).join(', '), subject_prefix: config.subject_prefix || '[PFTS]',
            max_events_per_message: config.max_events_per_message || 25,
            worker_poll_seconds: config.worker_poll_seconds || 15, max_attempts: config.max_attempts || 5,
            retry_base_seconds: config.retry_base_seconds || 60, timeout_seconds: config.timeout_seconds || 15,
            password: '', clear_password: false
          })
        } else if (active.value === 'users') {
          const result = await api('/users'); data.users = result.items; data.allPages = result.all_pages
        } else if (active.value === 'config') data.config = (await api('/config/effective')).config
        else if (active.value === 'audit') data.audit = (await api('/audit-logs?page_size=50')).items
        else if (active.value === 'search' && searchTerm.value) data.search = (await api(`/search?${queryString({ q: searchTerm.value })}`)).items
      }).catch(() => {})
    }

    async function renderMap() {
      if (active.value !== 'map') return
      const config = await api('/map/config')
      await nextTick()
      if (!mapEl.value || config.provider !== 'leaflet' || !config.tile_url) return
      if (leafletMap) leafletMap.remove()
      leafletMap = L.map(mapEl.value).setView(config.default_center, config.default_zoom)
      L.tileLayer(config.tile_url, { attribution: config.attribution, maxZoom: 19 }).addTo(leafletMap)
      const points = data.events.filter(e => e.latitude !== null && e.latitude !== '' && e.longitude !== null && e.longitude !== '' && Number.isFinite(Number(e.latitude)) && Number.isFinite(Number(e.longitude)))
      points.forEach(event => {
        const marker = L.marker([Number(event.latitude), Number(event.longitude)]).addTo(leafletMap)
        const popup = document.createElement('div')
        const person = document.createElement('strong'); person.textContent = event.person_name
        popup.append(person, document.createElement('br'), document.createTextNode(event.location_name || ''), document.createElement('br'), document.createTextNode(event.title))
        marker.bindPopup(popup)
        marker.on('click', () => openEvent(event.id))
      })
      if (points.length) leafletMap.fitBounds(points.map(e => [Number(e.latitude), Number(e.longitude)]), { padding: [30, 30], maxZoom: 10 })
    }

    async function reloadMap() { await loadPage(); await renderMap() }
    async function selectPage(key) {
      active.value = key; data.selectedEvent = null
      if (key === 'map') data.mapPeople = (await api('/map/people')).items
      await loadPage(); if (key === 'map') await renderMap()
    }
    onBeforeUnmount(() => { if (leafletMap) leafletMap.remove() })

    async function openEvent(id) {
      await perform(async () => { data.selectedEvent = await api(`/events/${id}`) }).catch(() => {})
    }

    function resetPersonForm() {
      Object.assign(personForm, { name: '', aliases: '', organization: '', title: '', country_region: '', language: 'zh-CN' })
      editingPersonId.value = null
    }

    function startEditPerson(person) {
      editingPersonId.value = person.id
      Object.assign(personForm, {
        name: person.name || '', aliases: (person.aliases || []).join(', '), organization: person.organization || '',
        title: person.title || '', country_region: person.country_region || '', language: person.language || 'zh-CN'
      })
      window.scrollTo({ top: 0, behavior: 'smooth' })
    }

    async function savePerson() {
      await perform(async () => {
        const editing = editingPersonId.value
        await api(editing ? `/persons/${editing}` : '/persons', { method: editing ? 'PUT' : 'POST', body: JSON.stringify({
          ...personForm, aliases: personForm.aliases.split(/[,，]/).map(v => v.trim()).filter(Boolean), enabled: true, bio: '', native_name: '', avatar_path: ''
        }) })
        resetPersonForm()
        data.persons = (await api('/persons')).items; flash(editing ? '人物信息已更新' : '人物已创建')
      }).catch(() => {})
    }

    async function deletePerson(person) {
      if (!window.confirm(`确定删除“${person.name}”吗？历史事件和证据会保留。`)) return
      await perform(async () => {
        await api(`/persons/${person.id}`, { method: 'DELETE' })
        if (editingPersonId.value === person.id) resetPersonForm()
        data.persons = (await api('/persons')).items
        flash('人物已删除，历史事件和证据已保留')
      }).catch(() => {})
    }

    function resetSourceForm() {
      Object.assign(sourceForm, { name: '', type: 'website', entry_url: '', trust_level: 3, schedule_seconds: 3600, person_ids: [], discovery_enabled: true, discovery_max_pages: 12, discovery_max_depth: 1 })
      editingSourceId.value = null
    }

    function startEditSource(source) {
      editingSourceId.value = source.id
      Object.assign(sourceForm, {
        name: source.name || '', type: source.display_type || source.type, entry_url: source.entry_url || '',
        trust_level: source.trust_level || 3, schedule_seconds: source.schedule_seconds || 3600,
        person_ids: [...(source.person_ids || [])], discovery_enabled: Boolean(source.discovery_enabled),
        discovery_max_pages: source.discovery_max_pages || 12, discovery_max_depth: source.discovery_max_depth ?? 1
      })
      window.scrollTo({ top: 0, behavior: 'smooth' })
    }

    async function saveSource() {
      await perform(async () => {
        const editing = editingSourceId.value
        await api(editing ? `/sources/${editing}` : '/sources', { method: editing ? 'PUT' : 'POST', body: JSON.stringify({
          ...sourceForm, discovery_enabled: sourceForm.type === 'website', entry_url: sourceForm.type === 'manual' ? '' : sourceForm.entry_url,
          organization: '', language: 'zh-CN', enabled: true
        }) })
        resetSourceForm()
        await loadCommon(); flash(editing ? '信息源已更新' : '信息源与采集任务已创建')
      }).catch(() => {})
    }

    async function deleteSource(source) {
      if (!window.confirm(`确定删除信息源“${source.name}”吗？历史材料会保留，采集任务将停用。`)) return
      await perform(async () => {
        await api(`/sources/${source.id}`, { method: 'DELETE' })
        if (editingSourceId.value === source.id) resetSourceForm()
        await loadCommon(); flash('信息源已删除，历史材料已保留')
      }).catch(() => {})
    }

    async function testSource(id) {
      await perform(async () => { const result = await api(`/sources/${id}/test`, { method: 'POST' }); flash(result.message) }).catch(() => {})
    }

    async function addDocument() {
      await perform(async () => {
        const result = await api('/documents/manual', { method: 'POST', body: JSON.stringify({
          ...documentForm, source_id: Number(documentForm.source_id), published_at: documentForm.published_at || null
        }) })
        Object.assign(documentForm, { source_id: '', title: '', content_text: '', canonical_url: '', published_at: '' })
        flash(`材料已分析，生成 ${result.event_count} 条事件`)
      }).catch(() => {})
    }

    async function runTask(id) {
      await perform(async () => { const result = await api(`/tasks/${id}/run`, { method: 'POST' }); flash(`运行完成：${result.status}`); await loadPage() }).catch(() => {})
    }

    async function saveEmailConfig() {
      const payload = {
        ...emailForm,
        smtp_port: Number(emailForm.smtp_port),
        max_events_per_message: Number(emailForm.max_events_per_message),
        worker_poll_seconds: Number(emailForm.worker_poll_seconds),
        max_attempts: Number(emailForm.max_attempts),
        retry_base_seconds: Number(emailForm.retry_base_seconds),
        timeout_seconds: Number(emailForm.timeout_seconds),
        to_addresses: emailForm.to_addresses.split(/[,;，；\n]/).map(value => value.trim()).filter(Boolean),
        clear_fields: []
      }
      await perform(async () => {
        await api('/notifications/email/config', { method: 'PUT', body: JSON.stringify(payload) })
        flash('邮件配置已保存')
        await loadPage()
      }).catch(() => {})
    }

    async function resetEmailOverrides() {
      if (!window.confirm('确定清除页面邮件配置并恢复 app.json / 环境变量配置吗？')) return
      const clear_fields = [
        'enabled', 'smtp_host', 'smtp_port', 'security', 'username', 'password_env', 'credential_key_env',
        'from_address', 'from_name', 'to_addresses', 'subject_prefix', 'max_events_per_message',
        'worker_poll_seconds', 'max_attempts', 'retry_base_seconds', 'timeout_seconds'
      ]
      await perform(async () => {
        await api('/notifications/email/config', {
          method: 'PUT', body: JSON.stringify({ clear_fields, clear_password: true })
        })
        flash('已恢复文件/环境配置')
        await loadPage()
      }).catch(() => {})
    }

    async function testEmail() {
      await perform(async () => {
        const result = await api('/notifications/email/test', { method: 'POST' })
        flash(result.message || '测试邮件已发送')
      }).catch(() => {})
    }

    function resetRuleForm() {
      Object.assign(ruleForm, { name: '', task_ids: [], event_types: [], enabled: true })
      editingRuleId.value = null
    }

    function selectAllRuleTasks() {
      ruleForm.task_ids = data.notificationOptions.tasks.map(task => Number(task.id))
    }

    function clearRuleTasks() {
      ruleForm.task_ids = []
    }

    function editRule(rule) {
      editingRuleId.value = rule.id
      Object.assign(ruleForm, {
        name: rule.name, task_ids: [...rule.task_ids], event_types: [...rule.event_types], enabled: Boolean(rule.enabled)
      })
    }

    async function saveNotificationRule() {
      await perform(async () => {
        const editing = editingRuleId.value
        await api(editing ? `/notifications/rules/${editing}` : '/notifications/rules', {
          method: editing ? 'PUT' : 'POST',
          body: JSON.stringify({
            ...ruleForm, task_ids: ruleForm.task_ids.map(Number), event_types: [...ruleForm.event_types]
          })
        })
        resetRuleForm()
        flash(editing ? '推送规则已更新' : '推送规则已创建')
        await loadPage()
      }).catch(() => {})
    }

    async function toggleRule(rule) {
      await perform(async () => {
        await api(`/notifications/rules/${rule.id}`, {
          method: 'PUT',
          body: JSON.stringify({ name: rule.name, task_ids: rule.task_ids, event_types: rule.event_types, enabled: !rule.enabled })
        })
        await loadPage()
      }).catch(() => {})
    }

    async function removeRule(rule) {
      if (!window.confirm(`确定删除推送规则“${rule.name}”吗？历史投递记录会保留。`)) return
      await perform(async () => {
        await api(`/notifications/rules/${rule.id}`, { method: 'DELETE' })
        if (editingRuleId.value === rule.id) resetRuleForm()
        flash('推送规则已删除')
        await loadPage()
      }).catch(() => {})
    }

    async function openDelivery(id) {
      await perform(async () => { data.selectedDelivery = await api(`/notifications/deliveries/${id}`) }).catch(() => {})
    }

    async function retryDelivery(id) {
      await perform(async () => {
        await api(`/notifications/deliveries/${id}/retry`, { method: 'POST' })
        data.selectedDelivery = null
        flash('失败批次已进入重试队列')
        await loadPage()
      }).catch(() => {})
    }

    async function runMaintenance(kind, dryRun) {
      const labels = { attribution: '事件归属重验', chinadaily: '中国日报正文清理' }
      if (!dryRun && !window.confirm(`即将执行“${labels[kind]}”，会修改自动生成且未人工锁定的数据。确认已经完成 SQLite 备份并继续吗？`)) return
      const state = maintenance[kind]
      const path = kind === 'attribution' ? '/maintenance/recheck-event-attribution' : '/maintenance/cleanup-chinadaily-content'
      const payload = {
        dry_run: dryRun,
        source_id: state.source_id ? Number(state.source_id) : null,
        ...(kind === 'attribution' ? { person_id: state.person_id ? Number(state.person_id) : null } : {})
      }
      await perform(async () => {
        state.result = await api(path, { method: 'POST', body: JSON.stringify(payload) })
        flash(`${labels[kind]}${dryRun ? '预览' : '执行'}完成`)
        if (!dryRun) await loadPage()
      }).catch(() => {})
    }

    async function review(id, action) {
      const reason = window.prompt(action === 'approve' ? '审核说明（可选）' : '请填写驳回原因', '')
      if (reason === null) return
      await perform(async () => {
        await api(`/events/${id}/review`, { method: 'POST', body: JSON.stringify({ action, reason }) })
        flash('审核结果已保存'); data.selectedEvent = null; await loadPage()
      }).catch(() => {})
    }

    async function savePermissions(target) {
      await perform(async () => {
        await api(`/users/${target.id}/permissions`, { method: 'PUT', body: JSON.stringify({ pages: target.pages }) })
        flash('权限已保存')
      }).catch(() => {})
    }

    async function searchNow() { active.value = 'search'; await loadPage() }

    watch(() => [filters.person_id, filters.event_type, filters.confirmation_status, filters.review_status, filters.start_date, filters.end_date, filters.sort_order, filters.location.join('|')], () => {
      if (['timeline', 'map'].includes(active.value)) loadPage()
    })
    onMounted(checkSession)

    return {
      user, active, loading, error, notice, data, filters, loginForm, personForm, editingPersonId, sourceForm, editingSourceId, documentForm,
      maintenance, emailForm, ruleForm, editingRuleId, deliveryFilters, searchTerm, mapEl, mapPersonId, reloadMap, eventLabels, statusLabels, formatBeijing, locationFilterLabel, percent, visibleNav, manualSources,
      login, logout, loadPage, selectPage, openEvent, savePerson, startEditPerson, resetPersonForm, deletePerson, saveSource, startEditSource, resetSourceForm, deleteSource, testSource, addDocument,
      runTask, runMaintenance, saveEmailConfig, resetEmailOverrides, testEmail, saveNotificationRule, editRule, resetRuleForm, selectAllRuleTasks, clearRuleTasks, toggleRule, removeRule,
      openDelivery, retryDelivery, review, savePermissions, searchNow
    }
  },
  template: `
    <main v-if="!user" class="login-shell">
      <section class="login-panel">
        <div class="brand-mark">足</div>
        <p class="eyebrow">PUBLIC FIGURES · EVIDENCE FIRST</p>
        <h1>人物足迹</h1>
        <p class="muted">把公开行程、言论及其他相关事实整理成一条可以核验的时间线。</p>
        <form @submit.prevent="login" class="stack">
          <label>用户名<input v-model="loginForm.username" autocomplete="username" required /></label>
          <label>密码<input v-model="loginForm.password" type="password" autocomplete="current-password" required /></label>
          <button class="primary" :disabled="loading">{{ loading ? '正在登录…' : '进入系统' }}</button>
          <p v-if="error" class="error">{{ error }}</p>
        </form>
      </section>
    </main>

    <div v-else class="app-shell">
      <aside class="sidebar">
        <div class="brand"><span>足</span><div><strong>人物足迹</strong><small>PFTS · 证据优先</small></div></div>
        <nav><button v-for="item in visibleNav" :key="item[0]" :class="{ active: active === item[0] }" @click="selectPage(item[0])">{{ item[1] }}</button></nav>
        <div class="user-card"><small>当前用户</small><strong>{{ user.username }}</strong><span>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</span><button @click="logout">退出</button></div>
      </aside>
      <section class="content">
        <header><div><p class="eyebrow">LIVE RESEARCH DESK</p><h2>{{ visibleNav.find(v => v[0] === active)?.[1] }}</h2></div><div class="header-meta"><span class="live-dot"></span> 北京时间 · 数据可追溯</div></header>
        <p v-if="error" class="error banner">{{ error }} <button @click="error=''">×</button></p>
        <p v-if="notice" class="notice banner">{{ notice }}</p>
        <div v-if="loading" class="loading">正在读取数据…</div>

        <template v-if="active === 'dashboard' && data.dashboard">
          <div class="metric-grid">
            <article v-for="(value,key) in data.dashboard.counts" :key="key" class="metric"><span>{{ {persons:'跟踪人物',sources:'启用来源',documents_today:'今日材料',events_today:'今日事件',needs_review:'待审核',failed_tasks:'异常任务'}[key] }}</span><strong>{{ value }}</strong></article>
          </div>
          <div class="two-column">
            <section class="panel"><div class="section-title"><h3>最新事件</h3><button @click="selectPage('timeline')">查看全部</button></div><div class="event-list"><button class="event-row" v-for="event in data.dashboard.recent_events" :key="event.id" @click="openEvent(event.id)"><span :class="['type',event.event_type]">{{ eventLabels[event.event_type] }}</span><div><strong>{{ event.title }}</strong><small>{{ event.person_name }} · {{ formatBeijing(event.start_at) }}</small></div><span class="status">{{ statusLabels[event.confirmation_status] }}</span></button><p v-if="!data.dashboard.recent_events.length" class="empty">还没有事件，先创建人物和人工来源。</p></div></section>
            <section class="panel"><div class="section-title"><h3>运行健康</h3></div><div v-if="data.dashboard.failed_runs.length" class="event-list"><div class="event-row static" v-for="run in data.dashboard.failed_runs" :key="run.id"><span class="type warning">!</span><div><strong>{{ run.task_name }}</strong><small>{{ run.error_summary || run.status }}</small></div></div></div><div v-else class="healthy"><b>✓</b><strong>没有失败任务</strong><span>采集链路目前很安静。</span></div></section>
          </div>
        </template>

        <template v-if="['timeline','review'].includes(active)">
          <div :class="['toolbar', { 'timeline-toolbar': active === 'timeline' }]">
            <input v-model="filters.q" @keyup.enter="loadPage" placeholder="搜索标题、地点或言论" />
            <select v-model="filters.person_id"><option value="">全部人物</option><option v-for="p in data.persons" :value="p.id">{{ p.name }}</option></select>
            <select v-model="filters.event_type"><option value="">全部类型</option><option value="itinerary">行程</option><option value="statement">言论</option><option value="other">其他</option></select>
            <select v-if="active==='timeline'" v-model="filters.confirmation_status"><option value="">全部发生状态</option><option v-for="s in ['rumored','expected','confirmed','ongoing','completed','cancelled','disputed']" :value="s">{{ statusLabels[s] }}</option></select>
            <select v-if="active==='timeline'" v-model="filters.review_status"><option value="">全部审核状态</option><option v-for="s in ['pending','needs_review','approved','rejected']" :value="s">{{ statusLabels[s] }}</option></select>
            <input v-if="active==='timeline'" v-model="filters.start_date" type="date" title="开始日期" />
            <input v-if="active==='timeline'" v-model="filters.end_date" type="date" title="结束日期" />
            <select v-if="active==='timeline'" v-model="filters.sort_order"><option value="desc">时间降序（新到旧）</option><option value="asc">时间升序（旧到新）</option></select>
            <details v-if="active==='timeline'" class="location-filter">
              <summary aria-label="选择地点（可多选）">
                <span>{{ locationFilterLabel(filters.location) }}</span>
                <span aria-hidden="true" class="location-filter-arrow">⌄</span>
              </summary>
              <div class="location-filter-panel" aria-label="地点选项">
                <div class="location-filter-heading">
                  <strong>选择地点</strong>
                  <button v-if="filters.location.length" type="button" @click="filters.location=[]">清空</button>
                </div>
                <label v-for="place in data.locations" :key="place">
                  <input v-model="filters.location" type="checkbox" :value="place" />
                  <span>{{ place }}</span>
                </label>
                <p v-if="!data.locations.length" class="location-filter-empty">暂无可选地点</p>
              </div>
            </details>
            <button @click="loadPage">筛选</button>
          </div>
          <p class="result-count">共 {{ data.eventTotal }} 条事件</p>
          <div class="timeline">
            <article v-for="event in data.events" :key="event.id" :class="['timeline-card', event.event_type]" @click="openEvent(event.id)">
              <div class="timeline-date"><strong>{{ formatBeijing(event.start_at).split(' ')[0] }}</strong><span>{{ event.time_precision === 'unknown' ? '时间未知' : '北京时间' }}</span></div>
              <div class="timeline-body"><div class="card-meta"><span :class="['type',event.event_type]">{{ eventLabels[event.event_type] }}</span><span>{{ event.person_name }}</span><span>⌖ {{ event.location_name || '无地点' }}</span></div><h3>{{ event.title }}</h3><p>{{ event.summary }}</p><footer><span class="status">{{ statusLabels[event.confirmation_status] }}</span><span class="status">{{ statusLabels[event.review_status] }}</span><span>可信度 {{ percent(event.confidence) }}</span><span>{{ event.source_names || '来源未知' }}</span></footer></div>
            </article>
            <p v-if="!data.events.length" class="empty">这个筛选条件下还没有事件。</p>
          </div>
        </template>

        <template v-if="active === 'persons'">
          <section v-if="user.role==='admin'" class="panel form-panel"><div class="section-title"><h3>{{ editingPersonId ? '编辑人物' : '新增跟踪人物' }}</h3><button v-if="editingPersonId" @click="resetPersonForm">取消编辑</button></div><form class="form-grid" @submit.prevent="savePerson"><label>姓名<input v-model="personForm.name" required /></label><label>别名（逗号分隔）<input v-model="personForm.aliases" /></label><label>组织<input v-model="personForm.organization" /></label><label>职位<input v-model="personForm.title" /></label><label>国家/地区<input v-model="personForm.country_region" /></label><button class="primary">{{ editingPersonId ? '保存修改' : '创建人物' }}</button></form></section>
          <div class="card-grid"><article class="person-card" v-for="person in data.persons" :key="person.id"><div class="avatar">{{ person.name.slice(0,1) }}</div><div><h3>{{ person.name }}</h3><p>{{ person.title || '公开人物' }}<span v-if="person.organization"> · {{ person.organization }}</span></p><small>{{ person.aliases.join(' / ') || '暂无别名' }}</small></div><div class="person-side"><strong>{{ person.event_count }}<small>事件</small></strong><div v-if="user.role==='admin'" class="person-actions"><button @click="startEditPerson(person)">编辑</button><button class="delete-link" @click="deletePerson(person)">删除</button></div></div></article><p v-if="!data.persons.length" class="empty">还没有人物。</p></div>
        </template>

        <template v-if="active === 'sources'">
          <section class="panel form-panel"><div class="section-title"><h3>{{ editingSourceId ? '编辑信息源' : '新增信息源' }}</h3><button v-if="editingSourceId" @click="resetSourceForm">取消编辑</button></div><p class="form-hint">选择“网站（自动发现）”后，只需填写网站入口；系统会在同域页面中查找与关联人物姓名或别名匹配的资讯链接。</p><form class="form-grid" @submit.prevent="saveSource"><label>名称<input v-model="sourceForm.name" required /></label><label>类型<select v-model="sourceForm.type"><option value="website">网站（自动发现）</option><option value="rss">RSS / Atom</option><option value="web_page">单篇网页</option><option value="manual">人工材料</option></select></label><label v-if="sourceForm.type!=='manual'">{{ sourceForm.type==='website' ? '网站入口 URL' : '入口 URL' }}<input v-model="sourceForm.entry_url" type="url" required /></label><label>可信等级<input v-model.number="sourceForm.trust_level" type="number" min="1" max="5" /></label><label>关联人物<select v-model="sourceForm.person_ids" multiple :required="sourceForm.type==='website'"><option v-for="p in data.persons" :value="p.id">{{ p.name }}</option></select></label><label v-if="sourceForm.type==='website'">最多扫描页面<input v-model.number="sourceForm.discovery_max_pages" type="number" min="1" max="50" /></label><label v-if="sourceForm.type==='website'">最大站内层级<select v-model.number="sourceForm.discovery_max_depth"><option :value="0">仅入口页</option><option :value="1">入口页 + 一层栏目</option><option :value="2">最多两层栏目</option></select></label><label>采集周期（秒）<input v-model.number="sourceForm.schedule_seconds" type="number" min="60" /></label><button class="primary">{{ editingSourceId ? '保存修改' : '创建来源' }}</button></form></section>
          <section class="panel"><table><thead><tr><th>来源</th><th>类型</th><th>可信度</th><th>材料数</th><th>最近状态</th><th></th></tr></thead><tbody><tr v-for="source in data.sources" :key="source.id"><td><strong>{{ source.name }}</strong><small>{{ source.entry_url || '人工录入' }}</small></td><td>{{ {website:'网站发现',web_page:'单篇网页',rss:'RSS',manual:'人工'}[source.display_type || source.type] }}</td><td>{{ source.trust_level }}/5</td><td>{{ source.document_count }}</td><td>{{ source.last_status || '尚未运行' }}</td><td><div class="table-actions"><button @click="testSource(source.id)">测试</button><button @click="startEditSource(source)">编辑</button><button class="delete-link" @click="deleteSource(source)">删除</button></div></td></tr></tbody></table></section>
          <section class="panel form-panel"><div class="section-title"><h3>录入公开材料</h3><span>保存后立即分析</span></div><form class="stack" @submit.prevent="addDocument"><div class="form-grid"><label>人工来源<select v-model="documentForm.source_id" required><option value="">请选择</option><option v-for="s in manualSources" :value="s.id">{{ s.name }}</option></select></label><label>标题<input v-model="documentForm.title" required /></label><label>公开时间<input v-model="documentForm.published_at" type="datetime-local" /></label><label>原文链接<input v-model="documentForm.canonical_url" type="url" /></label></div><label>正文<textarea v-model="documentForm.content_text" rows="8" required placeholder="粘贴公开来源正文；系统只会从这里提取事实。"></textarea></label><button class="primary">保存并分析</button></form></section>
        </template>

        <template v-if="active === 'tasks'">
          <section v-if="user.role==='admin'" class="panel maintenance-panel">
            <div class="section-title"><h3>数据质量维护</h3><span>请先预览；执行前备份 SQLite，人工锁定事件不会修改</span></div>
            <div class="maintenance-grid">
              <article>
                <h4>事件归属重验</h4>
                <p>重新检查证据中的动作/言论主体，清除仅被提及人物的错误归属。</p>
                <div class="maintenance-filters">
                  <label>人物范围<select v-model="maintenance.attribution.person_id"><option value="">全部人物</option><option v-for="person in data.persons" :key="person.id" :value="person.id">{{ person.name }}</option></select></label>
                  <label>来源范围<select v-model="maintenance.attribution.source_id"><option value="">全部来源</option><option v-for="source in data.sources" :key="source.id" :value="source.id">{{ source.name }}</option></select></label>
                </div>
                <div class="maintenance-actions"><button @click="runMaintenance('attribution',true)">预览影响</button><button class="danger" @click="runMaintenance('attribution',false)">确认执行</button></div>
                <div v-if="maintenance.attribution.result" class="maintenance-result">
                  <strong>{{ maintenance.attribution.result.dry_run ? '预览结果' : '执行结果' }}</strong>
                  <span>扫描证据 {{ maintenance.attribution.result.scanned_evidence }}</span>
                  <span>无效证据 {{ maintenance.attribution.result.invalid_evidence }}</span>
                  <span>孤立事件 {{ maintenance.attribution.result.orphan_events }}</span>
                  <span>保留事件 {{ maintenance.attribution.result.kept_events }}</span>
                  <span>跳过锁定 {{ maintenance.attribution.result.locked_skipped }}</span>
                  <ul v-if="maintenance.attribution.result.sample?.length"><li v-for="item in maintenance.attribution.result.sample" :key="item.evidence_id">{{ item.person_name }} · {{ item.title }} · {{ item.reason }}</li></ul>
                </div>
              </article>
              <article>
                <h4>中国日报正文清理</h4>
                <p>识别首页、频道和页面框架污染，清洗合法文章并重做自动分析。</p>
                <div class="maintenance-filters">
                  <label>来源范围<select v-model="maintenance.chinadaily.source_id"><option value="">全部中国日报来源</option><option v-for="source in data.sources" :key="source.id" :value="source.id">{{ source.name }}</option></select></label>
                </div>
                <div class="maintenance-actions"><button @click="runMaintenance('chinadaily',true)">预览影响</button><button class="danger" @click="runMaintenance('chinadaily',false)">确认执行</button></div>
                <div v-if="maintenance.chinadaily.result" class="maintenance-result">
                  <strong>{{ maintenance.chinadaily.result.dry_run ? '预览结果' : '执行结果' }}</strong>
                  <span>扫描材料 {{ maintenance.chinadaily.result.scanned_documents }}</span>
                  <span>拒绝材料 {{ maintenance.chinadaily.result.rejected_documents }}</span>
                  <span>可清洗材料 {{ maintenance.chinadaily.result.cleanable_documents }}</span>
                  <span>孤立事件 {{ maintenance.chinadaily.result.orphan_events }}</span>
                  <span>跳过锁定 {{ maintenance.chinadaily.result.locked_skipped }}</span>
                  <ul v-if="maintenance.chinadaily.result.sample?.length"><li v-for="item in maintenance.chinadaily.result.sample" :key="item.id">{{ item.source_name }} · {{ item.title }} · {{ item.reason }}</li></ul>
                </div>
              </article>
            </div>
          </section>
          <section class="panel"><table><thead><tr><th>任务</th><th>来源</th><th>周期</th><th>上次运行</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="task in data.tasks" :key="task.id"><td><strong>{{ task.name }}</strong></td><td>{{ task.source_name }}</td><td>{{ task.schedule_seconds }} 秒</td><td>{{ formatBeijing(task.last_run_at) }}</td><td>{{ task.last_status || '未运行' }}</td><td><button class="primary small" @click="runTask(task.id)">立即运行</button></td></tr></tbody></table></section>
          <section class="panel"><div class="section-title"><h3>最近运行</h3></div><table><thead><tr><th>任务</th><th>开始时间</th><th>状态</th><th>发现/新增/重复</th><th>事件</th><th>邮件</th><th>失败</th></tr></thead><tbody><tr v-for="run in data.runs" :key="run.id"><td>{{ run.task_name }}</td><td>{{ formatBeijing(run.started_at) }}</td><td><span class="status">{{ run.status }}</span></td><td>{{ run.discovered_count }}/{{ run.created_count }}/{{ run.duplicate_count }}</td><td>{{ run.event_count }}</td><td><button v-if="run.notification_batches && user.pages.includes('notifications')" @click="selectPage('notifications')">{{ run.notification_items }} 项 / {{ run.notification_batches }} 批</button><span v-else>{{ run.notification_items || 0 }}</span><small v-if="run.notification_error" class="delivery-error">{{ run.notification_error }}</small></td><td>{{ run.failed_count }}</td></tr></tbody></table></section>
        </template>

        <template v-if="active === 'notifications'">
          <section class="panel notification-summary" v-if="data.notificationConfig">
            <div class="section-title"><h3>邮件通道</h3><span :class="['status', emailForm.enabled ? 'success' : '']">{{ emailForm.enabled ? '已启用' : '已停用' }}</span></div>
            <p class="form-hint">页面配置逐字段覆盖 app.json 和环境变量；SMTP 密码始终脱敏。当前密码来源：{{ data.notificationConfig.config.password_source }}</p>
            <form v-if="user.role==='admin'" class="form-grid notification-form" @submit.prevent="saveEmailConfig">
              <label class="check-line"><input v-model="emailForm.enabled" type="checkbox" />启用邮件推送</label>
              <label>SMTP 主机<input v-model="emailForm.smtp_host" placeholder="smtp.example.com" /></label>
              <label>SMTP 端口<input v-model.number="emailForm.smtp_port" type="number" min="1" max="65535" /></label>
              <label>连接安全<select v-model="emailForm.security"><option value="starttls">STARTTLS</option><option value="ssl">SMTPS</option><option value="none">无加密</option></select></label>
              <label>用户名<input v-model="emailForm.username" autocomplete="username" /></label>
              <label>新 SMTP 密码<input v-model="emailForm.password" type="password" autocomplete="new-password" placeholder="留空则保持原值" /></label>
              <label>密码环境变量<input v-model="emailForm.password_env" /></label>
              <label>页面密码主密钥环境变量<input v-model="emailForm.credential_key_env" /></label>
              <label>发件邮箱<input v-model="emailForm.from_address" type="email" /></label>
              <label>发件人名称<input v-model="emailForm.from_name" /></label>
              <label class="span-two">收件邮箱（逗号分隔）<input v-model="emailForm.to_addresses" placeholder="one@example.com, two@example.com" /></label>
              <label>主题前缀<input v-model="emailForm.subject_prefix" /></label>
              <label>每封最大事件数<input v-model.number="emailForm.max_events_per_message" type="number" min="1" max="100" /></label>
              <label>Worker 轮询秒数<input v-model.number="emailForm.worker_poll_seconds" type="number" min="5" /></label>
              <label>最大尝试次数<input v-model.number="emailForm.max_attempts" type="number" min="1" max="20" /></label>
              <label>重试基数秒数<input v-model.number="emailForm.retry_base_seconds" type="number" min="1" /></label>
              <label>SMTP 超时秒数<input v-model.number="emailForm.timeout_seconds" type="number" min="1" max="120" /></label>
              <label class="check-line"><input v-model="emailForm.clear_password" type="checkbox" />清除页面保存的密码</label>
              <div class="notification-actions span-two"><button class="primary">保存邮件配置</button><button type="button" @click="testEmail">发送测试邮件</button><button type="button" class="delete-link" @click="resetEmailOverrides">恢复文件配置</button></div>
            </form>
            <dl class="config-sources"><div v-for="(source,field) in data.notificationConfig.sources" :key="field"><dt>{{ field }}</dt><dd>{{ source }}</dd></div></dl>
          </section>

          <section v-if="user.role==='admin'" class="panel form-panel">
            <div class="section-title"><h3>{{ editingRuleId ? '编辑推送规则' : '新增推送规则' }}</h3><button v-if="editingRuleId" @click="resetRuleForm">取消编辑</button></div>
            <form class="form-grid notification-form" @submit.prevent="saveNotificationRule">
              <label>规则名称<input v-model="ruleForm.name" required /></label>
              <label class="check-line"><input v-model="ruleForm.enabled" type="checkbox" />启用规则</label>
              <fieldset class="task-picker span-two">
                <legend>采集任务（已选择 {{ ruleForm.task_ids.length }} 项）</legend>
                <div class="task-picker-actions"><button type="button" @click="selectAllRuleTasks">全选</button><button type="button" @click="clearRuleTasks">清空</button></div>
                <div class="task-option-grid">
                  <label v-for="task in data.notificationOptions.tasks" :key="task.id" class="task-option">
                    <input v-model="ruleForm.task_ids" type="checkbox" :value="task.id" />
                    <span><strong>{{ task.name }}</strong><small>{{ task.source_name }} · {{ task.enabled ? '启用' : '停用' }}</small></span>
                  </label>
                </div>
                <p v-if="!data.notificationOptions.tasks.length" class="form-hint">暂无可选采集任务，请先在信息源页面创建任务。</p>
              </fieldset>
              <fieldset><legend>事件类型</legend><label v-for="type in data.notificationOptions.event_types" :key="type" class="check-line"><input v-model="ruleForm.event_types" type="checkbox" :value="type" />{{ eventLabels[type] }}</label></fieldset>
              <button class="primary">{{ editingRuleId ? '保存规则' : '创建规则' }}</button>
            </form>
          </section>

          <section class="panel">
            <div class="section-title"><h3>推送规则</h3><span>{{ data.notificationRules.length }} 条</span></div>
            <table><thead><tr><th>规则</th><th>任务</th><th>事件类型</th><th>状态</th><th v-if="user.role==='admin'"></th></tr></thead><tbody>
              <tr v-for="rule in data.notificationRules" :key="rule.id"><td><strong>{{ rule.name }}</strong></td><td>{{ rule.task_ids.map(id => data.notificationOptions.tasks.find(task => task.id===id)?.name || '#'+id).join('、') }}</td><td>{{ rule.event_types.map(type => eventLabels[type]).join('、') }}</td><td><span class="status">{{ rule.enabled ? '启用' : '停用' }}</span></td><td v-if="user.role==='admin'"><div class="table-actions"><button @click="editRule(rule)">编辑</button><button @click="toggleRule(rule)">{{ rule.enabled ? '停用' : '启用' }}</button><button class="delete-link" @click="removeRule(rule)">删除</button></div></td></tr>
            </tbody></table><p v-if="!data.notificationRules.length" class="empty">尚未创建推送规则。</p>
          </section>

          <section class="panel">
            <div class="section-title"><h3>投递记录</h3><span>共 {{ data.deliveryTotal }} 批</span></div>
            <div class="toolbar notification-toolbar"><select v-model="deliveryFilters.task_id"><option value="">全部任务</option><option v-for="task in data.notificationOptions.tasks" :value="task.id">{{ task.name }}</option></select><select v-model="deliveryFilters.delivery_status"><option value="">全部状态</option><option v-for="status in ['pending','sending','retrying','sent','failed','skipped']" :value="status">{{ status }}</option></select><button @click="loadPage">筛选</button></div>
            <table><thead><tr><th>时间</th><th>任务</th><th>收件人</th><th>事件</th><th>状态/尝试</th><th>错误</th></tr></thead><tbody>
              <tr v-for="delivery in data.deliveries" :key="delivery.id" @click="openDelivery(delivery.id)" class="clickable-row"><td>{{ formatBeijing(delivery.created_at) }}</td><td>{{ delivery.task_name }}<small>第 {{ delivery.part_number }} 部分</small></td><td>{{ delivery.recipient }}</td><td>{{ delivery.item_count }}</td><td><span class="status">{{ delivery.status }}</span> / {{ delivery.attempt_count }}</td><td class="delivery-error">{{ delivery.last_error || '—' }}</td></tr>
            </tbody></table><p v-if="!data.deliveries.length" class="empty">暂无投递记录；规则只会作用于创建后的新任务运行。</p>
          </section>
        </template>

        <template v-if="active === 'map'">
          <section class="map-fallback"><p class="eyebrow">LOCATION VIEW · SAFE PRECISION</p><h3>公开地点分布</h3><p>地图只展示公开报道中的地点，不推断实时位置或路线。</p><div class="map-toolbar"><select v-model="mapPersonId" @change="reloadMap"><option value="">全部人物</option><option v-for="p in data.mapPeople" :value="p.id">{{ p.name }}</option></select></div><div ref="mapEl" class="leaflet-map"></div><h3>未定位地点</h3><div class="location-grid"><button v-for="event in data.events.filter(e=>e.location_name && (e.latitude==null || e.longitude==null))" :key="event.id" @click="openEvent(event.id)"><span>⌖</span><strong>{{ event.location_name }}</strong><small>{{ event.person_name }} · {{ statusLabels[event.confirmation_status] }}</small></button></div><p v-if="!data.events.some(e=>e.location_name)" class="empty">暂无具有公开地点的事件。</p></section>
        </template>

        <template v-if="active === 'search'">
          <form class="search-hero" @submit.prevent="searchNow"><p class="eyebrow">FULL TEXT SEARCH</p><h3>从证据里找答案</h3><div><input v-model="searchTerm" placeholder="人物、地点、主题或原文关键词" required /><button class="primary">搜索</button></div></form><div class="search-results"><button v-for="item in data.search" :key="item.result_type+'-'+item.id" @click="item.result_type==='event' && openEvent(item.id)"><span>{{ item.result_type === 'event' ? '事件' : '材料' }}</span><div><strong>{{ item.title }}</strong><p>{{ item.summary }}</p><small>{{ item.person_name }} · {{ formatBeijing(item.start_at) }}</small></div></button><p v-if="searchTerm && !data.search.length" class="empty">没有找到匹配结果。</p></div>
        </template>

        <template v-if="active === 'users'">
          <section class="panel"><div class="section-title"><h3>用户与页面权限</h3><span>用户账号来自 data/password.txt</span></div><div class="permission-card" v-for="target in data.users" :key="target.id"><div><strong>{{ target.username }}</strong><span>{{ target.role }}</span><small>最近登录：{{ formatBeijing(target.last_login_at) }}</small></div><div class="checks"><label v-for="page in data.allPages"><input type="checkbox" :value="page" v-model="target.pages" :disabled="target.role==='admin'" />{{ page }}</label></div><button @click="savePermissions(target)" :disabled="target.role==='admin'">保存</button></div></section>
        </template>

        <template v-if="active === 'config'"><section class="panel"><div class="section-title"><h3>当前生效配置</h3><span>敏感字段已脱敏</span></div><pre>{{ JSON.stringify(data.config, null, 2) }}</pre></section></template>
        <template v-if="active === 'audit'"><section class="panel"><table><thead><tr><th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>结果</th><th>摘要</th></tr></thead><tbody><tr v-for="item in data.audit" :key="item.id"><td>{{ formatBeijing(item.created_at) }}</td><td>{{ item.username || '系统/未知' }}</td><td>{{ item.action }}</td><td>{{ item.object_type }} #{{ item.object_id }}</td><td>{{ item.result }}</td><td>{{ item.change_summary }}</td></tr></tbody></table></section></template>

          <aside v-if="data.selectedEvent" class="detail-overlay" @click.self="data.selectedEvent=null"><section class="detail-panel"><button class="close" @click="data.selectedEvent=null">×</button><div class="card-meta"><span :class="['type',data.selectedEvent.event_type]">{{ eventLabels[data.selectedEvent.event_type] }}</span><span>{{ data.selectedEvent.person_name }}</span></div><h2>{{ data.selectedEvent.title }}</h2><p class="lead">{{ data.selectedEvent.summary }}</p><div class="detail-facts"><div><small>发生时间</small><strong>{{ formatBeijing(data.selectedEvent.start_at) }}</strong></div><div><small>地点</small><strong>{{ data.selectedEvent.location_name || '无地点' }}</strong></div><div><small>确认状态</small><strong>{{ statusLabels[data.selectedEvent.confirmation_status] }}</strong></div><div><small>可信度</small><strong>{{ percent(data.selectedEvent.confidence) }}</strong></div></div><blockquote v-if="data.selectedEvent.quote_text">“{{ data.selectedEvent.quote_text }}”</blockquote><h3>证据链</h3><article class="evidence" v-for="ev in data.selectedEvent.evidence" :key="ev.id"><div><strong>{{ ev.source_name }}</strong><a v-if="ev.canonical_url.startsWith('http')" :href="ev.canonical_url" target="_blank" rel="noreferrer">查看原文 ↗</a></div><p>{{ ev.evidence_text }}</p><small>{{ ev.document_title }} · {{ formatBeijing(ev.published_at || ev.collected_at) }} · 来源等级 {{ ev.trust_level }}/5</small></article><div v-if="user.role==='admin'" class="review-actions"><button class="primary" @click="review(data.selectedEvent.id,'approve')">通过审核</button><button class="danger" @click="review(data.selectedEvent.id,'reject')">驳回</button></div></section></aside>
          <aside v-if="data.selectedDelivery" class="detail-overlay" @click.self="data.selectedDelivery=null"><section class="detail-panel"><button class="close" @click="data.selectedDelivery=null">×</button><p class="eyebrow">EMAIL DELIVERY</p><h2>{{ data.selectedDelivery.task_name }}</h2><div class="detail-facts"><div><small>状态</small><strong>{{ data.selectedDelivery.status }}</strong></div><div><small>收件人</small><strong>{{ data.selectedDelivery.recipient }}</strong></div><div><small>创建时间</small><strong>{{ formatBeijing(data.selectedDelivery.created_at) }}</strong></div><div><small>发送时间</small><strong>{{ formatBeijing(data.selectedDelivery.sent_at) }}</strong></div></div><p v-if="data.selectedDelivery.last_error" class="error">{{ data.selectedDelivery.last_error }}</p><h3>事件明细</h3><article class="evidence" v-for="item in data.selectedDelivery.items" :key="item.id"><div><strong>{{ item.person_name || '事件已删除' }} · {{ item.title || '#'+item.event_id }}</strong><span class="status">{{ item.status }}</span></div><small>{{ eventLabels[item.event_type] || item.event_type }} · {{ item.skip_reason || '正常' }}</small></article><button v-if="user.role==='admin' && data.selectedDelivery.status==='failed'" class="primary" @click="retryDelivery(data.selectedDelivery.id)">重新投递</button></section></aside>
      </section>
    </div>
  `
}

createApp(App).mount('#app')
