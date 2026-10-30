# A3 数字绑定与回答序列化：117轮差异

2026-10-29，承接方案 #183/#184。A3改变回答数字的来源核对、引用映射与程序统计，不调整路由。历史 `request-routing-v1/v2/v3/v4.json`、A2清单、观察器及2a的304题题库不改写。

[独立差异夹具](../tests/fixtures/request-routing-a3-deltas.json)在v4投影上应用A3变更；冻结v4与A2父文件的LF规范化SHA-256。39个场景、105+12共117轮逐字段核对，其中35轮变化、82轮零变化。只允许以下回答序列化路径的 `sha256` 与 `utf8_bytes` 改前/改后值：

- `/before/product_saved/answer/full_json/…`
- `/after/product_saved/answer/full_json/…`
- `/outcome/response/result/full_json/…`

变化来自新增数值计数及实际匹配来源引用；离线演示模型为可以唯一定位的数字附标注，以避免多个相近数值的舍入歧义，用户可见示意文字保持。路由、证券身份、参数、事件、工具读取与其他投影字段差异为0；保存回答变化也会影响后续轮的before/after摘要，不能将35轮解释成35次路由变化。原失败、待澄清及B上下文题仍保留。

## 变化轮次

轮号按场景内从1开始，完整逐字段before/after值在夹具中。其余24个场景的轮次均无变化。

| 场景 | 变化轮次 |
| --- | --- |
| `edits-refresh-sample` | 4 |
| `edit-company-lookalikes` | 2、3 |
| `ordinal-selections` | 3 |
| `named-time-ordinals` | 2、3、6 |
| `company-pending-owns-replies` | 6 |
| `company-technical-capabilities` | 1、2、3、4 |
| `current-pronoun-versus-list` | 2、3、4 |
| `company-followups-and-history` | 1、2、3、4、5 |
| `lazy-clock-named-member` | 2 |
| `selected-technical-followup` | 4 |
| `failed-research-consumes-current` | 1、2 |
| `decision-direct-and-history` | 1、2 |
| `decision-paraphrases` | 1、2、3、4 |
| `decision-identity-boundary` | 2、3 |
| `decision-domain-limits` | 1、2、3 |

`tests/routing_narrative_support.py`先核对原v4逐字段before值，再校验A3父摘要、路径和before值后应用独立覆盖；不删除完整观察投影检查。完整验证入口：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_routing_baseline tests.test_decision_routing_baseline
```

全量Python验证也包含这117轮。离线机械通过不代表真实模型质量；固定36稿校准与删句口径见[A3结果](answer-numeric-a3-results.md)。
