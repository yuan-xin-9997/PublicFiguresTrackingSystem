// This application intentionally uses an inline template to keep the frontend build
// dependency-light, so it needs Vue's build that includes the runtime template compiler.
import { computed, createApp, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue/dist/vue.esm-bundler.js'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { eventLabels, formatBeijing, percent, queryString, statusLabels } from './utils.js'
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
      selectedEvent: null, users: [], allPages: [], config: null, audit: [], search: [], documents: [], mapPeople: []
    })
    const filters = reactive({ q: '', person_id: '', event_type: '', confirmation_status: '', review_status: '' })
    const loginForm = reactive({ username: 'admin', password: '' })
    const personForm = reactive({ name: '', aliases: '', organization: '', title: '', country_region: '', language: 'zh-CN' })
    const editingPersonId = ref(null)
    const sourceForm = reactive({ name: '', type: 'website', entry_url: '', trust_level: 3, schedule_seconds: 3600, person_ids: [], discovery_enabled: true, discovery_max_pages: 12, discovery_max_depth: 1 })
    const editingSourceId = ref(null)
    const documentForm = reactive({ source_id: '', title: '', content_text: '', canonical_url: '', published_at: '' })
    const searchTerm = ref('')
    const mapEl = ref(null)
    const mapPersonId = ref('')
    let leafletMap = null

    const nav = [
      ['dashboard', '总览'], ['timeline', '时间线'], ['persons', '人物'], ['map', '地图'], ['search', '搜索'],
      ['review', '审核中心'], ['sources', '信息源'], ['tasks', '任务中心'], ['users', '用户权限'],
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
          const params = active.value === 'map' ? { page_size: 100, event_type: 'itinerary', person_id: mapPersonId.value } : { page_size: 100, ...filters }
          if (active.value === 'review') params.review_status = 'needs_review'
          const result = await api(`/events?${queryString(params)}`)
          data.events = result.items; data.eventTotal = result.total
        } else if (active.value === 'persons') data.persons = (await api(`/persons?${queryString({ q: filters.q })}`)).items
        else if (active.value === 'sources') { await loadCommon() }
        else if (active.value === 'tasks') {
          data.tasks = (await api('/tasks')).items
          data.runs = (await api('/task-runs?page_size=30')).items
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

    watch(() => [filters.person_id, filters.event_type, filters.confirmation_status, filters.review_status], () => {
      if (['timeline', 'map'].includes(active.value)) loadPage()
    })
    onMounted(checkSession)

    return {
      user, active, loading, error, notice, data, filters, loginForm, personForm, editingPersonId, sourceForm, editingSourceId, documentForm,
      searchTerm, mapEl, mapPersonId, reloadMap, eventLabels, statusLabels, formatBeijing, percent, visibleNav, manualSources,
      login, logout, loadPage, selectPage, openEvent, savePerson, startEditPerson, resetPersonForm, deletePerson, saveSource, startEditSource, resetSourceForm, deleteSource, testSource, addDocument,
      runTask, review, savePermissions, searchNow
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
          <div class="toolbar">
            <input v-model="filters.q" @keyup.enter="loadPage" placeholder="搜索标题、地点或言论" />
            <select v-model="filters.person_id"><option value="">全部人物</option><option v-for="p in data.persons" :value="p.id">{{ p.name }}</option></select>
            <select v-model="filters.event_type"><option value="">全部类型</option><option value="itinerary">行程</option><option value="statement">言论</option><option value="other">其他</option></select>
            <select v-if="active==='timeline'" v-model="filters.confirmation_status"><option value="">全部发生状态</option><option v-for="s in ['rumored','expected','confirmed','ongoing','completed','cancelled','disputed']" :value="s">{{ statusLabels[s] }}</option></select>
            <select v-if="active==='timeline'" v-model="filters.review_status"><option value="">全部审核状态</option><option v-for="s in ['pending','needs_review','approved','rejected']" :value="s">{{ statusLabels[s] }}</option></select>
            <button @click="loadPage">筛选</button>
          </div>
          <p class="result-count">共 {{ data.eventTotal }} 条事件</p>
          <div class="timeline">
            <article v-for="event in data.events" :key="event.id" :class="['timeline-card', event.event_type]" @click="openEvent(event.id)">
              <div class="timeline-date"><strong>{{ formatBeijing(event.start_at).split(' ')[0] }}</strong><span>{{ event.time_precision === 'unknown' ? '时间未知' : '北京时间' }}</span></div>
              <div class="timeline-body"><div class="card-meta"><span :class="['type',event.event_type]">{{ eventLabels[event.event_type] }}</span><span>{{ event.person_name }}</span><span v-if="event.location_name">⌖ {{ event.location_name }}</span></div><h3>{{ event.title }}</h3><p>{{ event.summary }}</p><footer><span class="status">{{ statusLabels[event.confirmation_status] }}</span><span class="status">{{ statusLabels[event.review_status] }}</span><span>可信度 {{ percent(event.confidence) }}</span><span>{{ event.evidence_count }} 条证据</span></footer></div>
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
          <section class="panel"><table><thead><tr><th>任务</th><th>来源</th><th>周期</th><th>上次运行</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="task in data.tasks" :key="task.id"><td><strong>{{ task.name }}</strong></td><td>{{ task.source_name }}</td><td>{{ task.schedule_seconds }} 秒</td><td>{{ formatBeijing(task.last_run_at) }}</td><td>{{ task.last_status || '未运行' }}</td><td><button class="primary small" @click="runTask(task.id)">立即运行</button></td></tr></tbody></table></section>
          <section class="panel"><div class="section-title"><h3>最近运行</h3></div><table><thead><tr><th>任务</th><th>开始时间</th><th>状态</th><th>发现/新增/重复</th><th>事件</th><th>失败</th></tr></thead><tbody><tr v-for="run in data.runs" :key="run.id"><td>{{ run.task_name }}</td><td>{{ formatBeijing(run.started_at) }}</td><td><span class="status">{{ run.status }}</span></td><td>{{ run.discovered_count }}/{{ run.created_count }}/{{ run.duplicate_count }}</td><td>{{ run.event_count }}</td><td>{{ run.failed_count }}</td></tr></tbody></table></section>
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

        <aside v-if="data.selectedEvent" class="detail-overlay" @click.self="data.selectedEvent=null"><section class="detail-panel"><button class="close" @click="data.selectedEvent=null">×</button><div class="card-meta"><span :class="['type',data.selectedEvent.event_type]">{{ eventLabels[data.selectedEvent.event_type] }}</span><span>{{ data.selectedEvent.person_name }}</span></div><h2>{{ data.selectedEvent.title }}</h2><p class="lead">{{ data.selectedEvent.summary }}</p><div class="detail-facts"><div><small>发生时间</small><strong>{{ formatBeijing(data.selectedEvent.start_at) }}</strong></div><div><small>地点</small><strong>{{ data.selectedEvent.location_name || '未提供' }}</strong></div><div><small>确认状态</small><strong>{{ statusLabels[data.selectedEvent.confirmation_status] }}</strong></div><div><small>可信度</small><strong>{{ percent(data.selectedEvent.confidence) }}</strong></div></div><blockquote v-if="data.selectedEvent.quote_text">“{{ data.selectedEvent.quote_text }}”</blockquote><h3>证据链</h3><article class="evidence" v-for="ev in data.selectedEvent.evidence" :key="ev.id"><div><strong>{{ ev.source_name }}</strong><a v-if="ev.canonical_url.startsWith('http')" :href="ev.canonical_url" target="_blank" rel="noreferrer">查看原文 ↗</a></div><p>{{ ev.evidence_text }}</p><small>{{ ev.document_title }} · {{ formatBeijing(ev.published_at || ev.collected_at) }} · 来源等级 {{ ev.trust_level }}/5</small></article><div v-if="user.role==='admin'" class="review-actions"><button class="primary" @click="review(data.selectedEvent.id,'approve')">通过审核</button><button class="danger" @click="review(data.selectedEvent.id,'reject')">驳回</button></div></section></aside>
      </section>
    </div>
  `
}

createApp(App).mount('#app')
