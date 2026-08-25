/** 小数 → 百分比字符串：0.234 → "23.40%" */
export function fmtPct(v?: number | null, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return `${(v * 100).toFixed(digits)}%`
}

/** 金额千分位：234000 → "234,000.00" */
export function fmtMoney(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 整数千分位：6600000 → "6,600,000" */
export function fmtInt(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return Math.round(v).toLocaleString('zh-CN')
}

/** 普通数字 */
export function fmtNum(v?: number | null, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '-'
  return v.toFixed(digits)
}

/** 红涨绿跌着色（正值红、负值绿） */
export function pnlColor(v?: number | null): string | undefined {
  if (v === null || v === undefined || Number.isNaN(v)) return undefined
  if (v > 0) return '#cf1322'
  if (v < 0) return '#3f8600'
  return undefined
}
