import { describe, expect, it } from 'vitest'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8')

describe('task center maintenance controls', () => {
  it('keeps maintenance controls admin-only and requires confirmation', () => {
    expect(source).toContain('v-if="user.role===\'admin\'" class="panel maintenance-panel"')
    expect(source).toContain("window.confirm(`即将执行")
    expect(source).toContain("if (!dryRun")
  })

  it('provides both dry-run maintenance endpoints and scope filters', () => {
    expect(source).toContain('/maintenance/recheck-event-attribution')
    expect(source).toContain('/maintenance/cleanup-chinadaily-content')
    expect(source).toContain('maintenance.attribution.person_id')
    expect(source).toContain('maintenance.attribution.source_id')
    expect(source).toContain('maintenance.chinadaily.source_id')
    expect(source).toContain('预览影响')
  })

  it('preserves existing Beijing time and timeline helpers', () => {
    expect(source).toContain('formatBeijing')
    expect(source).toContain('locationFilterLabel')
    expect(source).toContain('timeline-toolbar')
  })
})
