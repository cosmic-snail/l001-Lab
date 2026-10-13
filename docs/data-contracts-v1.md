# l001-Lab 数据契约 v1

本文件描述 l001-Lab 第 2 步定义的四个顶层 JSON 记录、公共 API 与哈希契约。实现位于 `pilot/contracts.py`，仅使用 Python 标准库。本步骤只定义契约：不生成目标、不做几何求值、不运行检查器、不读取工件内容。

## 单位与坐标

- 单位固定为 `mm`。
- 右手坐标系 XYZ，`Z` 轴向上。
- 毛坯范围 `[0,L] x [0,W] x [-H,0]`，顶面 `Z=0`。
- 刀具轴固定为竖直 `[0,0,1]`；倾斜刀具按不支持处理。

## 记录总览

| 记录 | `schema_version` | 用途 |
| --- | --- | --- |
| Case | `case-v1` | 输入案例与目标声明 |
| Candidate | `candidate-v1` | 候选刀具路径 |
| Evidence | `evidence-v1` | 单次检查报告 |
| Run | `run-v1` | 一次运行的清单 |

### Case `case-v1`

字段：`case_id`、`version`（非负整数）、`family_id`、`ancestor_id`（可为 `null`）、`seed`（uint64）、`units`、`coordinates`、`stock`、`target`、`artifacts`。

- `coordinates` 固定为 `{handedness:right, up:Z, top_z:0, tool_axis:[0,0,1]}`。
- `stock` 为 `{L,W,H}`，三者均为正。
- `target` 为 `{kind:rounded_rect_blind_pocket, a, b, R, d}`：封闭的圆角矩形盲槽，四壁与底面均为正；`a,b>0`，`R>0` 且 `R<=min(a,b)/2`，`d>0` 且 `d<H`；须严格位于毛坯侧壁与底面之内（`a<L` 且 `b<W`）。该不等式描述第一阶段支持域，不构成对 Candidate 的几何分类。
- `artifacts` 为 `{stock: Artifact, target: Artifact}`；`Artifact` 为 `{path, sha256}`：`path` 是相对可移植路径（正斜杠，禁止绝对路径、盘符、UNC、回溯与空段），`sha256` 是文件原始字节 SHA256 校验和（64 位小写十六进制），不能填入语义哈希。
- 校验不读取文件内容，也不从校验和推断几何。

### Candidate `candidate-v1`

字段：`candidate_id`、`case_id`、`version`、`case_hash`（语义 Case 哈希）、`units`、`tools`、`path`。

- `tools` 非空，`tool_id` 唯一；`radius>0`、`cutting_length>0`、`axis=[0,0,1]`。
- `path` 非空，每段为 `{start, end, tool_id, operation_id, mode}`；`tool_id` 必须引用已定义刀具；相邻段必须首尾严格相接。
- `mode` 必须为字符串并取 `CUT_XY`、`CUT_Z`、`RAPID_XY`、`RAPID_Z` 之一；非字符串属于输入错误（`ERROR_INPUT`），字符串但不在集合内按不支持（`UNKNOWN_UNSUPPORTED`）处理。XY 模式要求首末 `Z` 相同，Z 模式要求首末 `X/Y` 相同；允许零长度段。
- `CUT_Z` 采用中心切削假设；`RAPID` 不去除材料。
- 校验不假设路径位于毛坯或目标之内；`case-v1` 的 `target` 不等式属于第一阶段支持域声明，几何上过深或刀具过宽的候选仍是合法 Candidate，留待后续评估。

### Evidence `evidence-v1`

字段：`case_id`、`candidate_id`、`version`、`check_id`、`status`、`reason_code`、`metrics`、`dependency_hashes`、`checker_version`、`tolerance_version`、`state_hash`、`wall_seconds`、`cpu_seconds`、`input_artifacts`、`output_artifacts`。

- `check_id` 取 `Q1`、`Q2`、`Q3`。
- `status` 取 `PASS`、`FAIL`、`UNKNOWN`、`ERROR`；`reason_code` 必须匹配：
  - `PASS` 对应 `OK`
  - `FAIL` 对应 `GEOMETRY_FAIL`
  - `UNKNOWN` 对应 `UNKNOWN_NUMERIC`、`UNKNOWN_UNSUPPORTED`、`UNKNOWN_TIMEOUT`
  - `ERROR` 对应 `ERROR_INPUT`、`ERROR_KERNEL`、`ERROR_PROCESS`
- `metrics` 仅允许有限数值，或有限数值数组。
- `dependency_hashes` 非空，键为语义输入名，值为 64 位小写十六进制；每个检查的必需依赖必须齐全：
  - `Q1`：`tools`、`path`、`fixtures`、`coordinates`
  - `Q2`：`stock`、`tools`、`path`、`coordinates`、`prior_state`
  - `Q3`：`stock`、`target`、`tools`、`path`、`coordinates`
- Evidence 是报告结构，不是检查器；状态不得由候选元数据推导。

### Run `run-v1`

字段：`run_id`、`protocol_version`（固定 `1.0`）、`seed`（uint64）、`case_hashes`、`candidate_hashes`、`config_hash`、`checker_version`、`tolerance_version`、`started_at`、`mode`。

- `case_hashes`、`candidate_hashes` 为 ID 到语义哈希的非空映射。
- `started_at` 为 UTC ISO8601 字符串；`mode` 取 `P1-A` 或 `P1-S`。
- 不包含私有评估文件或标签。

## API

- `validate_case`、`validate_candidate`、`validate_evidence`、`validate_run`：返回规范化后的独立副本，拒绝缺失键与未知键。
- `serialize_record(kind, record)`、`parse_record(kind, text)`：JSON 记录边界。
- `canonical_json(value)`：确定性紧凑编码。
- `semantic_hash(value)`：语义哈希。
- `file_checksum(path)`：原始字节分块 SHA256。
- `ContractError(status, reason_code, message)`：统一异常。

规范化：`1.0` 归一为 `1`，`-0.0` 归一为 `0`；不做量化或近似。JSON 边界拒绝重复键、`NaN` 与 `Infinity`、布尔冒充数值、未知 `schema_version`、未知键与错误记录类型。

## 哈希契约

`canonical_json` 使用 UTF-8、键排序、紧凑分隔符、无 `NaN`；数学相等的数值产生相同十进制表示（`1` 与 `1.0`、`-0.0` 与 `0`）；`bool` 仍为 `bool`；保留任意大整数精度；拒绝非字符串映射键与非有限数。

`semantic_hash` 是带域分隔、带版本的 SHA256，输入为 `canonical_json` 的语义值：域前缀 `l001-lab/data-contracts/semantic/v1` 加一个 NUL 字节。它只哈希调用方显式提供的语义输入；若输入包含版本、实际路径、刀具、目标或依赖键，则一并参与。它不会自动排除未知字段，也不会静默忽略输入。

`file_checksum` 对原始字节分块计算 SHA256。两者域不同：JSON 空白或键顺序变化会改变 `file_checksum`，但不会改变 `semantic_hash`。

调用方须自行选择几何与检查依赖投影，并另行保留整条记录的溯源哈希；两种用途不要混用。

## 限制

- 未实现缓存失效、相似度复用或浮点舍入。
- 本步骤不生成目标、不评估几何、不运行检查器，也不计时。
- `semantic_hash` 不自动排除任何字段；未知字段是否参与哈希由调用方显式决定。
