import { describe, expect, it } from 'vitest'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const styles = fs.readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('daily timeline digest management UI', () => {
  it('loads digest configuration, rules, options and runs', () => {
    expect(source).toContain("api('/notifications/digests/config')")
    expect(source).toContain("api('/notifications/digests/rules')")
    expect(source).toContain("api('/notifications/digests/options')")
    expect(source).toContain("api(`/notifications/digests/runs?")
  })

  it('supports configurable people, send time and digest window', () => {
    expect(source).toContain("send_time: '08:30'")
    expect(source).toContain('type="time"')
    expect(source).toContain('昨天自然日')
    expect(source).toContain('发送前最近 N 小时')
    expect(source).toContain('@click="selectAllDigestPersons"')
    expect(source).toContain('@click="clearDigestPersons"')
    expect(source).toContain('每日时间线邮件至少选择一个人物')
  })

  it('provides preview, manual run, history and retry actions', () => {
    expect(source).toContain('/preview`')
    expect(source).toContain('/runs`')
    expect(source).toContain('runDigestRule')
    expect(source).toContain('openDigestRun')
    expect(source).toContain('retryDigestBatch')
    expect(source).toContain('最近日报运行')
  })

  it('has responsive and accessible digest controls', () => {
    expect(source).toContain('aria-label="日报发送时间"')
    expect(source).toContain('aria-live="polite"')
    expect(styles).toContain('.digest-preview-controls')
    expect(styles).toContain('.digest-required-hint')
    expect(styles).toContain('@media (max-width: 480px)')
  })
})
