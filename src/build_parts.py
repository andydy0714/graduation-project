"""按《典型零件机械加工生产实例》(陈宏钧 主编, 第 3 版, 第 4 章) 建各工件的 毛坯 + 成品。

用 FreeCAD 自带的 Python 跑：
    "D:\\FreeCAD 1.1\\bin\\python.exe" src\\build_parts.py [工件名 ...]

不带参数则全部重建。每个工件的输出目录形如 data/<中文名>_<Pinyin>/，内含
毛坯_<Pinyin>.step / 成品_<Pinyin>.step；工艺规程.txt 由文本单独维护，不在此生成。

统一约定（三处）：
  1. 螺纹不建牙型 —— 按大径车成光圆柱，即攻丝/车螺纹之前的形状；
  2. 齿轮做齿坯 —— 只到齿形加工前的外圆/端面，不做齿廓；
  3. 回转体上的局部特征照做 —— 键槽、退刀槽、锥面、均布孔等，只要图样给了尺寸就精确建出。

建模方法：车削件是回转体，把母线（半径, Z）绕 Z 轴旋 360° 即得精确 B-Rep；
倒角/退刀槽/锥面都直接写进母线，不做事后倒角。局部特征（键槽等）用布尔减。
"""

import math
import os
import sys

sys.path.insert(0, r'D:\FreeCAD 1.1\bin')
sys.path.insert(0, r'D:\FreeCAD 1.1\lib')

import FreeCAD
import Part
from FreeCAD import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 工程根（src 的上一级）
DATA = os.path.join(ROOT, 'data')                                    # 工件统一输出到 data/


# ---------------------------------------------------------------- 基本工具

def revolve(profile):
    """母线（(半径, Z) 点列）绕 Z 轴转一圈。"""
    pts = [Vector(r, 0.0, z) for r, z in profile]
    wire = Part.makePolygon(pts)
    face = Part.Face(wire)
    solid = face.revolve(Vector(0, 0, 0), Vector(0, 0, 1), 360.0)
    if hasattr(solid, 'removeSplitter'):
        solid = solid.removeSplitter()
    return solid


def stepped_profile(lands, chamfer=0.0):
    """等直径段串成的母线。

    lands: [(长度, 直径), ...]，自左端面依次相接（倒角吃进端部长度）。
    chamfer: 两端 C 值，0 表示不倒角。
    """
    profile = [(0.0, 0.0)]
    z = 0.0
    for i, (length, dia) in enumerate(lands):
        r = dia / 2.0
        if i == 0 and chamfer > 0:
            profile.append((r - chamfer, z))
            profile.append((r, z + chamfer))
        else:
            profile.append((r, z))
        z += length
        profile.append((r, z))
    if chamfer > 0:
        r_last = lands[-1][1] / 2.0
        z -= chamfer
        profile[-1] = (r_last, z)
        profile.append((r_last - chamfer, z + chamfer))
    profile.append((0.0, sum(l for l, _ in lands)))
    profile.append((0.0, 0.0))
    return profile


def keyway(shape, axis_dia, depth, width, z1, z2):
    """在 +X 侧铣一个圆头平键槽（A 型）。

    槽底是平面，距轴线 axis_dia/2 - depth（即图样标注的 d - t）；
    侧壁距轴线 ±width/2，两端 R = width/2 圆头。
    """
    R = axis_dia / 2.0
    x_bot = R - depth
    x_out = R + 5.0
    r_end = width / 2.0
    box = Part.makeBox(x_out - x_bot, width, (z2 - r_end) - (z1 + r_end),
                       Vector(x_bot, -r_end, z1 + r_end))
    cyl_a = Part.makeCylinder(r_end, x_out - x_bot,
                              Vector(x_bot, 0.0, z1 + r_end), Vector(1, 0, 0))
    cyl_b = Part.makeCylinder(r_end, x_out - x_bot,
                              Vector(x_bot, 0.0, z2 - r_end), Vector(1, 0, 0))
    cutter = box.fuse(cyl_a).fuse(cyl_b)
    cutter = cutter.removeSplitter()
    return shape.cut(cutter)


def _shift(profile, dz):
    """母线整体沿 Z 平移 dz。"""
    return [(r, z + dz) for r, z in profile]


def bore(shape, lands, z0=0.0):
    """从 z0 起，按 lands（(长度, 直径)）挖一个同轴内腔（通孔 / 台阶孔 / 盲孔）。

    注意：母线**必须从轴线出发再回到轴线**。OCC 的 Face.revolve 对不碰轴线的
    环状截面不可靠 —— 会漏掉内表面、给出无效实体。所以内腔一律另做成回转实体再减。
    平移直接写进母线点里，不用 TopoShape.translate()（在 FreeCAD 1.1 上那一步
    对 revolve 出来的实体不生效，体积会算错）。
    """
    cav = revolve(_shift(stepped_profile(lands), z0) if z0
                  else stepped_profile(lands))
    return shape.cut(cav)


def sleeve(outer_lands, bore_dia, chamfer=0.0):
    """带通孔的回转体（套、筒、环）：外圆按 lands，内孔是直径 bore_dia 的通孔。"""
    outer = revolve(stepped_profile(outer_lands, chamfer=chamfer))
    length = sum(l for l, _ in outer_lands)
    return bore(outer, [(length, bore_dia)])


def ring(outer_dia, bore_dia, length, c_out=(0.0, 0.0), c_in=(0.0, 0.0),
         z0=0.0):
    """环 / 套：外圆 outer_dia、内孔 bore_dia、轴向长 length，两端各带 C 值。

    c_out / c_in 都是 (z0 端, z0+length 端)。两端的外轮廓向轴线收、内轮廓背轴线张，
    这样旋转出来才是「带倒角的环」——内轮廓若也写成内收，孔口会反向留出一圈唇边。
    内外轮廓都从轴线出发再回到轴线 —— OCC 的 Face.revolve 只在这种情形下可靠
    （见 bore() 的说明），所以内孔做成独立的带倒角回转实体再布尔减。
    """
    R, r = outer_dia / 2.0, bore_dia / 2.0
    L = length
    z1 = z0 + L

    def outer_profile(c0, c1):
        pts = [(0.0, z0)]
        if c0 > 0:
            pts += [(R - c0, z0), (R, z0 + c0)]
        else:
            pts.append((R, z0))
        if c1 > 0:
            pts += [(R, z1 - c1), (R - c1, z1)]
        else:
            pts.append((R, z1))
        return pts + [(0.0, z1), (0.0, z0)]

    def cavity_profile(c0, c1):
        pts = [(0.0, z0)]
        if c0 > 0:
            pts += [(r + c0, z0), (r, z0 + c0)]
        else:
            pts.append((r, z0))
        if c1 > 0:
            pts += [(r, z1 - c1), (r + c1, z1)]
        else:
            pts.append((r, z1))
        return pts + [(0.0, z1), (0.0, z0)]

    # z 偏移直接写进母线点，不用 TopoShape.translate()：FreeCAD 1.1 上那一步对
    # revolve 出来的实体不生效，内孔会留在原处，毛坯体积凭空多出 πr²|z0|。
    return revolve(outer_profile(c_out[0], c_out[1])).cut(
        revolve(cavity_profile(c_in[0], c_in[1])))


def bolt_circle(shape, count, circle_dia, hole_dia, z0, z1, phase=0.0):
    """在 z0~z1 之间钻 count 个轴向均布通孔，孔心圆直径 circle_dia。"""
    holes = []
    for i in range(count):
        a = phase + 2.0 * math.pi * i / count
        c = Vector(circle_dia / 2.0 * math.cos(a),
                   circle_dia / 2.0 * math.sin(a), z0)
        holes.append(Part.makeCylinder(hole_dia / 2.0, z1 - z0, c, Vector(0, 0, 1)))
    cutter = holes[0]
    for h in holes[1:]:
        cutter = cutter.fuse(h)
    return shape.cut(cutter.removeSplitter())


def radial_hole(shape, dia, z, angle_deg, depth, start_dia):
    """在轴向位置 z 处、沿 angle_deg 方向，从直径 start_dia 的外圆往里钻深 depth 的孔。

    刀具自外圆之外 1mm 处向内进给 depth，向外多出的 1mm 落在材料之外，不影响结果。
    """
    a = math.radians(angle_deg)
    d = Vector(math.cos(a), math.sin(a), 0.0)
    p = d * (start_dia / 2.0 + 1.0) + Vector(0.0, 0.0, z)
    return shape.cut(Part.makeCylinder(dia / 2.0, depth + 1.0, p, -d))


def hex_prism(across_flats, length, z0):
    """正六棱柱（对边宽 across_flats），自 z0 沿 +Z 长 length，与 Z 轴同轴。"""
    R = across_flats / math.sqrt(3.0)          # 外接圆半径
    pts = [Vector(R * math.cos(math.pi / 6 + i * math.pi / 3),
                  R * math.sin(math.pi / 6 + i * math.pi / 3), z0)
           for i in range(6)]
    pts.append(pts[0])
    return Part.Face(Part.makePolygon(pts)).extrude(Vector(0, 0, length))


def spline_grooves(shape, z1, z2, count, d_minor, d_major, groove_width,
                   phase=0.0):
    """矩形齿外花键（GB/T 1144）：在 [z1,z2] 上铣出 count 个齿间槽。

    齿顶圆 d_major、齿根圆 d_minor、槽宽 groove_width（= 图样上的键宽 b，
    矩形花键的键宽与槽宽相等）。每道槽的两侧面是平行平面，槽底是 d_minor 的圆弧。
    做法：把一块长方体从 d_minor 圆柱外面切出来，就是该槽。
    """
    r_out = d_major / 2.0 + 1.0        # 只从 +X 一侧切出去，越过外圆即可
    cutter = None
    for i in range(count):
        a = phase + 2.0 * math.pi * i / count
        # 长方体自轴线沿 +X 伸出，y ∈ [-b/2, b/2]，再挖掉 d_minor 圆柱，
        # 剩下的正好是「齿根圆以外、两条平行侧面之间」的槽
        box = Part.makeBox(r_out, groove_width, z2 - z1,
                           Vector(0.0, -groove_width / 2.0, z1))
        box.rotate(Vector(0, 0, 0), Vector(0, 0, 1), math.degrees(a))
        root = Part.makeCylinder(d_minor / 2.0, z2 - z1,
                                 Vector(0, 0, z1), Vector(0, 0, 1))
        g = box.cut(root)
        cutter = g if cutter is None else cutter.fuse(g)
    return shape.cut(cutter.removeSplitter())


# ---------------------------------------------------------------- 校验 / 输出

def frustum(r0, r1, h):
    """圆台体积 V = π h (r0² + r0 r1 + r1²) / 3。"""
    return math.pi * h * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0


def chamfer_loss(lands, chamfer):
    """两端 C 倒角相对理想圆柱面切掉的体积（左端按 lands[0]，右端按 lands[-1]）。"""
    if chamfer <= 0:
        return 0.0
    loss = 0.0
    for (length, dia), at_left in ((lands[0], True), (lands[-1], False)):
        r = dia / 2.0
        loss += math.pi * r * r * chamfer - frustum(r, r - chamfer, chamfer)
    return loss


def ring_chamfer_loss(outer_dia, bore_dia, c_out=(0.0, 0.0), c_in=(0.0, 0.0)):
    """环两端外圆 / 内孔 45° 倒角切掉的体积（Pappus 定理）。

    外圆倒角是在半径 R 处切掉 c×c 的三角形，形心半径 R - c/3；
    内孔倒角是在半径 r 处切掉 c×c 的三角形，形心半径 r + c/3。
    """
    R, r = outer_dia / 2.0, bore_dia / 2.0
    loss = 0.0
    for c in c_out:
        if c > 0:
            loss += math.pi * c * c * (R - c / 3.0)
    for c in c_in:
        if c > 0:
            loss += math.pi * c * c * (r + c / 3.0)
    return loss


def keyway_volume(axis_dia, depth, width, length):
    """圆头平键槽的去除体积（数值积分，供对照 OCC 布尔差）。

    槽底平面距轴线 R - depth，侧壁 y ∈ [-b/2, b/2]，两端 R = b/2 圆头，
    总长 length 含两个圆头。去掉的料 = 轴表面 sqrt(R²-y²) 与槽底平面之间。
    """
    R = axis_dia / 2.0
    r_end = width / 2.0
    bottom = R - depth
    n = 20000
    dx = width / n
    area = 0.0
    for i in range(n):
        y = -r_end + (i + 0.5) * dx
        h = math.sqrt(max(R * R - y * y, 0.0)) - bottom
        if h <= 0:
            continue
        # 该 y 处槽沿 Z 的长度：中间段 length - width，两端圆头合计 2*sqrt(r_end²-y²)
        z_len = (length - width) + 2.0 * math.sqrt(max(r_end * r_end - y * y, 0.0))
        area += h * z_len * dx
    return area


def profile_volume(profile):
    """母线绕 Z 轴旋转所得的解析体积：相邻两点之间按圆台累加，符号自然抵消。

    对直母线的段是精确的；圆弧段只要采样足够密（数十点）即可到 1e-5 相对精度。
    与 OCC 的实体体积互相对照，可同时验证「母线写对了」和「旋转 / 布尔 / 导出
    读回这条链路没出错」。
    """
    return abs(sum(frustum(r0, r1, z1 - z0)
                   for (r0, z0), (r1, z1) in zip(profile, profile[1:])))


def arc_points(r_c, z_c, radius, t0_deg, t1_deg, n=32):
    """以 (r_c, z_c) 为圆心、半径 radius 的圆弧采样点（极角按度数给）。

    点 = (r_c + radius·cos t, z_c + radius·sin t)，t 从 t0 到 t1。
    """
    out = []
    for i in range(n + 1):
        t = math.radians(t0_deg + (t1_deg - t0_deg) * i / n)
        out.append((r_c + radius * math.cos(t), z_c + radius * math.sin(t)))
    return out


def report(tag, shape):
    bb = shape.BoundBox
    print('%-4s faces=%-3d vol=%-12.3f bbox %.2f x %.2f x %.2f  valid=%s'
          % (tag, len(shape.Faces), shape.Volume,
             bb.XLength, bb.YLength, bb.ZLength, shape.isValid()))
    return bb


def emit(out_dir, pinyin, blank, part, v_theory=None, notes=()):
    out_dir = os.path.join(DATA, out_dir)
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    print('')
    print('=== %s ===' % out_dir)
    for prefix, shp in (('毛坯', blank), ('成品', part)):
        p = os.path.join(out_dir, '%s_%s.step' % (prefix, pinyin))
        shp.exportStep(p)
        paths[prefix] = p

    # 读回校验（磁盘 -> B-Rep 的独立通道）
    back = {}
    print('--- 读回校验 ---')
    for prefix, p in paths.items():
        s = Part.Shape()
        s.read(p)
        back[prefix] = s
        report(prefix, s)
        if s.isNull() or not s.isValid():
            raise SystemExit('校验失败: %s' % prefix)
        print('     写出 %s (%d bytes)' % (p, os.path.getsize(p)))

    a, b = back['毛坯'], back['成品']
    ba, bb_ = a.BoundBox, b.BoundBox
    inside = (bb_.XMin >= ba.XMin - 1e-6 and bb_.XMax <= ba.XMax + 1e-6
              and bb_.YMin >= ba.YMin - 1e-6 and bb_.YMax <= ba.YMax + 1e-6
              and bb_.ZMin >= ba.ZMin - 1e-6 and bb_.ZMax <= ba.ZMax + 1e-6)
    print('成品落在毛坯内: %s' % inside)
    rest = a.cut(b)
    removed = a.Volume - rest.Volume
    print('布尔差 毛坯-成品: 去除 %.3f mm3 (%.2f%%)' % (removed, 100.0 * removed / a.Volume))
    if abs(removed - b.Volume) > max(1e-3, 1e-6 * a.Volume):
        print('注意: 去除量 != 成品体积，成品可能不完全在毛坯内')

    if v_theory is not None:
        # 与「读回后的实体」比 —— 交付出去的就是磁盘上这个 B-Rep
        got = back['成品'].Volume
        print('理论值 成品: %.3f  -> 与读回实体差 %.4f mm3 (%.4f%%)'
              % (v_theory, got - v_theory, 100.0 * abs(got - v_theory) / v_theory))
    for n in notes:
        print('备注: %s' % n)


def make_cylinder_blank(dia, length, part_length):
    """毛坯圆棒：与成品同轴，长度方向上居中。"""
    return Part.makeCylinder(dia / 2.0, length,
                             Vector(0, 0, -(length - part_length) / 2.0))


# ---------------------------------------------------------------- 4.1.1 柱塞

def build_zhusai():
    """图 4-1 / 表 4-1，柱塞，棒料 φ25×142。

    总长 120，主体 φ20(-0.01/-0.02)，距右端面 4~7 处开 φ17×3 退刀槽，
    右端 φ20 轴颈长 4(+0.15)，两端 C2 倒角。
    毛坯按工序 9「线切割切除两端工艺凸台」取：φ25×142 棒料，
    成品 120 居中放入，每端余 11mm 即工艺凸台。
    """
    R_MAIN, R_GROOVE, R_CHAMF = 10.0, 8.5, 8.0
    L_TOTAL, CHAMFER = 120.0, 2.0
    Z_G_L, Z_G_R = L_TOTAL - 7.0, L_TOTAL - 4.0

    profile = [
        (0.0, 0.0), (R_CHAMF, 0.0), (R_MAIN, CHAMFER),
        (R_MAIN, Z_G_L), (R_GROOVE, Z_G_L), (R_GROOVE, Z_G_R), (R_MAIN, Z_G_R),
        (R_MAIN, L_TOTAL - CHAMFER), (R_CHAMF, L_TOTAL), (0.0, L_TOTAL), (0.0, 0.0),
    ]
    part = revolve(profile)
    blank = make_cylinder_blank(25.0, 142.0, L_TOTAL)

    l_cyl_l = Z_G_L - CHAMFER                  # 左段 φ20：Z 2 -> 113
    l_cyl_r = (L_TOTAL - CHAMFER) - Z_G_R      # 右段 φ20：Z 116 -> 118
    v_theory = (frustum(R_CHAMF, R_MAIN, CHAMFER) * 2
                + math.pi * R_MAIN ** 2 * (l_cyl_l + l_cyl_r)
                + math.pi * R_GROOVE ** 2 * (Z_G_R - Z_G_L))

    emit('柱塞_ZhuSai', 'ZhuSai', blank, part, v_theory=v_theory,
         notes=('退刀槽 φ17×3，距右端面 4 ~ 7',))


# ---------------------------------------------------------------- 4.1.2 输出轴

def build_shuchuzhou():
    """图 4-2 / 表 4-2，输出轴，45 钢，棒料 φ90×400。

    轴向尺寸链（自左端面）：85 | 77 | 75 | 10 | 78 | 20 | 35 = 380
    对应直径依次：φ54.4 | φ60 | φ70 | φ88 | φ80 | φ70 | φ60
      - 工序 6「φ54.4(+0.05/0) × 85mm、φ60(+0.024/+0.011) × 77mm」-> 左半两段
      - 工序 5「φ80(+0.021/+0.002) × 78mm、φ60(+0.024/+0.011) × 35mm」-> 右半两段
      - 工序 7「磨削 φ60 两处、φ80 一处」-> 两处 φ60 均在磨，与此吻合
    最右端为 φ60×35（图上「20」在「35」左侧，且 φ60 的尺寸线箭头正落在 35 段
    的两条母线内，φ70 量的是更靠左的 20 段）。
    两端 C2 倒角（未注圆角 R1，端部按常规取 C2）；两端保留中心孔，未建（见备注）。
    两处键槽（工序 10「铣 18(0/-0.043)mm 键槽两处」），宽 18：
      - φ54.4 段：槽底 d-t = 47.5 -> t = 6.9，长 73
      - φ80  段：槽底 d-t = 73   -> t = 7.0，长 67（78 - 67 = 11 = 5.5 x 2，即图注 5.5）
    """
    LANDS = [(85.0, 54.4), (77.0, 60.0), (75.0, 70.0), (10.0, 88.0),
             (78.0, 80.0), (20.0, 70.0), (35.0, 60.0)]
    total = sum(l for l, _ in LANDS)
    assert abs(total - 380.0) < 1e-9, total

    plain = revolve(stepped_profile(LANDS, chamfer=2.0))

    z = 0.0
    zs = []
    for l, _ in LANDS:
        zs.append((z, z + l))
        z += l
    # φ54.4 段键槽：长 73，在 85 段内居中
    a1, b1 = zs[0]
    k1 = (a1 + (85.0 - 73.0) / 2.0, b1 - (85.0 - 73.0) / 2.0)
    # φ80 段键槽：长 67，两端各留 5.5
    a2, b2 = zs[4]
    k2 = (a2 + 5.5, b2 - 5.5)

    part = keyway(plain, 54.4, 6.9, 18.0, *k1)
    part = keyway(part, 80.0, 7.0, 18.0, *k2)

    blank = make_cylinder_blank(90.0, 400.0, total)

    # 逐段解析校核：理想圆柱段 - 两端倒角 - 两处键槽
    v_lands = sum(math.pi * (d / 2.0) ** 2 * l for l, d in LANDS)
    v_theory = (v_lands - chamfer_loss(LANDS, 2.0)
                - keyway_volume(54.4, 6.9, 18.0, 73.0)
                - keyway_volume(80.0, 7.0, 18.0, 67.0))

    emit('输出轴_ShuChuZhou', 'ShuChuZhou', blank, part, v_theory=v_theory,
         notes=(
             '尺寸链 85+77+75+10+78+20+35 = %.0f mm' % total,
             '键槽去除量(解析): φ54.4 段 %.3f，φ80 段 %.3f' % (
                 keyway_volume(54.4, 6.9, 18.0, 73.0),
                 keyway_volume(80.0, 7.0, 18.0, 67.0)),
             '两端中心孔(B2.5)未建：保留在成品上，属 GB/T 145 标准孔，'
             '体积占比 <0.05%，不影响外部轮廓',
         ))


def build_zhiqquan():
    """图 4-48 / 表 4-34，齿圈，ZG310—570，铸件。

    成品是一个薄壁环（齿坯，不做齿形）：
      外圆 φ615(-0.2/0)，内孔 φ550(-0.2/0)，厚 40 ±0.2
      图 4-48 上外圆一端标 C2，其余按技术要求 2「未注倒角 C1」
      φ592.5（齿根圆 = 605 - 2.5×5）、φ605±0.10（分度圆）是参考尺寸，不构成实体
    毛坯：原书只写「铸」，未给铸件毛坯尺寸，故按表 4-34 工序 4/5 的粗车尺寸取
      （粗车内孔至 φ545±1、粗车外圆至 φ620±1、保证厚度 45mm）。
    """
    OUT_D, BORE_D, L = 615.0, 550.0, 40.0
    C_OUT = (2.0, 1.0)          # z=0 端图上标 C2，另一端按未注倒角 C1
    C_IN = (1.0, 1.0)

    part = ring(OUT_D, BORE_D, L, c_out=C_OUT, c_in=C_IN)
    blank = ring(620.0, 545.0, 45.0, z0=-2.5)     # 与成品两端对称留量

    v_theory = (math.pi * ((OUT_D / 2.0) ** 2 - (BORE_D / 2.0) ** 2) * L
                - ring_chamfer_loss(OUT_D, BORE_D, C_OUT, C_IN))

    emit('齿圈_ZhiQuan', 'ZhiQuan', blank, part, v_theory=v_theory,
         notes=(
             'm=5 z=121：齿顶圆 d_a = 5×(121+2) = 615，与图样外圆一致；'
             '齿根圆 592.5、分度圆 605 是参考尺寸',
             '毛坯未在书上给出铸件尺寸，按工序 4/5 粗车后的 φ620/φ545×45 取值',
             '只做齿坯，未做齿廓（约定 2）',
         ))


# ------------------------------------------------------------ 4.1.3 定位销轴

def build_dingweixiaozhou():
    """图 4-3 / 表 4-3，定位销轴，T10A，棒料 φ35×35。

    轴向尺寸链（自 φ20 端面）：
      10 | 2 | 5 | 2 | 11 = 30
      φ20(+0.018/0) 定位轴颈 10（表 4-3 工序 4「保外圆 φ20 总长 10(-0.3/-0.4)」）
      退刀槽 φ18×2      （工序 4）
      φ30 台肩 5(+0.8/+0.6)（工序 5）
      退刀槽 φ16×2      （工序 5）
      φ18(+0.018/0) 小头段 11 —— 其中 φ18 圆柱 6、锥面 φ18→φ15 长 3.5、
      端部 φ15 平头 1.5（工序 5「车小头 φ15mm 处锥度」）

    不确定项：图 4-3 上「10 + 2 + 5 + 2 + 10 = 29」与总长 30 差 1mm，且小头处
    锥面 / φ15 平头的轴向长度图上未直接标注。这里以表 4-3 的工序尺寸为准闭合到
    30mm，小头段按图形的「平头 + 锥面 + 圆柱」三段比例分配（见 notes）。
    """
    R20, R18, R30, R16, R15 = 10.0, 9.0, 15.0, 8.0, 7.5
    L20, LG1, L30, LG2 = 10.0, 2.0, 5.0, 2.0
    L18, L_CONE, L15 = 6.0, 3.5, 1.5
    C = 1.0                                   # 端面倒钝（技术要求 1「尖角倒钝」）

    z = L20
    z_g1, z_30 = z, z + LG1
    z_g2 = z_30 + L30
    z_18 = z_g2 + LG2
    z_cone = z_18 + L18
    z_tip = z_cone + L_CONE
    z_end = z_tip + L15

    profile = [
        (0.0, 0.0), (R20 - C, 0.0), (R20, C),
        (R20, z_g1), (R18, z_g1), (R18, z_30), (R30, z_30),
        (R30, z_g2), (R16, z_g2), (R16, z_18), (R18, z_18),
        (R18, z_cone), (R15, z_tip), (R15, z_end),
        (0.0, z_end), (0.0, 0.0),
    ]
    part = revolve(profile)

    v_theory = (math.pi * (R20 ** 2 * L20 + R18 ** 2 * LG1 + R30 ** 2 * L30
                           + R16 ** 2 * LG2 + R18 ** 2 * L18)
                + frustum(R18, R15, L_CONE)
                + math.pi * R15 ** 2 * L15
                - (math.pi * R20 ** 2 * C - frustum(R20 - C, R20, C)))

    blank = make_cylinder_blank(35.0, 35.0, z_end)
    emit('定位销轴_DingWeiXiaoZhou', 'DingWeiXiaoZhou', blank, part,
         v_theory=v_theory, notes=(
             '尺寸链 10+2+5+2+11 = %.0f mm（φ20 端面起算）' % z_end,
             '小头段 11 拆为 φ18 圆柱 6 + 锥面 3.5 + φ15 平头 1.5：图上只标了 φ15 与'
             '锥面形状，未标轴向长度，按图形比例分配',
             '图 4-3 上「10+2+5+2+10 = 29」与总长 30 差 1mm，以表 4-3 工序尺寸为准',
             '毛坯按工序 1：棒料 φ35×35（成品 30 居中，两端各余 2.5）',
         ))


# -------------------------------------------------------------- 4.1.4 活塞杆

def build_huosaiGan():
    """图 4-5 / 表 4-4，活塞杆，38CrMoAlA，自由锻 φ62×1150。

    轴向尺寸链（自左端面）：
      100 | 7 | 10 | 68 | 10 | 770 | 60 | 5 | 60 = 1090
      左端 M39×2-6g 螺纹 100（C2 端倒角，约定 1 按大径 φ39 车成光圆柱）
      切槽 φ36 × 7        （工序 17：左端切槽 7mm×φ36mm）
      30° 锥面 10：φ36 涨到六方的对角半径（41/√3 = 23.672，即图上 47.3/2）
      六方 41×41（对角 47.3）长 68
      锥面 10：六方对角半径收/涨到 φ50（工序 12「六方与 φ50 连接的锥度」）
      φ50(-0.025/0) × 770（基准 A，氮化 0.2~0.3）
      1:20 锥面 60：φ50 收到 φ47（端面变化 3mm / 60mm = 1:20，供精磨对研）
      φ36 台阶 5（工序 16：切槽 5mm×φ36mm，锥面根部靠肩）
      右端 M39×2-6g 螺纹 60（C2 端倒角）

    不确定项：图 4-5 上「30°」是 φ36→六方那段锥面的半角，按对角半径反算得
    29.54°（≈30°）；六方段长 68 是尺寸链闭合后的值（图上只标了六方的 41/47.3，
    没标轴向长度），与图上「六方段约为螺纹段长的 2/3」的比例一致。
    """
    R_TH, R_G, R50, R_TAP, R_HEX = 19.5, 18.0, 25.0, 23.5, 41.0 / math.sqrt(3.0)
    L_TH_L, L_G_L, L_CONE, L_HEX, L_CONE2 = 100.0, 7.0, 10.0, 68.0, 10.0
    L50, L_TAP, L_G_R, L_TH_R = 770.0, 60.0, 5.0, 60.0
    C, C_R = 2.0, 1.0          # 工序 17 左端倒角 C2；工序 16 右端倒角 C1

    z = L_TH_L
    z_g1, z_c1 = z, z + L_G_L
    z_hex, z_c2 = z_c1 + L_CONE, z_c1 + L_CONE + L_HEX
    z_50, z_tap = z_c2 + L_CONE2, z_c2 + L_CONE2 + L50
    z_taper = z_tap + L_TAP                        # 1:20 锥面终点，φ50 -> φ47
    z_g2, z_end = z_taper + L_G_R, z_taper + L_G_R + L_TH_R

    profile = [
        (0.0, 0.0), (R_TH - C, 0.0), (R_TH, C), (R_TH, z_g1),
        (R_G, z_g1), (R_G, z_c1), (R_HEX, z_hex), (R_HEX, z_c2),
        (R50, z_50), (R50, z_tap), (R_TAP, z_taper), (R_G, z_taper),
        (R_G, z_g2), (R_TH, z_g2), (R_TH, z_end - C_R), (R_TH - C_R, z_end),
        (0.0, z_end), (0.0, 0.0),
    ]
    # 六方段：母线里先按外接圆柱 φ(2·R_HEX) 回转，再把圆柱内、六方外的料切掉。
    # 这样母线保持回转体、体积可以解析核对，而实体是真的六棱柱。
    part = revolve(profile)
    part = part.cut(Part.makeCylinder(R_HEX, L_HEX, Vector(0, 0, z_hex))
                    .cut(hex_prism(41.0, L_HEX, z_hex)))

    v_hex_cyl = math.pi * R_HEX ** 2 * L_HEX
    v_hex_real = math.sqrt(3.0) / 2.0 * 41.0 ** 2 * L_HEX
    v_theory = profile_volume(profile) - (v_hex_cyl - v_hex_real)
    blank = make_cylinder_blank(62.0, 1150.0, z_end)
    emit('活塞杆_HuoSaiGan', 'HuoSaiGan', blank, part, v_theory=v_theory,
         notes=(
             '尺寸链 100+7+10+68+10+770+60+5+60 = %.0f mm' % z_end,
             '30° 锥面按六方对角半径 φ47.34（41/√3）反算为 29.54°；'
             '六方段长 68 由尺寸链闭合得到',
             '1:20 锥面：φ50 收到 φ47，60mm 内端面变化 3mm，正好 1:20',
             '倒角按工序 16/17：右端 C1、左端 C2',
             '渗氮深度取工序 21 的 0.25~0.35mm（图 4-5 标注为 0.2~0.3mm，两者不一致）',
             '螺纹按大径车成光圆柱（约定 1）：M39×2-6g -> φ39',
             '毛坯按工序 2：自由锻 φ62×1150（成品 1090 居中，两端各余 30）',
         ))


# -------------------------------------------------------------- 4.1.7 调整偏心轴

def build_tiaoZhengPianXinZhou():
    """图 4-11 / 表 4-7，调整偏心轴，45 钢，六方钢 14mm×380mm（10 件连下）。

    轴向尺寸链（自偏心端端面）：
      17 | 0.5 | 5 | 2 | 8.5 = 33
      偏心轴颈 φ8(-0.03/-0.06) 长 17(+0.1/-0.3)，轴线相对螺纹 M8 基准轴线偏心 2mm，
        端面钻 M4 螺纹底孔 φ3.3 深 12（攻 M4 深 8 —— 图 4-11 上的「8」即此攻深）
      清根 φ12mm × 0.5mm（表 4-7 工序 4 原文；图 4-11「清根」引线指向六方左端台阶，
        旁边「0.5」即其宽度），即六方左端一个深 1mm 的让位台阶
      六方 14（对边）段长 5（图 4-11 的「5」量的是这段；六方是棒料的原始形状，
        故工序 3、4 都不车它）
      切槽 2×φ6.5mm（宽 2、底径 φ6.5）退刀槽
      M8 螺纹段长 8.5，端部倒角 C1（按约定 1 车成大径 φ8 光圆柱）

    工序 3 的「保证长度为 10.5mm」= 退刀槽 2 + 螺纹 8.5，与工序 3
    「从棒料外端车螺纹外径 φ8 及切槽」的做法一致，互为印证。

    偏心轴颈与其余同轴部分不能合成一条母线（轴线错开 2mm），故分开建：
    同轴部分用一条母线回转、再把六方棱柱外的料切掉；偏心轴颈单独做成 φ8 圆柱叠加。
    """
    R_PIN, R_HEX, R_ROOT, R_GROOVE, R_TH = 4.0, 14.0 / math.sqrt(3.0), 6.0, 3.25, 4.0
    OFF, C1 = 2.0, 1.0
    L_PIN, L_ROOT, L_HEX, L_GROOVE, L_TH = 17.0, 0.5, 5.0, 2.0, 8.5
    z_root = L_PIN
    z_hex = z_root + L_ROOT
    z_groove = z_hex + L_HEX
    z_th = z_groove + L_GROOVE
    z_end = z_th + L_TH
    D_M4_PILOT, L_M4_PILOT = 3.3, 12.0

    profile = [
        (0.0, z_root), (R_ROOT, z_root), (R_ROOT, z_hex),
        (R_HEX, z_hex), (R_HEX, z_groove),
        (R_GROOVE, z_groove), (R_GROOVE, z_th),
        (R_TH, z_th), (R_TH, z_end - C1), (R_TH - C1, z_end),
        (0.0, z_end), (0.0, z_root),
    ]
    part = revolve(profile)
    # 六方段：母线里按外接圆回转，再把该圆柱内、六方外的料切掉
    part = part.cut(Part.makeCylinder(R_HEX, L_HEX, Vector(0, 0, z_hex))
                    .cut(hex_prism(14.0, L_HEX, z_hex)))
    # 偏心轴颈：轴线偏 +Y 2mm
    part = part.fuse(Part.makeCylinder(R_PIN, L_PIN, Vector(0, OFF, 0.0),
                                       Vector(0, 0, 1)))
    # M4 螺纹底孔（约定 1 的内螺纹对应做法：按攻丝前底孔建）
    part = part.cut(Part.makeCylinder(D_M4_PILOT / 2.0, L_M4_PILOT,
                                      Vector(0, OFF, 0.0), Vector(0, 0, 1)))

    v_hex_cyl = math.pi * R_HEX ** 2 * L_HEX
    v_hex_real = math.sqrt(3.0) / 2.0 * 14.0 ** 2 * L_HEX
    v_theory = (profile_volume(profile) - (v_hex_cyl - v_hex_real)
                + math.pi * R_PIN ** 2 * L_PIN
                - math.pi * (D_M4_PILOT / 2.0) ** 2 * L_M4_PILOT)

    blank = hex_prism(14.0, 34.0, -1.0)     # 下料 34，工序 4 车端面到 33
    emit('调整偏心轴_TiaoZhengPianXinZhou', 'TiaoZhengPianXinZhou', blank,
         part, v_theory=v_theory,
         notes=(
             '尺寸链 17+0.5+5+2+8.5 = %.0f mm' % z_end,
             '偏心 2mm 相对 M8 螺纹基准轴线，偏心方向取 +Y',
             '图 4-11 上的「8」是 M4 攻深（表 4-7 工序 4「攻螺纹 M4，深 8mm」）；'
             '「5」是六方段长；「0.5」是清根宽；φ12 是清根直径',
             '与工序 3「保证长度为 10.5mm」互证：10.5 = 退刀槽 2 + 螺纹 8.5',
             '螺纹按大径车成光圆柱（约定 1）：M8 -> φ8；'
             'M4 内螺纹按攻丝前底孔 φ3.3 建',
             '毛坯按工序 1/3：六方钢 14，单件切至 34 长（成品 33，多出的 1mm 在偏心端）',
         ))


# -------------------------------------------------------------- 4.1.6 连杆螺钉

def seg_area(R, x):
    """半径 R 的圆被直线 x = const 截去 x 以外那部分的面积（x < R）。"""
    if x >= R:
        return 0.0
    h = R - x
    return R * R * math.acos((R - h) / R) - (R - h) * math.sqrt(2.0 * R * h - h * h)


def _flat_volume(r_of_z, x, z0, z1, n=400):
    """沿 z 把「圆被直线截掉的面积」积分起来，得到铣出该扁面的体积。"""
    total = 0.0
    for i in range(n):
        za = z0 + (z1 - z0) * i / n
        zb = z0 + (z1 - z0) * (i + 1) / n
        total += 0.5 * (seg_area(r_of_z(za), x) + seg_area(r_of_z(zb), x)) \
            * (zb - za)
    return total


def cross_hole_volume(r_hole, r_part, n=2000):
    """一个过径孔（钻头半径 r_hole）在半径 r_part 实心圆柱上挖去的体积。

    孔轴与圆柱轴垂直且相交，孔截面所在的圆整个落在材料里，但材料沿孔轴方向的
    边界是圆弧，所以不能按 πr²·(2·r_part) 算（那会多算约 0.5%）。
    体积 = ∬_{孔截面} 2√(r_part² − y²) dy du，代 y = r_hole·sin t 后积分核光滑：
        V = 4 r_hole² ∫_{-π/2}^{π/2} cos²t · √(r_part² − r_hole² sin²t) dt
    """
    r, R = r_hole, r_part

    def f(t):
        return math.cos(t) ** 2 * math.sqrt(R * R - (r * math.sin(t)) ** 2)

    half = math.pi / 2.0
    h = math.pi / n                     # 区间是 [-π/2, π/2]，跨度 π
    total = f(-half) + f(half)
    for i in range(1, n):
        t = -half + i * h
        total += (4.0 if i % 2 else 2.0) * f(t)
    return 4.0 * r * r * total * h / 3.0


def build_lianGanLuoDing():
    """图 4-7 / 表 4-6，连杆螺钉，40Cr，自由锻 φ52×27 + φ41×183。

    轴向尺寸链（自头部左端面）：15 | 45 | 60 | 30 | 40 = 190
      φ45 头部 15（工序 11 车 15.1，工序 13 磨右端面保证 15），左端面 C1
      头部铣两扁，对边 42±0.1（工序 16），扁深 (45-42)/2 = 1.5
      R2 过渡到 φ30×45（工序 11：φ30 杆径）
      φ34(-0.016/0) × 60 定位段（Ra 0.8，基准 A，圆度/圆柱度 0.008）
      φ30 × 30
      M30×2-6g × 40 螺纹（按约定 1 车成大径 φ30 光圆柱，与 φ30 段连成一体）
      距左端面 165（离右端面 25）处钻 2 × φ6 过径孔，互成 90°（工序 17，
      端视图呈十字；角向以 42±0.1 的扁面定位）
    图 4-7 的 R2（头部过渡）、R1（φ34 段两端根部，两处）已按标注建出；
    螺纹收尾 R0.5 未建（见 notes）。
    """
    R_HD, R30, R34, C1 = 22.5, 15.0, 17.0, 1.0
    R2, R1 = 2.0, 1.0
    L_HD, L_A, L_34, L_B, L_TH = 15.0, 45.0, 60.0, 30.0, 40.0
    z_c1, z_34a = L_HD, L_HD + L_A
    z_34b, z_th = z_34a + L_34, z_34a + L_34 + L_B
    z_end = z_th + L_TH
    HALF_FLAT = 42.0 / 2.0
    D_HOLE, Z_HOLE = 6.0, 165.0

    profile = [
        (0.0, 0.0), (R_HD - C1, 0.0), (R_HD, C1), (R_HD, z_c1),
        (R30 + R2, z_c1),
    ]
    profile += arc_points(R30 + R2, z_c1 + R2, R2, 270.0, 180.0)   # 头部 R2
    profile += [
        (R30, z_34a - R1),
    ]
    profile += arc_points(R30 + R1, z_34a - R1, R1, 180.0, 90.0)   # 台阶根部 R1
    profile += [
        (R34, z_34a + C1), (R34, z_34b - C1), (R34 - C1, z_34b),
    ]
    profile += arc_points(R30 + R1, z_34b + R1, R1, 270.0, 180.0)  # 台阶根部 R1
    profile += [
        (R30, z_th), (R30, z_end - C1), (R30 - C1, z_end),
        (0.0, z_end), (0.0, 0.0),
    ]
    part = revolve(profile)

    # 头部两扁：对边 42，两侧各切掉 |x| > 21 的料
    for x0 in (HALF_FLAT, -HALF_FLAT - 9.0):
        box = Part.makeBox(9.0, 60.0, z_c1 + 2.0, Vector(x0, -30.0, -2.0))
        part = part.cut(box)
    # 2 × φ6 过径孔，互成 90°
    part = radial_hole(part, D_HOLE, Z_HOLE, 0.0, 31.0, 30.0)
    part = radial_hole(part, D_HOLE, Z_HOLE, 90.0, 31.0, 30.0)

    # 理论体积：回转体 - 两扁 - 两孔
    # 两孔的相交部分正好是完整的 Steinmetz 体 16r³/3（两孔约束已蕴含 x²+y² ≤ 18 < R30²），
    # 但单孔不能用 πr²·30 估，见 cross_hole_volume 的说明。
    v_flat = _flat_volume(lambda z: R_HD - C1 + z if z < C1 else R_HD,
                          HALF_FLAT, 0.0, z_c1)
    r_hole = D_HOLE / 2.0
    v_holes = 2.0 * cross_hole_volume(r_hole, R30) \
        - 16.0 * r_hole ** 3 / 3.0
    v_theory = profile_volume(profile) - 2.0 * v_flat - v_holes

    blank = revolve(_shift(stepped_profile([(27.0, 52.0), (183.0, 41.0)]),
                           -10.0))
    emit('连杆螺钉_LianGanLuoDing', 'LianGanLuoDing', blank, part,
         v_theory=v_theory,
         notes=(
             '尺寸链 15+45+60+30+40 = %.0f mm' % z_end,
             '头部 φ45 按工序 13 磨后 15mm 建（工序 11 的 15.1 是留 0.1 磨量的工艺尺寸）',
             '两扁对边 42±0.1（工序 16），扁深 (45-42)/2 = 1.5mm',
             '螺纹按大径车成光圆柱（约定 1）：M30×2-6g 大径 φ30 与 φ30 杆径同径，连成一体',
             '2×φ6 过径孔距左端面 165（图 4-7），端视图呈十字故按互成 90° 建',
             '图 4-7 的 R2（头部过渡）与 φ34 段两端根部 R1 已建出',
             'R0.5（螺纹收尾，图 4-7）未建：螺纹已按约定 1 车成光圆柱，收尾圆角无处安放',
             '毛坯按工序 2 自由锻件：头部 φ52×27 + 杆部 φ41×183，总长 210（两端各余 10）',
         ))


# -------------------------------------------------------------- 4.1.5 阀螺栓

def build_faluosuan():
    """图 4-6 / 表 4-5，阀螺栓，45 钢，棒料 φ24×860（8 件连下）。

    轴向尺寸链（自左端面）：56 | 5 | 10 | 5 | 24 = 100
      左端 M20-7h 螺纹段 56（约定 1：按大径 φ20 车成光圆柱）
      R5 过渡 5（图 4-6 顶部两个 5）
      φ22(-0.025/-0.085) 定位外圆 10
      R5 过渡 5
      右端 M20-7h 螺纹段 24
    右端内孔 φ12.5 深 10，孔口 120° 坡口、最大直径 φ16.5，两端 C1。

    不确定项：R5 圆角径向只抬升 1mm（φ20 → φ22），而图上两个 5 是轴向尺寸，
    真正的 R5 圆弧与两端圆柱都相切在几何上不可能（需 R = 0.5）。这里按「与 φ22
    圆柱相切的真 R5 圆弧」建模：弧的轴向跨距 3mm，剩下 2mm 是 φ20 直段，
    合计正好是图上标的 5mm。
    """
    R_TH, R_LAND, R_HOLE, R_CS = 10.0, 11.0, 6.25, 8.25
    RF = 5.0                                  # R5 过渡圆角
    L_L, L_T, L_LAND, L_R = 56.0, 5.0, 10.0, 24.0
    Z_A, Z_B = L_L, L_L + L_T                 # 左过渡：[56, 61]
    Z_C, Z_D = Z_B + L_LAND, Z_B + L_LAND + L_T   # 右过渡：[71, 76]
    Z_END = Z_D + L_R                         # 100
    C = 1.0
    HOLE_DEPTH, CS_HALF = 10.0, 60.0          # 坡口 120° 全长 -> 半角 60°
    z_hole = Z_END - HOLE_DEPTH               # 孔底 90
    z_cs = Z_END - (R_CS - R_HOLE) / math.tan(math.radians(CS_HALF))
    t_hit = math.degrees(math.acos((R_TH - (R_LAND - RF)) / RF))   # 36.87°

    profile = [(0.0, 0.0), (R_TH - C, 0.0), (R_TH, C), (R_TH, Z_A)]
    profile += arc_points(R_LAND - RF, Z_A, RF, -t_hit, 0.0)       # 10 -> 11
    profile += [(R_LAND, Z_C)]
    profile += arc_points(R_LAND - RF, Z_C, RF, 0.0, t_hit)        # 11 -> 10
    profile += [(R_TH, Z_END - C), (R_TH - C, Z_END),
                (R_CS, Z_END), (R_HOLE, z_cs), (R_HOLE, z_hole),
                (0.0, z_hole), (0.0, 0.0)]
    part = revolve(profile)

    blank = make_cylinder_blank(24.0, Z_END + 1.0, Z_END)
    v_theory = profile_volume(profile)
    emit('阀螺栓_FaLuoSuan', 'FaLuoSuan', blank, part, v_theory=v_theory,
         notes=(
             '尺寸链 56+5+10+5+24 = %.0f mm（左端面起算）' % Z_END,
             'R5 过渡按与 φ22 圆柱相切的真圆弧建模，弧轴向跨距 %.2fmm，'
             '余下 2mm 为 φ20 直段，合计 5mm' % (RF * math.sin(math.radians(t_hit))),
             '螺纹按大径车成光圆柱（约定 1）：M20-7h -> φ20 圆柱',
             '毛坯按表 4-5 工序 3「切断保证总长 101mm」取 φ24×101',
         ))


# ---------------------------------------------------------------- 4.1.11 铜套

def build_tongtao():
    """图 4-16 / 表 4-11，铜套，ZCuSn10Zn2，棒料 φ45×40。

    外圆 φ39(+0.076/+0.060)、内孔 φ35(+0.041/+0.025)、总长 34(-0.45/-0.65)，
    两端 C1。内孔中部有一道环形润滑槽：深 0.5、长 24（工序 4「拉深 0.5mm，
    长 24mm 润滑槽」），两端各留 5mm 内孔台肩（图上两端标的「5」），
    槽中心距端面 17mm（= 总长之半）；同一轴向位置钻 φ5 径向油孔通入润滑槽。
    未注圆角 R0.5（未建）。

    注：表 4-11 工序 5 把外圆写成「φ35(+0.076/+0.060)mm」，与图 4-16 的
    φ39 矛盾（φ35 是内孔）——按图样取外圆 φ39、内孔 φ35（壁厚 2mm）。
    """
    R_OUT, R_BORE, L = 19.5, 17.5, 34.0
    GROOVE_W, GROOVE_D = 24.0, 0.5
    C = 1.0
    R_G = R_BORE + GROOVE_D                 # 槽底半径 18.0
    z_mid = L / 2.0                         # 17，槽 / 油孔都在这里
    OIL_D, OIL_DEPTH = 5.0, 2.0

    part = ring(R_OUT * 2.0, R_BORE * 2.0, L, c_out=(C, C), c_in=(C, C))
    part = part.cut(ring(R_G * 2.0, R_BORE * 2.0, GROOVE_W,
                         z0=z_mid - GROOVE_W / 2.0))
    part = radial_hole(part, OIL_D, z_mid, 0.0, OIL_DEPTH, R_OUT * 2.0)

    # 解析体积：外圆 - 内孔 - 润滑槽 - 油孔，再扣掉两端内外倒角
    v_outer = math.pi * R_OUT ** 2 * L - chamfer_loss([(L, R_OUT * 2.0)], C)
    v_bore = math.pi * R_BORE ** 2 * L + 2.0 * (
        frustum(R_BORE + C, R_BORE, C) - math.pi * R_BORE ** 2 * C)
    v_groove = math.pi * (R_G ** 2 - R_BORE ** 2) * GROOVE_W
    v_oil = math.pi * (OIL_D / 2.0) ** 2 * (R_OUT - R_G)
    v_theory = v_outer - v_bore - v_groove - v_oil

    blank = make_cylinder_blank(45.0, 40.0, L)
    emit('铜套_TongTao', 'TongTao', blank, part, v_theory=v_theory,
         notes=(
             '外圆 φ39(+0.076/+0.060)、内孔 φ35(+0.041/+0.025)：表 4-11 工序 5 '
             '把外圆误写作 φ35，按图 4-16 取 φ39（壁厚 2mm）',
             '润滑槽长 24、深 0.5，两端各留 5mm 内孔台肩（图上两端标的 5），'
             '槽中心距端面 17mm；油孔 φ5 的轴线同一位置，两者都落在总长 34 的中分面上',
             '毛坯按工序 1：棒料 φ45×40（成品 34 居中，两端各余 3）',
             '未注圆角 R0.5 未建（约定 3 只建图样给了尺寸的局部特征）',
         ))


# ---------------------------------------------------------------- 4.1.8 接头

def _hex_bevel_volume(across_flats, r_face, face_angle_deg, n=4000):
    """六棱柱端面按 face_angle（量自端面）倒锥时切掉的体积。

    锥面半径随进深增长：r(z) = r_face + z·tan(90° − face_angle)。
    r 不超过内切圆半径 a 时，切掉的就是「六方形 − 圆」；
    超出 a 之后圆已经越过六个平面，只剩六个角部余料，单个角部面积
    （θ* = acos(a/R) 为圆与平面相割的临界角，角部方向在两条棱正中间）：
        ∫_{θ*}^{30°} (a²sec²φ − R²) dφ = a²(1/√3 − tanθ*) − R²(π/6 − θ*)
    再乘 6。两段在 R = a 处连续（各角部余料恰为 0.0538a²）。
    """
    a = across_flats / 2.0
    c = across_flats / math.sqrt(3.0)
    t = math.tan(math.radians(90.0 - face_angle_deg))
    z_top = (c - r_face) / t
    a_hex = math.sqrt(3.0) / 2.0 * across_flats ** 2

    def removed_area(z):
        R = r_face + t * z
        if R <= a:
            return a_hex - math.pi * R * R
        th = math.acos(a / R)
        return 6.0 * (a * a * (1.0 / math.sqrt(3.0) - math.tan(th))
                      - R * R * (math.pi / 6.0 - th))

    h = z_top / n
    total = 0.0
    for i in range(n):
        za, zb = i * h, (i + 1) * h
        total += 0.5 * (removed_area(za) + removed_area(zb)) * h
    return total


def build_jieTou():
    """图 4-13 / 表 4-8，接头，Q235-A，棒料 φ50mm×340mm（五件连下）。

    轴向（自左端面）：16（M27×2-6g 螺纹段）| 3（切槽，底径 φ22）
      | 21（六方 42 对边，对角 48.5）| 17（锥管螺纹段，其中锥螺纹长 15）= 57
    内孔：φ15 通孔（表 4-8 工序 2「钻 φ15mm 孔深 60mm」，零件才 57 长，实为通孔）。
    六方两端面按 30° 倒锥（表 4-8 工序 2「倒六方左端面 30°角」、工序 3
    「倒六方右端面 30°角」）；图上只标角度、未标宽度，取端面处 R18.5
    —— 这是按图上量得的锥面在端面处的位置取整，锥顶到棱（对角半径 24.25）
    的进深 3.3mm，与图上倒角范围相符。

    两处按约定 1 不建牙型：M27×2-6g 车成大径 φ27 光圆柱；
    R1½ 锥管螺纹按图上 φ21.8 的外圆锥面建（不车牙）。
    """
    R_TH, R_GROOVE, R_TAPER, R_BORE = 13.5, 11.0, 10.9, 7.5
    R_HEX = 42.0 / math.sqrt(3.0)
    C1 = 1.0
    L_TH, L_GROOVE, L_HEX, L_TAPER = 16.0, 3.0, 21.0, 17.0
    TAPER = 1.0 / 60.0                      # 图 4-13 标注 1:60
    R_BEVEL, BEVEL_ANGLE = 18.5, 30.0       # 六方端面 30° 倒锥
    z_groove = L_TH
    z_hex = z_groove + L_GROOVE
    z_tap = z_hex + L_HEX
    z_end = z_tap + L_TAPER

    r_end = R_TAPER - TAPER * (L_TAPER - C1)

    profile = [
        (0.0, 0.0), (R_TH - C1, 0.0), (R_TH, C1), (R_TH, z_groove),
        (R_GROOVE, z_groove), (R_GROOVE, z_hex),
        (R_HEX, z_hex), (R_HEX, z_tap),
        (R_TAPER, z_tap), (r_end, z_end - C1), (r_end - C1, z_end),
        (0.0, z_end), (0.0, 0.0),
    ]
    part = revolve(profile)
    # 六方：母线按外接圆回转，再把该圆柱内、六方外的料切掉
    part = part.cut(Part.makeCylinder(R_HEX, L_HEX, Vector(0, 0, z_hex))
                    .cut(hex_prism(42.0, L_HEX, z_hex)))
    # 六方两端面 30° 倒锥
    t = math.tan(math.radians(90.0 - BEVEL_ANGLE))
    z_top = (R_HEX - R_BEVEL) / t
    for sign, z_face in ((1.0, z_hex), (-1.0, z_hex + L_HEX)):
        pts = [(R_BEVEL, z_face), (R_HEX + 5.0, z_face),
               (R_HEX + 5.0, z_face + sign * z_top),
               (R_HEX, z_face + sign * z_top)]
        face = Part.Face(Part.makePolygon(
            [Vector(p[0], 0, p[1]) for p in pts] + [Vector(pts[0][0], 0, pts[0][1])]))
        part = part.cut(face.revolve(Vector(0, 0, 0), Vector(0, 0, 1), 360.0))
    # φ15 通孔
    part = part.cut(Part.makeCylinder(R_BORE, z_end, Vector(0, 0, 0),
                                      Vector(0, 0, 1)))

    v_hex_cyl = math.pi * R_HEX ** 2 * L_HEX
    v_hex_real = math.sqrt(3.0) / 2.0 * 42.0 ** 2 * L_HEX
    v_bevel = 2.0 * _hex_bevel_volume(42.0, R_BEVEL, BEVEL_ANGLE)
    v_theory = (profile_volume(profile) - (v_hex_cyl - v_hex_real)
                - v_bevel - math.pi * R_BORE ** 2 * z_end)

    blank = Part.makeCylinder(25.0, 58.0, Vector(0, 0, 0))   # 工序 2 切下 58，工序 3 车到 57
    emit('接头_JieTou', 'JieTou', blank, part, v_theory=v_theory,
         notes=(
             '尺寸链 16+3+21+17 = %.0f mm；锥螺纹段长 15（表 4-8 工序 3）' % z_end,
             '锥管螺纹段按图 4-13 的 φ21.8 与 1:60 建（不车牙，约定 1）',
             '书稿存疑：R1-1/2 依 GB/T 7306 应为 1:16 锥度、大径 47.8mm，'
             '与图上 φ21.8、1:60 不符（φ21.8 反而接近 R1/2 的 20.955mm）。'
             '几何一律按图上显式尺寸建，不擅自改成标准值',
             '六方端面 30° 倒锥的宽度图上未注，取端面处 R18.5、进深 3.3mm 到棱',
             'M27×2-6g 车成 φ27 光圆柱（约定 1）；'
             '两端 C1（表 4-8 工序 2、工序 3 各一处）',
             '毛坯按工序 1/2：棒料 φ50，单件切至 58 长，'
             '多出的 1mm 在锥管螺纹端（工序 3 从那端车端面到 57）',
         ))


# ---------------------------------------------------------------- 注册表

PARTS = {
    '柱塞': build_zhusai,
    '输出轴': build_shuchuzhou,
    '齿圈': build_zhiqquan,
    '定位销轴': build_dingweixiaozhou,
    '阀螺栓': build_faluosuan,
    '铜套': build_tongtao,
    '活塞杆': build_huosaiGan,
    '连杆螺钉': build_lianGanLuoDing,
    '调整偏心轴': build_tiaoZhengPianXinZhou,
    '接头': build_jieTou,
}


def main():
    names = sys.argv[1:] or list(PARTS)
    for n in names:
        if n not in PARTS:
            raise SystemExit('未知工件: %s（可选: %s）' % (n, ', '.join(PARTS)))
        PARTS[n]()


if __name__ == '__main__':
    main()
