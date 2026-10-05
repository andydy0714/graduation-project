"""独立验收：用 FreeCAD 的 Import 模块（不是 Part.Shape().read()）把交付的 STEP 读进来，
再核对体积、包围盒、有效性，以及「成品能否被毛坯完全包含」。

    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\verify_steps.py

逐一扫 data/ 下每个工件文件夹，逐个单一实体地读回 毛坯/成品，最后打印「验收通过」。
"""

import glob
import os
import sys

import FreeCAD
import Import
import Mesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 工程根
DATA = os.path.join(ROOT, 'data')


def load(path):
    doc = FreeCAD.newDocument(os.path.basename(path))
    Import.insert(path, doc.Name)
    objs = [o for o in doc.Objects if hasattr(o, 'Shape')]
    shapes = [o.Shape for o in objs]
    FreeCAD.closeDocument(doc.Name)
    return shapes


def main():
    bad = []
    for folder in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, folder)
        if not os.path.isdir(d) or folder.startswith('_'):
            continue
        steps = sorted(glob.glob(os.path.join(d, '*.step')))
        if not steps:
            continue
        print('=== %s ===' % folder)
        got = {}
        for p in steps:
            shp = load(p)
            tag = os.path.basename(p).split('_')[0]
            print('  %-4s 对象数=%d' % (tag, len(shp)))
            if len(shp) != 1:
                bad.append('%s %s: 对象数 %d' % (folder, tag, len(shp)))
                continue
            s = shp[0]
            bb = s.BoundBox
            print('       valid=%-5s solids=%d faces=%-3d vol=%.3f  bbox %.2f x %.2f x %.2f'
                  % (s.isValid(), len(s.Solids), len(s.Faces), s.Volume,
                     bb.XLength, bb.YLength, bb.ZLength))
            if s.isNull() or not s.isValid() or len(s.Solids) != 1:
                bad.append('%s %s: 无效实体' % (folder, tag))
            got[tag] = s
        if '毛坯' in got and '成品' in got:
            a, b = got['毛坯'], got['成品']
            ba, bbb = a.BoundBox, b.BoundBox
            inside = (bbb.XMin >= ba.XMin - 1e-6 and bbb.XMax <= ba.XMax + 1e-6
                      and bbb.YMin >= ba.YMin - 1e-6 and bbb.YMax <= ba.YMax + 1e-6
                      and bbb.ZMin >= ba.ZMin - 1e-6 and bbb.ZMax <= ba.ZMax + 1e-6)
            rest = a.cut(b)
            removed = a.Volume - rest.Volume
            ok = inside and abs(removed - b.Volume) < 1e-3
            print('  成品在毛坯内=%s  去除=%.3f  成品体积=%.3f  差=%.6f  %s'
                  % (inside, removed, b.Volume, removed - b.Volume,
                     'OK' if ok else '不一致 !'))
            if not ok:
                bad.append('%s: 布尔包含关系不成立' % folder)
    print('')
    print('验收通过' if not bad else '有问题:\n  ' + '\n  '.join(bad))
    return 1 if bad else 0


sys.exit(main())
