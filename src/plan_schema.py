# -*- coding: utf-8 -*-
"""零依赖 JSON Schema 子集校验器（工程不引入 jsonschema 依赖）。

支持的关键字：``type``（含类型列表）、``enum``、``required``、``properties``、
``additionalProperties``(false)、``items``、``minItems``、``maxItems``、
``minLength``、``pattern``、``minimum``、``maximum``。
**不支持** ``$ref`` / ``anyOf`` / ``allOf`` —— 契约文件里不要用。

用法（命令行自查）：
    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\plan_schema.py <schema.json> <data.json>
退出码 0 = 通过，1 = 有错（逐条打印）。
"""

import json
import re
import sys


def load(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def type_of(value):
    """给错误信息用的人类可读类型名（bool 必须排在 int 前面）。"""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return type(value).__name__


def matches_type(value, name):
    if name == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if name == 'object':
        return isinstance(value, dict)
    if name == 'array':
        return isinstance(value, list)
    if name == 'string':
        return isinstance(value, str)
    if name == 'boolean':
        return isinstance(value, bool)
    if name == 'null':
        return value is None
    raise ValueError('schema 里出现不支持的 type: %r' % name)


def _validate(value, schema, path, errors):
    if not isinstance(schema, dict):
        raise ValueError('%s: schema 节点不是对象' % path)

    names = schema.get('type')
    if names is not None:
        if isinstance(names, str):
            names = [names]
        if not any(matches_type(value, n) for n in names):
            errors.append('%s: 类型应为 %s，实际 %s' % (path, '/'.join(names), type_of(value)))
            return                       # 类型都不对，后续关键字不必再判

    if 'enum' in schema and value not in schema['enum']:
        errors.append('%s: 取值 %r 不在允许集合 %s 内' % (path, value, schema['enum']))

    if isinstance(value, str):
        if 'minLength' in schema and len(value) < schema['minLength']:
            errors.append('%s: 长度 %d 小于下限 %d' % (path, len(value), schema['minLength']))
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append('%s: 值 %r 不匹配 pattern %s' % (path, value, schema['pattern']))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errors.append('%s: 值 %r 小于下限 %r' % (path, value, schema['minimum']))
        if 'maximum' in schema and value > schema['maximum']:
            errors.append('%s: 值 %r 大于上限 %r' % (path, value, schema['maximum']))

    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                errors.append('%s: 缺必填字段 %s' % (path, key))
        props = schema.get('properties', {})
        for key in sorted(value):
            if key in props:
                _validate(value[key], props[key], '%s.%s' % (path, key), errors)
            elif schema.get('additionalProperties', True) is False:
                errors.append('%s: 出现未定义字段 %s' % (path, key))

    if isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            errors.append('%s: 元素数 %d 少于下限 %d' % (path, len(value), schema['minItems']))
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            errors.append('%s: 元素数 %d 超过上限 %d' % (path, len(value), schema['maxItems']))
        if 'items' in schema:
            for i, item in enumerate(value):
                _validate(item, schema['items'], '%s[%d]' % (path, i), errors)


def validate(obj, schema):
    """返回错误字符串列表，空列表表示通过。"""
    errors = []
    _validate(obj, schema, '$', errors)
    return errors


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[-1])
        print('用法: python src/plan_schema.py <schema.json> <data.json>')
        return 2
    schema = load(sys.argv[1])
    data = load(sys.argv[2])
    errors = validate(data, schema)
    for e in errors:
        print(e)
    print('--- %s 对 %s: %d 个错误' % (sys.argv[2], sys.argv[1], len(errors)))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
