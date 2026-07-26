import { describe, expect, it } from 'vitest'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const styles = fs.readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('email notification management UI', () => {
  it('adds a permission-aware push management page', () => {
    expect(source).toContain("['notifications', '推送管理']")
    expect(source).toContain("active === 'notifications'")
    expect(source).toContain("user.role==='admin'")
  })

  it('supports effective config, rules, test sending and delivery retry', () => {
    expect(source).toContain('/notifications/email/config')
    expect(source).toContain('/notifications/email/test')
    expect(source).toContain('/notifications/rules')
    expect(source).toContain('/notifications/deliveries/')
    expect(source).toContain('retryDelivery')
    expect(source).toContain('formatBeijing(delivery.created_at)')
  })

  it('uses explicit task checkboxes instead of a modifier-key multi-select', () => {
    expect(source).toContain('class="task-picker span-two"')
    expect(source).toContain('v-model="ruleForm.task_ids" type="checkbox"')
    expect(source).toContain('@click="selectAllRuleTasks"')
    expect(source).toContain('@click="clearRuleTasks"')
    expect(source).not.toContain('v-model="ruleForm.task_ids" multiple')
    expect(styles).toContain('.task-option-grid')
  })

  it('provides responsive notification layout and status feedback', () => {
    expect(styles).toContain('.notification-form')
    expect(styles).toContain('.config-sources')
    expect(styles).toContain('.delivery-error')
    expect(styles).toContain('@media (max-width: 480px)')
  })
})
