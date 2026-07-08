import { describe, expect, it } from 'vitest'
import { eventLabels, formatBeijing, percent, queryString } from './utils.js'

describe('frontend utilities', () => {
  it('formats UTC as Beijing time', () => {
    expect(formatBeijing('2026-01-01T00:00:00+00:00')).toContain('08:00')
  })
  it('maps labels and confidence', () => {
    expect(eventLabels.statement).toBe('言论')
    expect(percent(0.734)).toBe('73%')
  })
  it('omits empty query values', () => {
    expect(queryString({ q: '', page: 2, status: 'approved' })).toBe('page=2&status=approved')
  })
  it('repeats array values for multi-select filters', () => {
    expect(queryString({ location: ['北京', '上海'] })).toBe('location=%E5%8C%97%E4%BA%AC&location=%E4%B8%8A%E6%B5%B7')
  })
})
