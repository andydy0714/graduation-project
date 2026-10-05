# -*- coding: utf-8 -*-
"""数据集一致性核查：STEP 模型实测尺寸 ↔ 工艺规程文本里写到的数值。

做法：把每件工件 成品/毛坯 STEP 上的圆柱面直径、端面与台肩的轴向位置读出来，
与工艺规程.txt 中出现的 φ 值、长度值对照，列出两类差异：

  A. 规程里提到、成品上没有的直径 —— 一般是粗加工的中间尺寸（成品尺寸 + 余量），
     也可能是抄写错误；
  B. 成品上存在、规程里从未提到的直径 —— 需要解释，可能是漏写的加工内容。

差异不等于错误，只是把需要人过目的地方挑出来。用法：

    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\audit_dims.py [工件名 ...]
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geokernel as gk

import Part

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

DATA = gk.DATA


def cylinders_of(shape):
    """圆柱面清单 [(轴向, 直径, 起, 止)]；轴向 Z 表示与 Z 轴平行（外圆 / 内孔）。"""
    out = []
    for f in shape.Faces:
        s = f.Surface
        if not isinstance(s, Part.Cylinder):
            continue
        bb = f.BoundBox
        dia = round(2.0 * s.Radius, 3)
        ax = s.Axis
        if abs(abs(ax.z) - 1.0) < 1e-6:
            out.append(('Z', dia, round(bb.ZMin, 2), round(bb.ZMax, 2)))
        elif abs(abs(ax.x) - 1.0) < 1e-6:
            out.append(('X', dia, round(bb.XMin, 2), round(bb.XMax, 2)))
        elif abs(abs(ax.y) - 1.0) < 1e-6:
            out.append(('Y', dia, round(bb.YMin, 2), round(bb.YMax, 2)))
        else:
            out.append(('?', dia, None, None))
    return sorted(out, key=lambda t: (t[0], -t[1]))


def planes_of(shape):
    """与 Z 垂直的平面位置 [(z, 面积)]，按面积从大到小——都是端面与台肩。"""
    out = set()
    for f in shape.Faces:
        s = f.Surface
        if isinstance(s, Part.Plane) and abs(abs(s.Axis.z) - 1.0) < 1e-6:
            out.add((round(f.BoundBox.ZMin, 2), round(f.Area, 1)))
    return sorted(out, key=lambda t: -t[1])


def numbers_in_plan(path):
    """从工艺规程.txt 粗略提取：φ 值、带 mm 的数值、螺纹规格。"""
    text = open(path, encoding='utf-8').read()
    diams = sorted({float(m) for m in re.findall(r'[φΦ]\s*(\d+(?:\.\d+)?)', text)})
    lengths = sorted({float(m) for m in re.findall(r'(\d+(?:\.\d+)?)\s*mm', text)})
    threads = sorted({m for m in re.findall(r'M\s*(\d+(?:\.\d+)?)', text)})
    return diams, lengths, threads


def audit(name, folder):
    plan = os.path.join(folder, '工艺规程.txt')
    if not os.path.isfile(plan):
        print('【%s】缺 工艺规程.txt，跳过' % name)
        return
    diams_txt, lens_txt, threads = numbers_in_plan(plan)
    files = sorted(glob.glob(os.path.join(folder, '成品_*.step')))
    if not files:
        print('【%s】缺成品模型，跳过' % name)
        return

    shape = gk.read_step(files[0])
    cyl = cylinders_of(shape)
    dia_model = sorted({d for a, d, _, _ in cyl if a == 'Z'})
    radial = [(a, d, z0, z1) for a, d, z0, z1 in cyl if a in ('X', 'Y')]

    print('=' * 84)
    print('【%s】' % name)
    print('  规程 φ 值 : %s' % (diams_txt or '无'))
    print('  规程 mm 值: %s' % (lens_txt or '无'))
    if threads:
        print('  规程螺纹 : %s' % ', '.join('M' + t for t in threads))
    print('  成品轴向直径: %s' % dia_model)
    if radial:
        print('  成品径向孔 : %s' % [(a, d) for a, d, _, _ in radial])
    print('  成品端面/台肩 z: %s' % [z for z, _ in planes_of(shape)][:16])
    miss_txt = [d for d in diams_txt
                if not any(abs(d - m) < 0.02 for m in dia_model)
                and not any(abs(d - d2) < 0.02 for _, d2, _, _ in radial)]
    miss_model = [d for d in dia_model if not any(abs(d - t) < 0.02 for t in diams_txt)]
    print('  A 规程提到、成品上没有的直径（多为粗加工中间尺寸）: %s' % (miss_txt or '无'))
    print('  B 成品上有、规程从未提到的直径: %s' % (miss_model or '无'))
    print('  成品圆柱面明细:')
    for a, d, z0, z1 in cyl:
        print('      %s  φ%-9s %s ~ %s' % (a, d, z0, z1))


def main():
    names = sys.argv[1:]
    for folder_name in sorted(os.listdir(DATA)):
        folder = os.path.join(DATA, folder_name)
        if not os.path.isdir(folder):
            continue
        if names and not any(n in folder_name for n in names):
            continue
        audit(folder_name, folder)


if __name__ == '__main__':
    main()
