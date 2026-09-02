import { useMemo, useState } from 'react'
import { Button, Col, Collapse, Form, Input, InputNumber, Row, Select, Switch, Tag, Tooltip, Typography } from 'antd'
import { LockOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import type { Rule } from 'antd/es/form'
import type { ParamSchema } from '../api/types'

interface Props {
  schema: ParamSchema[]
}

type ParamValues = Record<string, unknown>

/** 条件显示判定：依赖值未设置（首帧/未渲染）时视为通过，避免闪烁隐藏 */
function matchesShowIf(p: ParamSchema, values: ParamValues | undefined): boolean {
  const cond = p.show_if
  if (!cond) return true
  return Object.entries(cond).every(([dep, allow]) => {
    const v = values?.[dep]
    if (v === undefined || v === null || v === '') return true
    return (allow ?? []).map(String).includes(String(v))
  })
}

/** 单个参数控件（挂在 ['params', key] 上） */
function ParamField({ p }: { p: ParamSchema }) {
  const label = p.unit ? `${p.label}（${p.unit}）` : p.label
  const rules: Rule[] = []
  if (p.min !== undefined || p.max !== undefined) {
    rules.push({
      validator: (_rule: Rule, value: number | null | undefined) => {
        if (value === undefined || value === null) return Promise.resolve()
        if (p.min !== undefined && value < p.min) {
          return Promise.reject(new Error(`不能小于 ${p.min}`))
        }
        if (p.max !== undefined && value > p.max) {
          return Promise.reject(new Error(`不能大于 ${p.max}`))
        }
        return Promise.resolve()
      }
    })
  }

  let control
  if (p.type === 'bool') {
    control = <Switch disabled={p.frozen} />
  } else if (p.type === 'select' || p.type === 'categorical') {
    // choices 元素支持 "value|中文标签" 展示格式：value 取 | 前，label 取 | 后（无 | 则 label=value）
    control = (
      <Select
        options={(p.choices ?? []).map((c) => {
          const i = c.indexOf('|')
          return i >= 0 ? { value: c.slice(0, i), label: c.slice(i + 1) } : { value: c, label: c }
        })}
        allowClear
        disabled={p.frozen}
      />
    )
  } else if (p.type === 'str') {
    control = <Input />
  } else {
    control = (
      <InputNumber
        style={{ width: '100%' }}
        min={p.min}
        max={p.max}
        step={p.step ?? (p.type === 'int' ? 1 : undefined)}
        precision={p.type === 'int' ? 0 : undefined}
        disabled={p.frozen}
      />
    )
  }

  return (
    <Col span={6} key={p.key}>
      <Form.Item
        name={['params', p.key]}
        label={
          p.frozen ? (
            <span>
              {label}
              <Tooltip title="已冻结（PARAM_FREEZE 约定）：默认值即推荐值，不进入寻优空间。两轮寻优实证显示该参数不敏感或已调定。">
                <LockOutlined style={{ marginLeft: 4, color: '#faad14' }} />
              </Tooltip>
            </span>
          ) : p.description ? (
            <span>
              {label}
              <Tooltip title={p.description}>
                <QuestionCircleOutlined style={{ marginLeft: 4, color: '#8c8c8c' }} />
              </Tooltip>
            </span>
          ) : (
            label
          )
        }
        initialValue={p.default}
        rules={rules}
        valuePropName={p.type === 'bool' ? 'checked' : 'value'}
      >
        {control}
      </Form.Item>
    </Col>
  )
}

/**
 * 策略参数表单：按 group 折叠分组，按 show_if 条件显示，
 * advanced 参数组内默认收起。挂在每个 ['params', key] 上。
 */
export default function ParamSchemaForm({ schema }: Props) {
  const form = Form.useFormInstance()
  const values = Form.useWatch('params', form) as ParamValues | undefined
  const [advOpen, setAdvOpen] = useState<Record<string, boolean>>({})

  const groups = useMemo(() => {
    const order: string[] = []
    const map = new Map<string, { basic: ParamSchema[]; advanced: ParamSchema[] }>()
    for (const p of schema) {
      if (!matchesShowIf(p, values)) continue
      const g = p.group || '其他'
      if (!map.has(g)) {
        map.set(g, { basic: [], advanced: [] })
        order.push(g)
      }
      const bucket = map.get(g)!
      if (p.advanced) bucket.advanced.push(p)
      else bucket.basic.push(p)
    }
    return order.map((name) => ({ name, ...map.get(name)! }))
  }, [schema, values])

  if (!schema || schema.length === 0) {
    return <Col span={24}>该策略没有参数</Col>
  }

  const sig = schema.map((p) => p.key).join(',')

  return (
    <>
      <Col span={24}>
        <Collapse
          // 切换策略时重建，重置展开状态；同策略内切换（如 t_mode）保留用户展开状态
          key={sig}
          size="small"
          defaultActiveKey={groups.length ? [groups[0].name] : []}
          items={groups.map((g) => {
            const total = g.basic.length + g.advanced.length
            const advShown = !!advOpen[g.name]
            return {
              key: g.name,
              label: (
                <span>
                  <b>{g.name}</b>
                  <Tag style={{ marginLeft: 8 }}>{total} 项</Tag>
                  {g.advanced.length > 0 && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      含 {g.advanced.length} 项高级参数
                    </Typography.Text>
                  )}
                </span>
              ),
              children: (
                <>
                  <Row gutter={16}>
                    {g.basic.map((p) => (
                      <ParamField key={p.key} p={p} />
                    ))}
                  </Row>
                  {g.advanced.length > 0 && (
                    <>
                      {advShown && (
                        <Row gutter={16}>
                          {g.advanced.map((p) => (
                            <ParamField key={p.key} p={p} />
                          ))}
                        </Row>
                      )}
                      <Button
                        type="link"
                        size="small"
                        style={{ paddingLeft: 0 }}
                        onClick={() =>
                          setAdvOpen((prev) => ({ ...prev, [g.name]: !prev[g.name] }))
                        }
                      >
                        {advShown
                          ? `收起 ${g.advanced.length} 项高级参数`
                          : `展开 ${g.advanced.length} 项高级参数（二次微调，默认不动）`}
                      </Button>
                    </>
                  )}
                </>
              )
            }
          })}
        />
      </Col>
      <Col span={24}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          共 {groups.reduce((s, g) => s + g.basic.length + g.advanced.length, 0)} 项参数，
          按「对回测结果的影响维度」分组；分组名后带问号的参数可悬浮查看说明。
        </Typography.Text>
      </Col>
    </>
  )
}
