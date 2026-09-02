import { DatePicker } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'

const { RangePicker } = DatePicker

/** 回测区间快捷预设：最近一季/半年/一年/两年/今年/去年 */
const RANGE_PRESETS: Array<{ label: string; value: [Dayjs, Dayjs] }> = [
  { label: '最近一季', value: [dayjs().subtract(3, 'month'), dayjs()] },
  { label: '最近半年', value: [dayjs().subtract(6, 'month'), dayjs()] },
  { label: '最近一年', value: [dayjs().subtract(1, 'year'), dayjs()] },
  { label: '最近两年', value: [dayjs().subtract(2, 'year'), dayjs()] },
  { label: '今年', value: [dayjs().startOf('year'), dayjs()] },
  {
    label: '去年',
    value: [
      dayjs().subtract(1, 'year').startOf('year'),
      dayjs().subtract(1, 'year').endOf('year')
    ]
  }
]

/** 回测/对比实验共用的时间范围选择（带快捷预设，宽度占满） */
export default function BacktestRangePicker(props: React.ComponentProps<typeof RangePicker>) {
  return <RangePicker style={{ width: '100%' }} presets={RANGE_PRESETS} {...props} />
}
