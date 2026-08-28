import { Col, Form, Input, InputNumber, Select, Switch } from 'antd'
import type { Rule } from 'antd/es/form'
import type { ParamSchema } from '../api/types'

interface Props {
  schema: ParamSchema[]
}

/** 根据策略 param_schema 动态渲染参数表单（挂在 ['params', key] 上） */
export default function ParamSchemaForm({ schema }: Props) {
  if (!schema || schema.length === 0) {
    return <Col span={24}>该策略没有参数</Col>
  }

  return (
    <>
      {schema.map((p) => {
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
          control = <Switch />
        } else if (p.type === 'select' || p.type === 'categorical') {
          control = <Select options={(p.choices ?? []).map((c) => ({ value: c, label: c }))} allowClear />
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
            />
          )
        }

        return (
          <Col span={8} key={p.key}>
            <Form.Item
              name={['params', p.key]}
              label={label}
              initialValue={p.default}
              rules={rules}
              valuePropName={p.type === 'bool' ? 'checked' : 'value'}
            >
              {control}
            </Form.Item>
          </Col>
        )
      })}
    </>
  )
}
