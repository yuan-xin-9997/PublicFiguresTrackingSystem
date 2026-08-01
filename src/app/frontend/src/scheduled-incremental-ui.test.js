import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const styles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('scheduled incremental notification UI', () => {
  it('loads configuration, rules and run history', () => {
    expect(source).toContain("api('/notifications/incremental/config')")
    expect(source).toContain('api(`/notifications/incremental/runs?')
    expect(source).toContain("delivery_mode: 'immediate'")
    expect(source).toContain('scheduled_incremental')
  })

  it('supports time editing, reset confirmation, preview and run now', () => {
    expect(source).toContain('addRuleSendTime')
    expect(source).toContain('removeRuleSendTime')
    expect(source).toContain('此次修改会把增量起点重置')
    expect(source).toContain('/preview`')
    expect(source).toContain('/run-now`')
    expect(source).toContain('立即汇总')
  })

  it('shows run detail and protects write controls by role', () => {
    expect(source).toContain('selectedIncrementalRun')
    expect(source).toContain('retryIncrementalBatch')
    expect(source).toContain("user.role==='admin' && batch.status==='failed'")
    expect(source).toContain('tabindex="0"')
  })

  it('provides responsive scheduled incremental styling', () => {
    expect(styles).toContain('.send-time-picker')
    expect(styles).toContain('.incremental-preview')
    expect(styles).toContain('.incremental-runs-panel')
    expect(styles).toContain('.incremental-toolbar')
  })
})
