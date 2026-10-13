# 第 2 步：数据契约与坐标约定验证摘要

日期：2026-10-13。基线 main：`49669a5d85590f95e51748777ec98da8dfe388c5`。
分支：`codex/i001a-step2-contracts`。

## 交付与边界

- `pilot/contracts.py`：标准库实现的 `Case`、`Candidate`、`Evidence`、`Run` TypedDict v1 定义及四个严格校验入口，JSON 解析/序列化、规范化语义哈希与原始文件校验和。
- `pilot/__init__.py`：公共函数入口。
- `tests/test_contracts.py`：JSON 往返、有限数值、坐标、尺寸、路径及依赖校验、状态/原因组合、规范化和哈希测试。
- `docs/data-contracts-v1.md`：字段与 API、支持域、错误语义及哈希规则。

毫米、右手 XYZ、顶面 Z=0、刀轴 [0,0,1]；支持 CUT_XY/CUT_Z/RAPID_XY/RAPID_Z，禁止 XYZ 斜移及倾斜刀轴。契约接受待评估的过深或宽刀候选，拒绝私有预期标签字段，不据此生成几何状态。

结构错误为 `ERROR/ERROR_INPUT`，不支持的运动为 `UNKNOWN/UNKNOWN_UNSUPPORTED`；Evidence 的几何 `FAIL`、运行 `ERROR` 和不确定 `UNKNOWN` 原因必须匹配。命名依赖必须齐全且哈希格式正确；此步骤不读取工件或证明依赖与实际文件内容相符。

语义哈希保留调用方提供的全部字段，域分隔且版本化；键顺序、等值整数/浮点与负零规范化，不做容差舍入。文件校验和只针对原始字节。未实现检查器、缓存失效或跨记录引用解析。

代码由本机 DSH `deepseek-v4-flash` 编写和修正。保留旧目录及成果，从新 main 独立检出；复用已有 Python 3.11.15 venv，未重复安装依赖。发布内容不含模型提示、响应、会话、安装日志、机器清单或生成几何工件。

## 实际测试

基线第 1 步：**19 passed in 9.54s**。

初版契约：91 项通过。静态审查后补充边界测试：最初生成测试的辅助调用签名及四条正例夹具调用不匹配，修正测试后，确认 16 个真实边界失败。随后由 DSH 修正哈希末尾换行、路径控制字符、非字符串运动枚举及闭合盲腔侧壁/半径约束。

最终实际命令（复用 venv 的 `python`）：

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -m pytest -q tests/test_contracts.py tests/test_smoke_geometry.py
```

结果：**138 passed in 4.47s，退出码 0**，其中契约 119 项、既有冒烟 19 项。标准库契约模块没有新增依赖。既有冒烟代码、配置与测试文件未修改。

本轮没有生成 24 条数据，没有执行 Q3/G0 或实现预算 runner。第 2 步交付暂停待用户检查，不自行合并或进入第 3 步。
