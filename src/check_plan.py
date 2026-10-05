# -*- coding: utf-8 -*-
"""录入校验器：校验 exp/plans/ 下的 process_plan.json（有则含 grade.json），
并产出 W2 汇报要的两张表（加工方法清单、歧义句清单）。

**只校验、不生成**：不解析语义、不判对错（判对错是 W6 判分器对着 grade.json 的事）、
不写 data/、不改 kb/ 与 exp/标注规范.md。

查九项（编号与 exp/标注规范.md、计划文件对应）：
  A schema        plan/grade 合 src/process_plan.schema.json、src/grade.schema.json
  B 原文还原      每条 op 的 no/name/text 按 write_plan.py 的版式逐行还原 == 工艺规程.txt 字节
  B' 指纹         plan_sha1 == 工艺规程.txt 原始字节的 sha1
  C 几何自洽      role=final 的 dia 目标必须在成品实测直径里（阈值 geokernel.TOL_MM）；
                 可查集合 = 圆柱面直径 + 圆边直径（锥面端部，见 probe_dias）
  D 条数/工序号   与文本行数一致、工序号连续
  E 阶段边界      bodies 必须为空；neutral 工序不得有 target/尺寸
  F 统计/待确认   source 分布、pending 清单（参照解析集应 0 条）
  G 判分块一致    grade 与 plan 的工序号集合、kind、graded、期望值一一对应
  H 术语表自检    KEYWORDS 里每个词必须在 kb/术语与省略写法.md 里逐字出现
  I 两张表        method_inventory / method_scan_raw / ambiguity_hits / index.json

用法：
    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\check_plan.py [工件名 ...]   不带参数 = 扫全部
    ... src\\check_plan.py --no-geo     跳过 C（无 FreeCAD 也能跑 A/B/B'/D~I）
    ... src\\check_plan.py --selftest   反向自测：5 个变异体必须都被报出来
退出码：0 = 无 ERROR（可有 WARN）；1 = 有 ERROR。
"""

import glob
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan_schema as ps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
EXP = os.path.join(ROOT, 'exp')
PLANS = os.path.join(EXP, 'plans')
KB = os.path.join(ROOT, 'kb')

PLAN_SCHEMA = os.path.join(ROOT, 'src', 'process_plan.schema.json')
GRADE_SCHEMA = os.path.join(ROOT, 'src', 'grade.schema.json')

# 省略写法的关键词表。作用有两个：① 扫 10 件规程产出「歧义句清单」（W2 汇报 + W8 术语检索）；
# ② 与 kb/术语与省略写法.md 对账 —— 每个词必须在 md 里逐字出现（H 项），
#    md 改了关键词而这里没跟，校验器立刻报错。新增词条要同时改两处。
KEYWORDS = [
    # §一 数值省略
    '见平即可', '见圆即可', '至图样', '按图样', '各部', '留精加工余量', '留磨削余量',
    '留加工余量', '按 GB/T',
    # §二 工序关系
    '倒头', '接上序', '两顶尖', '一夹一顶', '中心架', '跟刀架', '切下', '切断',
    '件连下', '工艺凸台', '垫上铜皮',
    # §三 加工方法与特征
    '钻中心孔', '修研', '车螺纹', '套螺纹', '板牙', '滚齿', '插齿', '拉深', '未注倒角',
    '倒角', '锥度', '切槽', '配磨', '研修', '研配', '研磨', '倒坡口', '去毛刺', '尖角倒钝',
]


# ---------------------------------------------------------------- 基础设施

def tol_mm():
    """判定阈值只从 geokernel.py 取，不在这里另抄一份。"""
    src = io.open(os.path.join(ROOT, 'src', 'geokernel.py'), encoding='utf-8').read()
    m = re.search(r'^TOL_MM\s*=\s*([0-9.]+)', src, re.M)
    if not m:
        raise SystemExit('geokernel.py 里找不到 TOL_MM')
    return float(m.group(1))


def load_geo():
    """几何模块只在需要时导入（无 FreeCAD 时用 --no-geo）。"""
    import geokernel as gk
    import audit_dims as ad
    return gk, ad


def read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def load_json(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def dump_json(path, obj):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write('\n')


def rebuild_line(op):
    """与 src/write_plan.py:29 完全同一条公式（含 name 过长时 ' ' * 负数 == '' 的行为）。"""
    return '%-4s%s%s' % (op['no'], op['name'], ' ' * (8 - 2 * len(op['name'])) + op['text'])


def parse_plan_txt(path):
    """仅供「原始方法扫描」用的粗解析：前 4 列取工序号，其后第一个空格前是工序名称。
    权威校验不走这里（B 项用反向还原，不依赖解析）。"""
    rows = []
    text = io.open(path, encoding='utf-8', newline='').read()
    for i, line in enumerate(text.split('\n')):
        if not line.strip():
            continue
        no = line[:4].strip()
        rest = line[4:]
        m = re.match(r'([^ ]+)', rest)
        name = m.group(1) if m else ''
        rows.append({'line': i + 1, 'no': no, 'name': name,
                     'text': rest[len(name):].lstrip(' ')})
    return rows


class Report(object):
    def __init__(self):
        self.items = []

    def _add(self, level, part, op, check, msg):
        self.items.append({'level': level, 'part': part, 'op': op, 'check': check, 'msg': msg})

    def err(self, part, op, check, msg):
        self._add('ERROR', part, op, check, msg)

    def warn(self, part, op, check, msg):
        self._add('WARN', part, op, check, msg)

    def errors(self):
        return [i for i in self.items if i['level'] == 'ERROR']

    def warnings(self):
        return [i for i in self.items if i['level'] == 'WARN']


# ---------------------------------------------------------------- 逐件校验

def check_plan_obj(part, plan, rep, plan_schema):
    """A / B / B' / D / E / F 六项。part 是目录名（可与 plan['part'] 不同，用来做变异测试）。"""
    for e in ps.validate(plan, plan_schema):
        rep.err(part, '', 'A', e)

    ops = plan.get('ops', [])
    plan_txt = os.path.join(DATA, part, '工艺规程.txt')
    if not os.path.isfile(plan_txt):
        rep.err(part, '', 'B', '找不到 %s' % plan_txt)
        return
    raw = read_bytes(plan_txt)
    if plan.get('plan_sha1') != hashlib.sha1(raw).hexdigest():
        rep.err(part, '', "B'", 'plan_sha1=%s 与 工艺规程.txt 的 sha1 不符 → 文本被改过或指纹抄错'
                % plan.get('plan_sha1'))

    text = raw.decode('utf-8')
    if not text.endswith('\n') or text.endswith('\n\n') or '\r' in text:
        rep.warn(part, '', 'B', '工艺规程.txt 的行尾/结尾不符合版式约定（应为单个 LF 结尾、无 CR）')
    file_lines = text.split('\n')[:-1]
    rebuilt = [rebuild_line(o) for o in ops]
    if len(file_lines) != len(rebuilt):
        rep.err(part, '', 'D', '行数不符：工艺规程.txt %d 行，录入 %d 条工序' % (len(file_lines), len(rebuilt)))
    for i, (a, b) in enumerate(zip(file_lines, rebuilt)):
        if a != b:
            rep.err(part, ops[i]['no'] if i < len(ops) else '?', 'B',
                    '第 %d 行还原不一致\n      文件: %r\n      录入: %r' % (i + 1, a, b))
    if '|' in text:
        rep.warn(part, '', 'B', '工艺规程.txt 里出现竖线｜不会被当作分隔符（内容里的普通字符）')

    nos = [o.get('no') for o in ops]
    if nos != [str(i + 1) for i in range(len(nos))]:
        rep.err(part, '', 'D', '工序号不是 1..n 连续：%s' % nos)

    for o in ops:
        if o.get('bodies'):
            rep.err(part, o.get('no'), 'E', 'bodies 必须为空（W5 前不得先填）：%r' % o['bodies'])
        if o.get('kind') == 'neutral':
            if o.get('target'):
                rep.err(part, o.get('no'), 'E', 'neutral 工序不得有加工部位（target=%r）' % o['target'])
            if o.get('size', {}).get('items'):
                rep.err(part, o.get('no'), 'E', 'neutral 工序不得有目标尺寸（size.items 非空）')
        elif not o.get('size', {}).get('items') and not o.get('not_modeled'):
            rep.warn(part, o.get('no'), 'E', '去除类工序既没有目标尺寸也没有 not_modeled 说明')

    stats = {'total': len(ops), 'removal': 0, 'neutral': 0, 'quasi_removal': 0,
             'pending': [], 'sources': {}}
    for o in ops:
        stats[o['kind']] = stats.get(o['kind'], 0) + 1
        stats['sources'][o['source']] = stats['sources'].get(o['source'], 0) + 1
        if o.get('pending'):
            stats['pending'].append({'no': o['no'], 'reason': o['pending']})
        if o['source'] == 'pending' and not o.get('pending'):
            rep.err(part, o['no'], 'F', 'source=pending 但没有写 pending 说明')
    stats['graded_total'] = stats['removal'] + stats['quasi_removal']
    return stats


def probe_dias(ad, shape):
    """成品上「量得到」的直径集合：圆柱面直径 + 所有圆边的直径。

    只取 cylinders_of 会漏掉锥面的端部——坡口的最大直径、1:20 锥面的小端、
    管螺纹锥面的两端，它们不是圆柱面但在成品上是实实在在的圆（圆边）。
    加进来只会放宽「必要条件」，不会让错值蒙混：值仍须在成品上真的出现。
    """
    out = {d for a, d, _, _ in ad.cylinders_of(shape) if a in ('Z', 'X', 'Y')}
    for e in shape.Edges:
        c = e.Curve
        if isinstance(c, ad.Part.Circle):
            out.add(round(2.0 * c.Radius, 6))
    return out


def check_geometry(part, plan, rep, geo, tol):
    """C：role=final 且 what=dia 的目标必须在成品实测直径里（必要条件；其余项目前不查）。"""
    gk, ad = geo
    files = glob.glob(os.path.join(DATA, part, '成品_*.step'))
    if not files:
        rep.warn(part, '', 'C', '找不到成品 STEP，几何校验跳过（记 unverified）')
        return {'verified': 0, 'unverified': 0, 'exempt': 0}
    try:
        shape = gk.read_step(files[0])
        cyl = ad.cylinders_of(shape)
    except Exception as exc:                      # 「取不到几何」与「几何不符」要分开
        rep.warn(part, '', 'C', '读成品 STEP 失败，几何校验跳过：%s' % exc)
        return {'verified': 0, 'unverified': 0, 'exempt': 0}
    dias = probe_dias(ad, shape)
    cyl_dias = sorted({d for a, d, _, _ in cyl if a in ('Z', 'X', 'Y')})
    out = {'verified': 0, 'unverified': 0, 'exempt': 0}
    for o in plan['ops']:
        finals = [i for i in o['size']['items'] if i['role'] == 'final']
        if o['not_modeled']:
            out['exempt'] += len(finals)
            continue
        for it in finals:
            if it['what'] != 'dia':
                out['unverified'] += 1            # 长度/键槽宽/倒角不在 cylinders_of 的可查范围内
                continue
            if any(abs(it['value'] - d) <= tol for d in dias):
                out['verified'] += 1
            else:
                rep.err(part, o['no'], 'C',
                        '目标直径 φ%s 在成品实测直径（圆柱面 %s，圆边端部 %s）里 %.2fmm 内找不到'
                        % (it['value'], cyl_dias,
                           sorted(d for d in dias if not any(
                               abs(d - c) <= 1e-9 for c in cyl_dias)), tol))
    return out


def check_grade_obj(part, plan, grade, rep, grade_schema):
    """G：判分块与录入的 plan 一一对应（不判对错，只判两份答案自相矛盾）。"""
    if grade is None:
        rep.warn(part, '', 'G', '没有 grade.json（只有参照解析集才有），跳过判分块一致性检查')
        return
    for e in ps.validate(grade, grade_schema):
        rep.err(part, '', 'A', '[grade] ' + e)
    pnos = [o['no'] for o in plan['ops']]
    gnos = [o['no'] for o in grade.get('ops', [])]
    if pnos != gnos:
        rep.err(part, '', 'G', 'grade 的工序号与 plan 不一致：%s vs %s' % (gnos, pnos))
    pmap = {o['no']: o for o in plan['ops']}
    for g in grade.get('ops', []):
        p = pmap.get(g['no'])
        if p is None:
            continue
        if g['graded'] != (p['kind'] != 'neutral'):
            rep.err(part, g['no'], 'G',
                    '入不入分母不一致：grade.graded=%s 但 plan.kind=%s'
                    % (g['graded'], p['kind']))
        if g['kind'] != p['kind']:
            rep.err(part, g['no'], 'G', 'kind 不一致：grade=%s plan=%s' % (g['kind'], p['kind']))
        for t in g['targets']:
            if not any(i['what'] == t['what'] and abs(i['value'] - t['value']) <= 0.1
                       for i in p['size']['items']):
                rep.err(part, g['no'], 'G',
                        'grade 期望 %s=%s 在 plan 的 size.items 里找不到对应值'
                        % (t['what'], t['value']))
        for key in g['ambiguity']:
            if not any(k in key for k in KEYWORDS):
                rep.warn(part, g['no'], 'G', 'ambiguity 里这条不含任何术语表关键词：%r' % key)


def check_keywords_kb(rep):
    """H：关键词表与 kb/术语与省略写法.md 对账。"""
    path = os.path.join(KB, '术语与省略写法.md')
    md = io.open(path, encoding='utf-8').read()
    for kw in KEYWORDS:
        if kw not in md:
            rep.err('(kb)', '', 'H', '关键词 %r 不在 kb/术语与省略写法.md 里逐字出现' % kw)


# ---------------------------------------------------------------- 两张表

def build_tables(parts_all, plans):
    """I：加工方法清单 + 全 10 件原始方法扫描 + 歧义句清单。"""
    by_part_method = {}
    by_method_parts = {}
    for part, plan in plans.items():
        for o in plan['ops']:
            m = o['method']
            by_part_method.setdefault(part, {})
            by_part_method[part][m] = by_part_method[part].get(m, 0) + 1
            by_method_parts.setdefault(m, [])
            if part not in by_method_parts[m]:
                by_method_parts[m].append(part)
    raw_scan = {}
    for part in parts_all:
        path = os.path.join(DATA, part, '工艺规程.txt')
        if os.path.isfile(path):
            raw_scan[part] = [r['name'] for r in parse_plan_txt(path)]
    dump_json(os.path.join(EXP, 'method_inventory.json'), {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'exp/plans/*/process_plan.json',
        'note': ('已录入 process_plan 的工件才算进 by_part_method；未录入的看 raw_scan'
                 '（只按工序名称列粗分，未经 schema 约束，仅供汇报）'),
        'parts_with_plan': sorted(plans),
        'parts_all': parts_all,
        'by_part_method': by_part_method,
        'by_method_parts': by_method_parts,
        'raw_scan_by_part': raw_scan,
        'total_ops_with_plan': sum(len(p['ops']) for p in plans.values()),
    })

    hits = []
    for part in parts_all:
        path = os.path.join(DATA, part, '工艺规程.txt')
        if not os.path.isfile(path):
            continue
        for row in parse_plan_txt(path):
            for kw in KEYWORDS:
                if kw in row['text']:
                    hits.append({'part': part, 'line': row['line'], 'no': row['no'],
                                 'name': row['name'], 'keyword': kw,
                                 'span': _span(row['text'], kw), 'text': row['text']})
    by_kw = {}
    for h in hits:
        by_kw.setdefault(h['keyword'], []).append({'part': h['part'], 'no': h['no'],
                                                   'span': h['span']})
    dump_json(os.path.join(EXP, 'ambiguity_hits.json'), {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'note': ('省略写法的实际出现位置。用于 W2 汇报与 W8 检索增强；关键词表在 '
                 'src/check_plan.py 的 KEYWORDS，与 kb/术语与省略写法.md 对账（H 项）。'),
        'counts': {'hits': len(hits),
                   'parts': len({h['part'] for h in hits}),
                   'by_keyword': {k: len(v) for k, v in sorted(by_kw.items())}},
        'by_keyword': by_kw,
        'hits': hits,
    })
    return hits


def _span(text, kw):
    i = text.find(kw)
    return text[max(0, i - 6):i + len(kw) + 6]


# ---------------------------------------------------------------- 主流程

def part_dirs():
    if not os.path.isdir(PLANS):
        return []
    return sorted(d for d in os.listdir(PLANS)
                  if os.path.isfile(os.path.join(PLANS, d, 'process_plan.json')))


def all_data_parts():
    return sorted(d for d in os.listdir(DATA) if os.path.isdir(os.path.join(DATA, d)))


def run(names, no_geo):
    tol = tol_mm()
    plan_schema = ps.load(PLAN_SCHEMA)
    grade_schema = ps.load(GRADE_SCHEMA)
    geo = None if no_geo else load_geo()
    rep = Report()

    parts = [d for d in part_dirs()]
    if names:
        parts = [d for d in parts if any(n in d for n in names)]
    rows = []
    plans = {}
    for part in parts:
        plan = load_json(os.path.join(PLANS, part, 'process_plan.json'))
        plans[part] = plan
        stats = check_plan_obj(part, plan, rep, plan_schema) or {}
        if plan.get('part') != part:
            rep.warn(part, '', 'A', "plan['part']=%r 与目录名不一致" % plan.get('part'))
        geo_stat = None
        if geo is not None:
            geo_stat = check_geometry(part, plan, rep, geo, tol)
        gpath = os.path.join(PLANS, part, 'grade.json')
        grade = load_json(gpath) if os.path.isfile(gpath) else None
        check_grade_obj(part, plan, grade, rep, grade_schema)
        rows.append({'part': part, 'counts': stats, 'geometry': geo_stat,
                     'has_grade': grade is not None})
    check_keywords_kb(rep)
    build_tables(all_data_parts(), plans)

    dump_json(os.path.join(EXP, 'check_plan.json'), {
        'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'tol_mm': tol,
        'geometry_checked': geo is not None,
        'parts': rows,
        'errors': len(rep.errors()),
        'warnings': len(rep.warnings()),
        'items': rep.items,
    })
    dump_json(os.path.join(EXP, 'plans', 'index.json'), {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'note': '计数由 check_plan.py 算出（plan 文件里不手写数字，避免漂移）。',
        'tol_mm': tol,
        'parts': [{'part': r['part'], 'has_grade': r['has_grade'],
                   'total': r['counts'].get('total', 0),
                   'removal': r['counts'].get('removal', 0),
                   'neutral': r['counts'].get('neutral', 0),
                   'quasi_removal': r['counts'].get('quasi_removal', 0),
                   'graded_total': r['counts'].get('graded_total', 0),
                   'pending': r['counts'].get('pending', []),
                   'sources': r['counts'].get('sources', {}),
                   'plan_sha1': plans[r['part']].get('plan_sha1')} for r in rows],
    })

    for r in rows:
        print('【%s】工序 %d（去除类 %d / 准去除 %d / 非去除 %d，入分母 %d）'
              % (r['part'], r['counts'].get('total', 0), r['counts'].get('removal', 0),
                 r['counts'].get('quasi_removal', 0), r['counts'].get('neutral', 0),
                 r['counts'].get('graded_total', 0)))
        if r['geometry']:
            g = r['geometry']
            print('    几何校验：实测到 %d 项，超出可查范围 %d 项，not_modeled 豁免 %d 项'
                  % (g['verified'], g['unverified'], g['exempt']))
        if r['counts'].get('pending'):
            print('    待确认项：%s' % r['counts']['pending'])
    for it in rep.items:
        print('[%s][%s] %s 工序 %s: %s' % (it['level'], it['check'], it['part'], it['op'], it['msg']))
    print('--- ERROR %d，WARN %d' % (len(rep.errors()), len(rep.warnings())))
    return 1 if rep.errors() else 0


# ---------------------------------------------------------------- 反向自测

def selftest():
    """R16：只会通过、不会失败的检查等于没检查。5 个变异体必须都被报出来。

    只在内存里改，不落盘任何变异文件。变异体 (d) 要几何校验才能抓到，
    因此本自测应当在 FreeCAD Python 下跑（不要加 --no-geo）。
    """
    tol = tol_mm()
    plan_schema = ps.load(PLAN_SCHEMA)
    grade_schema = ps.load(GRADE_SCHEMA)
    try:
        geo = load_geo()
    except Exception as exc:
        geo = None
        print('[SKIP] 没有几何模块（%s），变异体 (d) 无法检验' % exc)
    cases = []

    def case(name, part, mutate, expect_check, need_geo=False):
        if need_geo and geo is None:
            cases.append((name, expect_check, {'SKIPPED'}, 0))
            return
        plan = load_json(os.path.join(PLANS, part, 'process_plan.json'))
        grade = load_json(os.path.join(PLANS, part, 'grade.json'))
        mutate(plan, grade)
        rep = Report()
        check_plan_obj(part, plan, rep, plan_schema)
        check_grade_obj(part, plan, grade, rep, grade_schema)
        if geo is not None:
            check_geometry(part, plan, rep, geo, tol)
        got = {i['check'] for i in rep.errors()}
        cases.append((name, expect_check, got, len(rep.errors())))

    def m_text(p, g):
        p['ops'][2]['text'] = p['ops'][2]['text'].replace('。', '', 1)

    def m_drop(p, g):
        del p['ops'][3]

    def m_kind(p, g):
        p['ops'][3]['kind'] = 'neutral'

    def m_dia(p, g):
        # 改 plan 里某条 role=final 的直径，并把 grade 里对应的期望值一起改，
        # 好让「几何不自洽」成为唯一被触发的检查（否则 G 项也会报）
        it = [x for x in p['ops'][4]['size']['items']
              if x['what'] == 'dia' and x['role'] == 'final'][0]
        old = it['value']
        it['value'] = old + 0.2
        for t in g['ops'][4]['targets']:
            if t['what'] == 'dia' and abs(t['value'] - old) < 1e-9:
                t['value'] = old + 0.2
                break

    def m_sha(p, g):
        p['plan_sha1'] = 'f' * 40

    case('(a) 工序内容少一个标点', '输出轴_ShuChuZhou', m_text, 'B')
    case('(b) 删掉一条工序', '输出轴_ShuChuZhou', m_drop, 'D')
    case('(c) 铜套工序 4 的 kind 改成 neutral', '铜套_TongTao', m_kind, 'E')
    case('(d) 某终值直径改 +0.2mm', '输出轴_ShuChuZhou', m_dia, 'C', need_geo=True)
    case("(e) plan_sha1 改错", '输出轴_ShuChuZhou', m_sha, "B'")

    bad = 0
    for name, expect, got, n in cases:
        ok = expect in got
        if not ok:
            bad += 1
        print('%s %s  期望 %s，实际 %s（%d 个 ERROR）'
              % ('[OK]  ' if ok else '[FAIL]', name, expect, sorted(got) or '无', n))
    print('--- 反向自测 %d/%d 命中' % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


def main():
    argv = sys.argv[1:]
    no_geo = '--no-geo' in argv
    if '--selftest' in argv:
        return selftest()
    names = [a for a in argv if not a.startswith('--')]
    return run(names, no_geo)


if __name__ == '__main__':
    sys.exit(main())
