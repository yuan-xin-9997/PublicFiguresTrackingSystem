import { describe, expect, it } from 'vitest'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('./main.js', import.meta.url), 'utf8')
const styles = fs.readFileSync(new URL('./styles.css', import.meta.url), 'utf8')

describe('dynamic push management UI', () => {
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
    expect(source).toContain('动态推送至少选择一个人物')
  })

  it('supports a multi-select information source picker where empty means all sources', () => {
    expect(source).toContain("source_ids: []")
    expect(source).toContain('v-model="digestForm.source_ids" type="checkbox"')
    expect(source).toContain('@click="selectAllDigestSources"')
    expect(source).toContain('@click="clearDigestSources"')
    expect(source).toContain('v-model="digestSourceSearch"')
    expect(source).toContain('未选择信息源时匹配全部信息源')
    expect(source).toContain('全部信息源')
    expect(styles).toContain('.source-option-grid')
  })

  it('uses the renamed "动态推送" labels throughout', () => {
    expect(source).toContain('新增动态推送')
    expect(source).toContain('编辑动态推送')
    expect(source).toContain('动态推送</h3>')
    expect(source).toContain('动态推送运行记录')
    expect(source).toContain('最近动态推送运行')
    expect(source).not.toContain('每日时间线邮件')
    expect(source).not.toContain('新增每日时间线邮件')
    expect(source).toContain('aria-label="动态推送发送时间"')
  })

  it('provides preview, manual run, history and retry actions', () => {
    expect(source).toContain('/preview`')
    expect(source).toContain('/runs`')
    expect(source).toContain('runDigestRule')
    expect(source).toContain('openDigestRun')
    expect(source).toContain('retryDigestBatch')
    expect(source).toContain('最近动态推送运行')
  })

  it('has responsive and accessible digest controls', () => {
    expect(source).toContain('aria-live="polite"')
    expect(styles).toContain('.digest-preview-controls')
    expect(styles).toContain('.digest-required-hint')
    expect(styles).toContain('@media (max-width: 480px)')
  })
})
