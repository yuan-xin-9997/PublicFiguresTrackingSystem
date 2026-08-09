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

  it('supports effective config, test sending and digest delivery', () => {
    expect(source).toContain('/notifications/email/config')
    expect(source).toContain('/notifications/email/test')
    expect(source).toContain('/notifications/digests/rules')
    expect(source).toContain('retryDigestBatch')
  })

  it('no longer renders the retired push-rule, scheduled-incremental and delivery-records modules', () => {
    expect(source).not.toContain('/notifications/rules')
    expect(source).not.toContain('/notifications/incremental')
    expect(source).not.toContain('/notifications/deliveries/')
    expect(source).not.toContain('新增推送规则')
    expect(source).not.toContain('推送规则</h3>')
    expect(source).not.toContain('定时增量运行')
    expect(source).not.toContain('投递记录</h3>')
    expect(source).not.toContain('selectedIncrementalRun')
    expect(source).not.toContain('saveNotificationRule')
    expect(source).not.toContain('ruleForm')
    expect(source).not.toContain('incrementalRunFilters')
    expect(source).not.toContain('openDelivery')
    expect(source).not.toContain('retryDelivery')
    expect(source).not.toContain('deliveryFilters')
  })

  it('provides responsive notification layout and status feedback', () => {
    expect(styles).toContain('.notification-form')
    expect(styles).toContain('.config-sources')
    expect(styles).toContain('.delivery-error')
    expect(styles).toContain('@media (max-width: 480px)')
  })
})
