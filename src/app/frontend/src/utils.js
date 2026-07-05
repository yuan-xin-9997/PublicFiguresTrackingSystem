export const eventLabels = { itinerary: '行程', statement: '言论', other: '其他' }
export const statusLabels = {
  rumored: '存疑', expected: '预计', confirmed: '已确认', ongoing: '进行中',
  completed: '已发生', cancelled: '已取消', disputed: '有争议',
  pending: '待审核', approved: '已通过', rejected: '已驳回', needs_review: '需复核'
}

export function formatBeijing(value) {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).format(date)
}

export function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '—'
}

export function queryString(values) {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) params.set(key, value)
  })
  return params.toString()
}
