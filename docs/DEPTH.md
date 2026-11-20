# 深度登记 再生合金成分混配与验收

- 规模：中型
- 使用者与工作流：再生金属配料员，合并废料检验批，按配方称重混配并复核各元素上下限。
- 核心规则：元素质量按物料净重×含量汇总；干湿基转换，低于检出限给上下界，不可把未检出当零，配方版本锁定。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 来料接收 | 运行来料接收并复核证据 | `POST /api/m1`；`/work/1` | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py` |
| M2 成分归一 | 运行成分归一并复核证据 | `POST /api/m2`；`/work/2` | `app/modules/m2_assay/rules.py` | `tests/test_m2.py` |
| M3 配方约束 | 运行配方约束并复核证据 | `POST /api/m3`；`/work/3` | `app/modules/m3_recipe/rules.py` | `tests/test_m3.py` |
| M4 混配试算 | 运行混配试算并复核证据 | `POST /api/m4`；`/work/4` | `app/modules/m4_blend/rules.py` | `tests/test_m4.py` |
| M5 不确定区间 | 运行不确定区间并复核证据 | `POST /api/m5`；`/work/5` | `app/modules/m5_uncertainty/rules.py` | `tests/test_m5.py` |
| M6 验收对照 | 运行验收对照并复核证据 | `POST /api/m6`；`/work/6` | `app/modules/m6_acceptance/rules.py` | `tests/test_m6.py` |
| M7 复配批次 | 撤料试算复核 | `POST /api/m7`；`/work/7` | `app/modules/m7_reblend/rules.py` | `tests/test_m7.py` |
| M7 整组补料 | 查看整组补料方案、确认扣料与验收重算 | `POST /api/reblend/plan`、`/api/reblend/confirm`；`/work/7` | `app/reblend.py`、`app/reblend_store.py`、`app/modules/m7_reblend/handler.py` | `tests/test_reblend.py`、`tests/test_reblend_api.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py::test_legacy_encoding` |
| TRAP-06 | 千分位与全角 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_assay/rules.py` | `tests/test_m2.py::test_thousands` |
| TRAP-07 | 空值不可当零 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_assay/rules.py` | `tests/test_m2.py::test_sentinel_isolation` |
| TRAP-10 | 干重精确守恒 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m4_blend/rules.py` | `tests/test_m4.py::test_decimal_dry_total` |
| TRAP-14 | 同键冲突 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_assay/rules.py` | `tests/test_m2.py::test_conflict_preserved` |
| TRAP-15 | 吨与千克 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_assay/rules.py` | `tests/test_m2.py::test_units` |
| TRAP-19 | 批号元数据冲突优先 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m3_recipe/rules.py` | `tests/test_m3.py::test_conflicting_lot_mass` |
| TRAP-25 | 配方版本锁定 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m3_recipe/rules.py` | `tests/test_m3.py::test_version_lock` |
| TRAP-31 | 补料未化验元素当零 | 补后区间虚窄、误判通过 | 未化验按0~100保守区间并入上下界 | `app/reblend.py` | `tests/test_reblend.py::test_unmeasured_element_never_treated_as_zero` |
| TRAP-32 | 剂量不等式负系数不翻转 | 可行域算反、超上限补料 | 系数正负分支显式处理并取交集 | `app/reblend.py` | `tests/test_reblend.py::test_dilution_material_has_upper_cap` |
| TRAP-33 | 并发确认超领共用库存 | 两人同时提交各扣一份 | BEGIN IMMEDIATE串行化＋不足整单回滚 | `app/reblend_store.py` | `tests/test_reblend_api.py::test_concurrent_confirms_never_over_issue` |
| TRAP-34 | 网络重试重复扣料 | 同方案扣两次库存 | 确认键唯一约束＋幂等回放 | `app/reblend_store.py` | `tests/test_reblend_api.py::test_retry_same_key_does_not_deduct_twice` |
| TRAP-35 | 急单与省料两难 | 只按耗料排序拖过交期 | 急单按交期优先、普通单按最小耗料轮候 | `app/reblend.py` | `tests/test_reblend.py::test_urgent_order_allocated_before_normal` |

## 对账规则

输入记录 = 采信记录 + 隔离记录 + 相同行重复。重复表头与空行单独记数。业务合计按阶段中的守恒或差值列复核。运行清单有原文SHA256、规则版本与耗时；相同规则版本下同文重复提交复用批次。

## 已知不覆盖

部署为本地单用户工具，不连接外部上游系统。CSV单文件5MB及一万条业务记录上限；不推断未提供的未知数据。
