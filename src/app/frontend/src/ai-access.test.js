import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue/dist/vue.esm-bundler.js'

vi.mock('vue/dist/vue.esm-bundler.js', async () => {
  const vue = await vi.importActual('vue/dist/vue.esm-bundler.js')
  return { ...vue, onMounted: vi.fn(), onBeforeUnmount: vi.fn() }
})
import { AiAccess } from './AiAccess.js'

function setup() {
  const emit = vi.fn()
  vi.stubGlobal('window', { confirm: vi.fn(() => true) })
  const rows = [{ id: 'ai_public', name: 'test', status: 'active', scopes: ['persons:read'] }]
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    let value = url.includes('/access') ? { api: 'https://example.test/api/v1', scopes: [{ scope: 'persons:read' }] } : { items: rows, total: rows.length }
    if (options.method === 'POST') value = { id: 'ai_public', client_secret: 'one-time-test-secret' }
    return { ok: true, json: async () => value }
  }))
  return { vm: AiAccess.setup({}, { emit }), emit }
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('AI credential UI lifecycle', () => {
  it('retains one-time secret across list refresh and clears it on confirmed close', async () => {
    const { vm, emit } = setup()
    await vm.load()
    Object.assign(vm.form, { name: 'test', scopes: ['persons:read'] })
    await vm.create()
    expect(vm.created.value.client_secret).toBe('one-time-test-secret')
    await vm.load()
    expect(vm.created.value.client_secret).toBe('one-time-test-secret')
    await nextTick()
    expect(emit).toHaveBeenCalledWith('secret-open', true)
    window.confirm.mockReturnValueOnce(false)
    vm.closeSecret()
    expect(vm.created.value).not.toBeNull()
    vm.closeSecret()
    expect(vm.created.value).toBeNull()
    expect(fetch.mock.calls.some(([, options]) => options.method === 'POST' && options.headers['X-PFTS-Request'] === '1')).toBe(true)
    expect(vm.sample.value).not.toContain('one-time-test-secret')
  })

  it('copies the actual secret and requires confirmation before revoking', async () => {
    const { vm } = setup()
    const writeText = vi.fn()
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    vm.created.value = { client_secret: 'memory-only-secret' }
    await vm.copySecret()
    expect(writeText).toHaveBeenCalledWith('memory-only-secret')
    expect(vm.copied.value).toBe(true)
    window.confirm.mockReturnValueOnce(false)
    await vm.revoke({ id: 'ai_public', name: 'test' })
    expect(fetch).not.toHaveBeenCalled()
    await vm.revoke({ id: 'ai_public', name: 'test' })
    expect(fetch).toHaveBeenCalledWith('/api/v1/ai/credentials/ai_public', expect.objectContaining({ method: 'DELETE' }))
  })

  it('handles permission changes and failed requests without losing visible state', async () => {
    const { vm } = setup()
    vm.form.scopes = ['persons:read', 'persons:write']
    await vm.load()
    expect(vm.form.scopes).toEqual(['persons:read'])
    fetch.mockResolvedValueOnce({ ok: false, json: async () => ({ error: { message: '会话已失效' } }) })
    await vm.revoke({ id: 'ai_public', name: 'test' })
    expect(vm.error.value).toBe('会话已失效')
    expect(vm.rows.value[0].status).toBe('active')
    expect(vm.busy.value).toBe(false)
  })
})
