import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const mainSource = readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const stylesSource = readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('timeline filter layout contract', () => {
  it('uses an accessible compact multi-location filter bound to the existing array', () => {
    expect(mainSource).toContain('class="location-filter"')
    expect(mainSource).toContain('aria-label="选择地点（可多选）"')
    expect(mainSource).toContain('v-model="filters.location" type="checkbox"')
    expect(mainSource).toContain('@click="filters.location=[]"')
    expect(mainSource).not.toContain('v-model="filters.location" multiple')
  })

  it('removes review workflow controls and exposes admin timeline deletion', () => {
    expect(mainSource).not.toContain("['review', '审核中心']")
    expect(mainSource).not.toContain('全部审核状态')
    expect(mainSource).not.toContain('/review`')
    expect(mainSource).not.toContain('通过审核')
    expect(mainSource).not.toContain('驳回')
    expect(mainSource).toContain('async function deleteEvent(event)')
    expect(mainSource).toContain("await api(`/events/${event.id}`, { method: 'DELETE' })")
    expect(mainSource).toContain('v-if="user.role===\'admin\'" class="delete-link small"')
    expect(mainSource).toContain('删除事件')
  })

  it('keeps collapsed toolbar controls at one shared height', () => {
    expect(stylesSource).toMatch(
      /\.toolbar > input,\s*\.toolbar > select,\s*\.toolbar > button,\s*\.location-filter > summary\s*\{\s*min-height:\s*43px;\s*height:\s*43px;/
    )
    expect(stylesSource).toContain('.toolbar { display: grid;')
    expect(stylesSource).toContain('align-items: start;')
  })

  it('contains responsive two-column, one-column and narrow-panel rules', () => {
    expect(stylesSource).toContain('@media (max-width: 1100px)')
    expect(stylesSource).toContain('.toolbar { grid-template-columns: 1fr 1fr; }')
    expect(stylesSource).toContain('@media (max-width: 760px)')
    expect(stylesSource).toContain('.toolbar { grid-template-columns: minmax(0, 1fr); }')
    expect(stylesSource).toContain('.location-filter-panel { position: static; width: 100%; max-width: none; margin-top: 5px; }')
    expect(stylesSource).toContain('@media (max-width: 480px)')
    expect(stylesSource).toContain('.location-filter-panel { max-height: 220px; }')
  })
})
