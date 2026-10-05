# -*- coding: utf-8 -*-
"""几何内核封装（geokernel）：本项目与 FreeCAD / Open CASCADE 打交道的唯一入口。

上层模块（算子库 operators.py、重演引擎 replay.py、几何查询工具 geotools.py、
验证 verify_geo.py、附图 make_fig.py）一律经过本模块调用几何内核，不直接用
Part 接口。这样做有三个目的：

  1. 容差、有效性检查、读回复检这类容易写错又必须一致的地方只写一次；
  2. 每次布尔运算都产生一条统一格式的运算记录，逐步累积即成为可追溯的操作日志；
  3. 将来若更换或升级几何内核，只改这一个文件。

自检（读回 data/ 下 10 件工件的毛坯与成品，核对体积、包围盒与布尔包含关系，
结果写入 exp/dataset_selfcheck.json）：

    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\geokernel.py --selfcheck
"""

import glob
import itertools
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, r'D:\FreeCAD 1.1\bin')
sys.path.insert(0, r'D:\FreeCAD 1.1\lib')

import FreeCAD
import Import
import Part

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 工程根
DATA = os.path.join(ROOT, 'data')                                    # 10 个典型工件
OUT = os.path.join(ROOT, 'out')                                      # 中间模型、日志、附图
EXP = os.path.join(ROOT, 'exp')                                      # 实验记录与统计结果

# 工艺尺寸判定阈值（mm），与开题报告的技术指标一致：关键尺寸偏差不得超过它
TOL_MM = 0.1
# 纯数值比较容差（mm）：只用于「两个坐标是否算同一点」这类判断，不用于工艺判定
TOL_NUMERIC = 1e-6

_DOC_SEQ = itertools.count(1)


# ---------------------------------------------------------------- 读 / 写

def read_step(path):
    """读入 STEP，返回其中一个实体（Part.Shape）。

    走 Import.insert 通道，与 write_step 的 exportStep 不是同一条代码路径，
    因此「写出再读回」构成一次独立复检。文件里若不止一个实体就报错，
    免得后续布尔运算拿到意料之外的形状。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    doc = FreeCAD.newDocument('rd_%d' % next(_DOC_SEQ))
    try:
        Import.insert(path, doc.Name)
        shapes = [o.Shape for o in doc.Objects if hasattr(o, 'Shape')]
    finally:
        FreeCAD.closeDocument(doc.Name)
    if len(shapes) != 1:
        raise ValueError('%s: 期望 1 个实体，实际读到 %d 个' % (path, len(shapes)))
    shape = shapes[0]
    if shape.isNull() or not shape.isValid():
        raise ValueError('%s: 实体无效' % path)
    return shape


def write_step(shape, path):
    """写出 STEP 并立刻读回复检，返回读回后的实体。"""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    shape.exportStep(path)
    back = read_step(path)
    dv = abs(back.Volume - shape.Volume)
    if dv > max(1e-6, abs(shape.Volume) * 1e-9):
        raise ValueError('%s: 读回体积不一致 %.6f vs %.6f' % (path, back.Volume, shape.Volume))
    return back


# ---------------------------------------------------------------- 查询

def bbox_tuple(shape):
    """包围盒 (xmin, ymin, zmin, xmax, ymax, zmax)，便于比较与写进日志。"""
    bb = shape.BoundBox
    return (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)


def describe(shape):
    """一个实体的一句话体检：有效性、实体数、面数、体积、包围盒尺寸。"""
    bb = shape.BoundBox
    return {
        'valid': bool(shape.isValid()),
        'solids': len(shape.Solids),
        'faces': len(shape.Faces),
        'volume': shape.Volume,
        'bbox': [round(v, 6) for v in bbox_tuple(shape)],
        'size': [round(bb.XLength, 6), round(bb.YLength, 6), round(bb.ZLength, 6)],
    }


def contains_bbox(outer, inner, tol=TOL_NUMERIC):
    """inner 的包围盒是否完全落在 outer 的包围盒内（必要条件，不是充分条件）。"""
    o, i = bbox_tuple(outer), bbox_tuple(inner)
    return all(i[k] >= o[k] - tol for k in range(3)) and all(i[k] <= o[k] + tol for k in range(3, 6))


def max_bbox_diff(a, b):
    """两个实体包围盒六个面的最大差值（mm），用于判断方位是否一致。"""
    ba, bb_ = bbox_tuple(a), bbox_tuple(b)
    return max(abs(ba[k] - bb_[k]) for k in range(6))


# ---------------------------------------------------------------- 布尔运算

def _record(op, params, before, after, seconds, valid, removed, error=None):
    """一条运算记录：操作、参数、运算前后的体积与包围盒、是否有效、耗时。"""
    return {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'op': op,
        'params': params or {},
        'volume_before': round(before.Volume, 6),
        'volume_after': round(after.Volume, 6),
        'volume_removed': round(removed, 6),
        'bbox_before': [round(v, 6) for v in bbox_tuple(before)],
        'bbox_after': [round(v, 6) for v in bbox_tuple(after)],
        'seconds': round(seconds, 4),
        'valid': bool(valid),
        'error': error,
    }


def cut(work, tool, op='cut', params=None, single=True):
    """布尔差 work - tool，返回 (结果实体, 运算记录)。

    两种用法对结果的要求不同，用 single 区分：

      single=True （重演每一步）：结果必须是单一有效实体。工件被切散，
        说明这一步的去除体构造有问题，应当停下来查，不能带着坏形状往下做。
      single=False（毛坯 - 成品）：待去除材料本来就可能分成几块——两端余料、
        外圆余量、切掉的料头互不相连，只要结果有效即可。
    """
    t0 = time.perf_counter()
    try:
        res = work.cut(tool)
    except Exception as exc:                      # 内核异常也记一笔，便于回溯
        rec = _record(op, params, work, work, time.perf_counter() - t0, False, 0.0, str(exc))
        raise RuntimeError('布尔差失败 %s: %s' % (op, exc)) from exc
    seconds = time.perf_counter() - t0
    removed = work.Volume - res.Volume
    rec = _record(op, params, work, res, seconds, res.isValid(), removed)
    rec['solids'] = len(res.Solids)
    if res.isNull() or not res.isValid():
        rec['valid'] = False
        raise RuntimeError('布尔差结果无效 %s（%s）' % (op, describe(res)))
    if single and len(res.Solids) != 1:
        rec['valid'] = False
        raise RuntimeError('布尔差后实体数 %d ≠ 1（%s），工件被切散' % (len(res.Solids), op))
    return res, rec


def common(a, b):
    """布尔交 a ∩ b：用于判断去除体是否落在成品范围内（过切、重复加工）。"""
    return a.common(b)


def cut_volume(work, tool):
    """只要去除体积，不要结果实体——用于过切 / 重复加工的量值判断。"""
    return work.cut(tool).Volume


# ---------------------------------------------------------------- 操作日志

class OpLogger:
    """把运算记录按 JSONL 追加写入文件，形成可追溯的操作日志。

    一行一条记录，方便用 pandas 直接读，也方便出问题时用文本工具翻。
    日志属于交付物的一部分（开题报告里「保留操作日志等可追溯的辅助文件」）。
    """

    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self._fh = open(path, 'w', encoding='utf-8', newline='\n')

    def write(self, rec):
        self._fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self._fh.flush()

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------- 数据集自检

def _part_folders():
    for name in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, name)
        if os.path.isdir(d):
            yield name, d


def selfcheck(report_path=None):
    """逐件读回毛坯 / 成品，核对体积、包围盒与布尔包含关系，结果写 JSON。"""
    rows, bad = [], []
    for name, folder in _part_folders():
        blanks = sorted(glob.glob(os.path.join(folder, '毛坯_*.step')))
        parts = sorted(glob.glob(os.path.join(folder, '成品_*.step')))
        if not (blanks and parts):
            print('跳过 %s：缺毛坯或成品' % name)
            continue
        blank, part = read_step(blanks[0]), read_step(parts[0])
        rest, rec = cut(blank, part, op='毛坯-成品', params={'工件': name}, single=False)
        removed = blank.Volume - rest.Volume
        inside = contains_bbox(blank, part)
        vol_ok = abs(removed - part.Volume) < 1e-3
        ok = inside and vol_ok and rec['valid']
        rows.append({
            'part': name,
            'blank': describe(blank),
            'finished': describe(part),
            'removed': round(removed, 6),
            'removed_solids': rec['solids'],
            'bbox_inside': inside,
            'volume_conserved': vol_ok,
            'seconds': rec['seconds'],
            'ok': ok,
        })
        if not ok:
            bad.append(name)
        print('%-22s 毛坯 %12.3f  成品 %12.3f  去除 %12.3f (%d 块)  包含=%-5s 守恒=%-5s  %s'
              % (name, blank.Volume, part.Volume, removed, rec['solids'],
                 inside, vol_ok, 'OK' if ok else '不一致 !'))

    print('')
    print('工件数 %d，通过 %d，失败 %d' % (len(rows), len(rows) - len(bad), len(bad)))
    if bad:
        print('未通过: %s' % '、'.join(bad))
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump({'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                       'parts': rows, 'failed': bad}, fh, ensure_ascii=False, indent=2)
        print('结果已写入 %s' % report_path)
    return 1 if bad else 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--selfcheck':
        sys.exit(selfcheck(os.path.join(EXP, 'dataset_selfcheck.json')))
    print(__doc__)
