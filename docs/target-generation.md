# G0 目标几何生成（pilot.targets）

## 范围

只做 l001-Lab STEP 3 的目标几何设计生成与数值验证：输出 `stock.step`、`target.step`、`preview.png`、
`validation.json`、`case.json`，根目录写 `manifest.json`、`config_snapshot.json`、`overview.png`。
不生成 Candidate、刀具、刀路、扫掠/材料去除仿真、Q3 状态、预算 runner 或 24 条候选记录。
目标几何 = 毛坯 Boolean 减去独立设计盲腔，只依赖独立设计参数。

## 冻结协议 v1.0

- 单位 mm，右手 XYZ，Z 向上；毛坯为 `[0,L]x[0,W]x[-H,0]`，顶面 Z=0。
- 目标：居中闭合圆角矩形盲腔（`rounded_rect_blind_pocket`），参数 `a,b,R,d`，深度沿 -Z，腔底在 Z=-d。
- 家族：六件全部属于 F1；`master_seed = 20261012`。
- 子种子规则：取 `SHA256('{master_seed}|F1|{case_id}|G0_TARGET')` 的前 8 字节，按大端解释为 uint64。
- 容差：包围盒极值 0.001 mm；体积误差 `max(0.001 mm^3, 1e-6 * 原始体积)`。
- 同一个 `config_version` 内，修改上表尺寸、家族、种子、单位、坐标、种子规则或容差都会被拒绝。

| case | stock L,W,H | target a,b,R,d |
| --- | --- | --- |
| S01 | 60,50,16 | 30,24,3,6 |
| S02 | 80,45,16 | 54,14,3,5 |
| S03 | 60,55,20 | 36,30,4,12 |
| S04 | 70,55,16 | 46,32,2,4 |
| S05 | 60,50,12 | 48,36,3,3 |
| S06 | 55,50,20 | 24,20,4,10 |

## 使用

```sh
# 先验证单件
python -m pilot.targets --config configs/g0_targets_v1.json --case S01 --out <NEW_ROOT>
# 不带 --case 生成六件
python -m pilot.targets --config configs/g0_targets_v1.json --out <NEW_ROOT>
```

退出码：0 全部 PASS；1 存在验证失败或 ERROR/UNKNOWN；2 配置或用法错误（含冻结值篡改）。输出根目录必须是新建目录或空目录，
否则拒绝覆盖并返回非零。CLI 始终请求 preview；请求的 `preview.png` / `overview.png` 缺失或渲染失败时为 ERROR 并返回非零。

## Python API

- `load_config(path)`：读取并校验冻结配置。
- `derive_seed(master_seed, family_id, case_id)`：冻结子种子。
- `build_design(spec)`：只用设计参数构建毛坯盒、盲腔棱柱与目标实体；测试可用较小合法参数。
- `generate_case(spec, output_root, master_seed, preview=True)`：单件全流程（构建、导出、重导入、检查、预览）。
- `generate_batch(config, output_root, master_seed=None, preview=True, case_ids=None)`：批量并写 manifest。
- `main(argv)`：CLI 入口。

## 哈希含义

- `design_semantic_hash`：设计投影 `{schema_version:'design-v1', protocol_version, units, coordinates, stock, target}` 的规范化语义哈希。
  只依赖独立设计参数，不含 seed、输出路径、STEP 时间戳，是设计参数身份，不是 BRep 相等的证明，
  也不授权复用既有 STEP、缓存或验证结果；任何缓存复用都必须先验证真实 BRep。
- `case_record_hash`：完整最终 Case 记录（含 `seed`、artifact 相对路径与 STEP 字节 `sha256`）的语义哈希。
  仅作溯源自证，会随 STEP 字节或路径变化，不得当作几何身份。
- 每个 STEP 额外记录 `sha256` 与字节数；写入文件的 sha256 均为导出后重新计算的真实值，不含占位哈希。

## 数值检查

报告 schema 为 `target-validation-v1`，作用域字符串明确声明这是目标几何的设计/内核一致性检查，
不是 Q3、不是制造公差、不是 G0 PASS。检查只分两个作用域：

- `design`：解析式设计一致性（尺寸为正、`R <= min(a,b)/2`、盲腔严格位于毛坯内、解析腔体积、解析目标体积）。
- `kernel`：实体与内核实测量（单一有效有限实体、毛坯/盲腔/目标包围盒、体积与体积守恒、
  实际 Z=-d 腔底平面、腔底足迹尺寸与居中、实际底厚与侧壁厚为正、
  由 `BRepAdaptor_Surface(face.wrapped).Cylinder().Radius()` 读取的真实圆柱面半径与竖直轴线）。

腔底通过“严格位于毛坯底面与顶面之间的水平平面面”识别，要求**所有**腔底平面都位于 Z=-d；
多个共面裁剪面属于有效情形，其包围盒给出实际 `a/b` 与四边壁厚。毛坯与目标各自独立做
导出/重导入配对检查，`reimport_bbox` 记录每个极值的 `abs_error_mm` 与 `max_abs_error_mm`，
`reimport_volume` 记录 `abs_error_mm3`；解析式体积检查同样记录 `abs_error_mm3`。
导出 STEP 后重新导入，重新检查实体数、有效性、包围盒、体积、腔底与圆柱半径，且不使用导出前对象冒充。

失败语义分为三类，均非 PASS，且任何非 PASS 结果都不写 `case.json`：

- `FAIL`：几何数值不一致（尺寸、包围盒、体积、腔底、圆柱半径、实体数等检查失败）。
- `ERROR`：运行/输入/内核异常，例如访问器异常、几何访问失败、导出或重导入失败、预览渲染失败。
- `UNKNOWN`：不支持的输入（例如未知 target kind），报告保留 `reason_code=UNKNOWN_UNSUPPORTED`。

报告与返回结果保留 `reason_code` 与 `error`。批量中任意一行非 PASS 则整体非 PASS，失败行保留不省略；
只要存在 ERROR/UNKNOWN 行或 overview 失败，整体状态为 ERROR（ERROR 优先于 FAIL）。

## 已知限制

- 只覆盖目标几何本身，不覆盖刀具可达性、刀路、材料去除或制造公差。
- `preview.png` / `overview.png` 由 `Shape.tessellate(tolerance=0.1, angularTolerance=0.1)` 的真实网格渲染，
  仅作观察辅助，不构成几何验证。
- 圆角半径允许取 `R = min(a,b)/2`（胶囊或整圆），此时平直段与圆柱面数量会变化，检查按实际面计数判断。
- 请求预览（`preview=True`，CLI 默认）时，`preview.png` 与 `overview.png` 属于交付物：
  任一预览渲染失败都会使该件或整体为 ERROR 并让 CLI 返回非零，报告仍保留。
  `preview=False` 仍只做几何验证，不要求查看产物。
