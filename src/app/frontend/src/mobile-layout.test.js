import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const mainSource = readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const stylesSource = readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('mobile layout contract', () => {
  it('provides an accessible mobile navigation drawer', () => {
    expect(mainSource).toContain('const mobileMenuOpen = ref(false)')
    expect(mainSource).toContain('aria-controls="primary-navigation"')
    expect(mainSource).toContain(':aria-expanded="mobileMenuOpen"')
    expect(mainSource).toContain('class="sidebar-backdrop"')
    expect(mainSource).toContain('aria-label="关闭导航"')
  })

  it('keeps account controls available inside the mobile drawer', () => {
    expect(stylesSource).toMatch(/@media \(max-width: 760px\)[\s\S]*?\.user-card \{ display: grid; \}/)
    expect(mainSource).toContain('{{ user.username }}')
    expect(mainSource).toContain('<button @click="logout">退出</button>')
  })

  it('supports narrow screens, touch targets, safe areas and wide tables', () => {
    expect(stylesSource).toContain('height: 100dvh')
    expect(stylesSource).toContain('env(safe-area-inset-top)')
    expect(stylesSource).toContain('.panel > table { min-width: 680px; }')
    expect(stylesSource).toContain('.search-hero > div { grid-template-columns: 1fr; }')
    expect(stylesSource).toContain('@media (prefers-reduced-motion: reduce)')
  })
})
