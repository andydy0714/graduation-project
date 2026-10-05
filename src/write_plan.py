"""把工序表按统一版式写成纯文本 工艺规程.txt。

版式（与已完成的柱塞/输出轴一致）：工序号左对齐占 4 列，工序名称后补空格到 8 列，
再跟工序内容；只要这三列，不要表格线，也不要工艺装备列。文件为 UTF-8、LF、无 BOM。

用法（stdin 每行「工序号|工序名称|工序内容」，工序内容里的竖线不参与分列）：
    "D:\\FreeCAD 1.1\\bin\\python.exe" write_plan.py <工件目录名> <<'EOF'
    1|下料|棒料 φ80mm × 760mm
    ...
    EOF
"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 工程根
DATA = os.path.join(ROOT, 'data')


def main():
    folder = sys.argv[1]
    lines = []
    for raw in io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8'):
        raw = raw.rstrip('\n').rstrip('\r')
        if not raw.strip():
            continue
        no, name, content = raw.split('|', 2)
        lines.append('%-4s%s%s' % (no, name, ' ' * (8 - 2 * len(name)) + content))
    path = os.path.join(DATA, folder, '工艺规程.txt')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    print('%s  %d 行' % (path, len(lines)))


main()
