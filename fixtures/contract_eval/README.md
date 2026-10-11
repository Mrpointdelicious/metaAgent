# 契约对齐业务评测 v1

本目录是自建、非敏感开发夹具，依据本地 AI_WebApi 的 DTO、服务组装逻辑和合成测试对齐字段。源码版本与 hash 见 `source-pins.json`。这里没有调用 .NET 服务或患者数据库，也不证明已部署接口兼容。

`cases.json`、两份 corpus 和源码 pin 在基线前冻结，hash 见 `frozen.sha256.json`。`python -m tools.contract_fixtures` 仅用于创建新目录，不覆盖现有目录；评测加载时重新校验所有 pin。

共 28 条开发条件：C01–C16 是新增条件，每次 19 个对话轮次；B01–B12 复用旧评测问题，每次 17 轮，但改用本目录的契约对齐后端。旧 `business_eval` 夹具与历史结果保持原样。B 的新结果不能称为旧抽象接口结果的重跑。

正常就诊返回使用 `medical_records[].content.doctor_orders`、`prescriptions[]`；康复返回使用 `plans[]`、`plan_page` 和 `iretour/irego.history`。C03 的处方 null、C04 的错误列表类型属于异常响应鲁棒性条件，不能计为正常 DTO 输出覆盖。另有单元测试检查可选字段省略与必需字段缺失。

C10 的图片只返回自建假制品元数据，用于验证允许条件下的工具与事件链路；没有真实生成或视觉验收 PNG。C15 的 `partial_corpus.json` 只移除了方案乙的结局一节，保持其余文字一致，检验缺一维时不补造答案。

所有条件均为已打开开发集，`clinical_approval=false`。自动检查不等于陈述支持、独立人工或临床验收。

冻结后的评分勘误单独保留：C09 关闭报表不要求读取详细分析；C10 可使用已查询历史记录定位。`tools/review_contract_eval.py` 的 `contract-route-review-v2` 对基线与候选统一复核，并额外要求 C10 的制品事件。它不修改原始分数、问题或 Gold，也不把词法告警自动判成语义错误。
