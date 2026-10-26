import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue/dist/vue.esm-bundler.js'
import { formatBeijing } from './utils.js'

export const AiAccess = {
  emits: ['secret-open'],
  setup(props, { emit }) {
    const info = ref(null), rows = ref([]), page = ref(1), total = ref(0)
    const busy = ref(false), error = ref(''), created = ref(null), copied = ref(false)
    const secretInput = ref(null)
    const form = reactive({ name: '', expires_in_days: 30, scopes: [] })
    watch(created, value => emit('secret-open', Boolean(value)))
    const sample = computed(() => info.value ? [
      '# 从环境变量读取凭据；推荐使用 src/examples/ai_client.py 自动换取与续取令牌',
      'export PFTS_BASE_URL="' + info.value.api.replace(/\/api\/v1$/, '') + '"',
      'export PFTS_CLIENT_ID="<凭据标识>"',
      'export PFTS_CLIENT_SECRET="<一次性密钥，通过受保护存储注入>"',
      'python src/examples/ai_client.py',
      '# OAuth: POST ' + info.value.token_endpoint,
      '# Basic 客户端认证；grant_type=client_credentials；resource=' + info.value.mcp,
      '# MCP: Authorization: Bearer <客户端自动获取的短期令牌>'
    ].join('\n') : '')
    async function request(path, options = {}) {
      const response = await fetch('/api/v1/ai/' + path, { credentials: 'same-origin', cache: 'no-store', ...options,
        headers: { 'Content-Type': 'application/json', 'X-PFTS-Request': '1' } })
      const body = await response.json()
      if (!response.ok) throw new Error(body.error?.message || '请求失败')
      return body
    }
    async function perform(fn) {
      busy.value = true; error.value = ''
      try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
    }
    async function load() {
      const [settings, result] = await Promise.all([request('access'), request('credentials?page=' + page.value)])
      info.value = settings; rows.value = result.items; total.value = result.total
      form.scopes = form.scopes.filter(s => settings.scopes.some(v => v.scope === s))
    }
    async function create() {
      await perform(async () => {
        // Refresh rights before submission; the server independently validates them again.
        await load()
        created.value = await request('credentials', { method: 'POST', body: JSON.stringify(form) })
        copied.value = false
        form.name = ''
        await load() // The one-time secret remains in the dialog during list refresh.
      })
    }
    function closeSecret() {
      if (!window.confirm('已保存密钥？关闭后无法再次查看；遗失时请撤销旧凭据并重新创建。')) return
      created.value = null; copied.value = false
    }
    async function copySecret() {
      try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(created.value.client_secret)
        else { secretInput.value.select(); if (!document.execCommand('copy')) throw new Error('copy failed') }
        copied.value = true
      }
      catch { error.value = '浏览器不允许自动复制，请选中密钥手工复制。' }
    }
    async function revoke(row) {
      if (!window.confirm('撤销“' + row.name + '”后，使用此凭据及其所有短期令牌的程序将立即无法调用。确定撤销？')) return
      await perform(async () => { await request('credentials/' + row.id, { method: 'DELETE' }); await load() })
    }
    async function go(delta) { page.value += delta; await perform(load) }
    function beforeUnload(event) {
      if (created.value) { event.preventDefault(); event.returnValue = '' }
    }
    onMounted(() => { window.addEventListener('beforeunload', beforeUnload); perform(load) })
    onBeforeUnmount(() => { created.value = null; window.removeEventListener('beforeunload', beforeUnload) })
    return { info, rows, page, total, busy, error, created, copied, secretInput, form, sample, formatBeijing, create, closeSecret, copySecret, revoke, go, load: () => perform(load) }
  },
  template: `
    <section class="panel ai-access">
      <div class="section-title"><h3>AI 接入</h3><button :disabled="busy" @click="load">刷新</button></div>
      <p>仅管理本人的凭据。每次调用均受所属用户当前权限约束，权限降低或停用后立即生效。</p>
      <p v-if="busy" role="status">正在加载…</p><p v-if="error" class="error" role="alert">{{ error }}</p>
      <template v-if="info">
        <form class="form-grid" @submit.prevent="create">
          <label>凭据名称<input v-model="form.name" maxlength="100" required :disabled="busy || !!created" /></label>
          <label>有效期（天，最多 365 天）<input v-model.number="form.expires_in_days" type="number" min="1" max="365" required /></label>
          <fieldset class="ai-scopes"><legend>权限范围（默认不授予，请选择必要权限）</legend>
            <label v-for="s in info.scopes" :key="s.scope"><input type="checkbox" v-model="form.scopes" :value="s.scope" />{{ s.description }} <small>{{ s.scope }}</small></label>
          </fieldset>
          <button class="primary" :disabled="busy || !!created || !form.scopes.length">创建凭据</button>
        </form>
        <p v-if="!info.scopes.length" class="empty">当前没有可授予的业务权限，请联系管理员。</p>
        <div class="ai-table"><table><thead><tr><th>名称 / 标识</th><th>权限范围</th><th>创建时间（北京）</th><th>过期时间（北京）</th><th>状态</th><th>操作</th></tr></thead>
          <tbody><tr v-for="row in rows" :key="row.id"><td>{{ row.name }}<br/><code>{{ row.id }}</code></td><td>{{ row.scopes.join(', ') }}</td><td>{{ formatBeijing(row.created_at) }}</td><td>{{ formatBeijing(row.expires_at) }}</td><td>{{ {active:'有效',expired:'已过期',revoked:'已撤销'}[row.status] }}</td><td><button :disabled="busy || row.status==='revoked'" @click="revoke(row)">撤销</button></td></tr></tbody>
        </table></div>
        <p v-if="!rows.length && !busy" class="empty">暂无凭据。创建后密钥只显示一次。</p>
        <div class="pagination"><button :disabled="busy || page===1" @click="go(-1)">上一页</button><span>第 {{ page }} 页，共 {{ total }} 条</span><button :disabled="busy || page*20>=total" @click="go(1)">下一页</button></div>
        <h3>当前环境地址与接入示例</h3>
        <dl><dt>API</dt><dd><code>{{ info.api }}</code></dd><dt>OpenAPI</dt><dd><a :href="info.openapi" target="_blank" rel="noopener">{{ info.openapi }}</a> · <a :href="info.docs" target="_blank" rel="noopener">交互文档</a></dd><dt>MCP（Streamable HTTP）</dt><dd><code>{{ info.mcp }}/</code></dd></dl>
        <p>{{ info.clients }}</p><p>令牌最长 {{ info.token_seconds }} 秒，客户端自动续取。API 与 MCP 分别换取对应 resource 的令牌。请使用 HTTPS 或受保护内网。</p>
        <pre>{{ sample }}</pre>
      </template>
      <div v-if="created" class="modal-backdrop ai-secret-backdrop"><section class="panel ai-secret" role="dialog" aria-modal="true" aria-labelledby="ai-secret-title">
        <h3 id="ai-secret-title">凭据已创建：密钥仅显示一次</h3>
        <p>关闭后无法再次查看。请先保存到受保护的凭据存储；遗失时应撤销旧凭据并重新创建。</p>
        <label>客户端标识<input :value="created.id" readonly /></label>
        <label>一次性密钥<input ref="secretInput" :value="created.client_secret" readonly autocomplete="off" spellcheck="false" /></label>
        <button @click="copySecret">{{ copied ? '已复制密钥' : '复制密钥' }}</button><button @click="closeSecret">我已保存，关闭</button>
      </section></div>
    </section>`
}
