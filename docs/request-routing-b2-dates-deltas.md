# B2：本地历史日期识别与117轮基线

2026-10-29，承接任务#204、方案#206和批准#207。当前规则为 `contextual-routing-b2-dates-v1`，父版本为 `contextual-routing-b-v1`。

模型漏填或只抄出部分历史日期时，程序在遮蔽证券名称、指标周期和展望期限后，仅补认一个完整、可解析的原文表达。最近/近期、近N自然日/天/日/周/月/年、ISO日期/区间及截至某日继续共用同一日期换算；中文数字也走同一校验。多个表达、剩余不支持的时间词、交易日、非法数量/日期、超限区间不猜。原文外模型条件仍先报 `ungrounded_intent`。“未来一个月”只进入展望期限；未来行情日期不获许可。

`applied_defaults`（参数处理记录）增加 `date_expression:local_recognition`。普通trace只记固定的本地日期操作及结果码，不写原问题；终端给出中文说明。公司研究共用日期函数，补认的ISO日期仍保留明确历史标记，不能把当前资料带入历史研究。明确修改/刷新日期时清理原本地识别标记，单改指标时保留它。

## 历史基线

以v5有效结果为父，沿用原观察器，重新执行路由v1/v2的105轮和决策v3的12轮。39个场景、117轮全部逐字段零差异，受影响轮次为0；路由、证券、参数、状态、调用数、事件和回答摘要均未变化。旧语料里模型已完整识别的路径照旧，原本不支持的时间仍返回原澄清，不以改文案制造摘要变化。新行为由独立日期案例及D20漏填模拟回归覆盖，不回录历史题目或观察结果。

v1～v5、A2/A3差异、B预期及观察器全部未改。以下逐场景列明所有轮次均零差异，B原有7个变化轮属于上一个版本，不算本批变化。

| 场景 | 本批核对轮次 | 字段差异 |
| --- | --- | --- |
| `knowledge-ready` | 1～2 | 0 |
| `knowledge-directory-missing` | 1 | 0 |
| `knowledge-stale` | 1～2 | 0 |
| `workflow-off` | 1～2 | 0 |
| `workflow-off-fixed` | 1 | 0 |
| `fixed-knowledge` | 1～2 | 0 |
| `fixed-knowledge-directory-gate` | 1 | 0 |
| `mixed-named-directory-gate` | 1～2 | 0 |
| `screen-new-and-replace` | 1～3 | 0 |
| `screen-explicit-interval` | 1 | 0 |
| `screen-no-source` | 1 | 0 |
| `edits-no-list` | 1～3 | 0 |
| `edits-refresh-sample` | 1～4 | 0 |
| `edits-empty-list` | 1～3 | 0 |
| `edit-company-lookalikes` | 1～3 | 0 |
| `ordinal-no-list` | 1 | 0 |
| `ordinal-selections` | 1～3 | 0 |
| `named-time-ordinals` | 1～6 | 0 |
| `company-pending-owns-replies` | 1～6 | 0 |
| `technical-pending` | 1～5 | 0 |
| `indicator-pending` | 1～4 | 0 |
| `company-technical-capabilities` | 1～4 | 0 |
| `company-disabled` | 1～3 | 0 |
| `knowledge-does-not-renew-list` | 1～4 | 0 |
| `current-pronoun-versus-list` | 1～4 | 0 |
| `company-followups-and-history` | 1～5 | 0 |
| `technical-edit-and-commands` | 1～5 | 0 |
| `replay-and-isolation` | 1～4 | 0 |
| `source-change` | 1～2 | 0 |
| `lazy-clock-explicit-ordinal` | 1～2 | 0 |
| `lazy-clock-named-member` | 1～2 | 0 |
| `selected-technical-followup` | 1～4 | 0 |
| `failed-research-consumes-current` | 1～4 | 0 |
| `cancelled-selection` | 1～3 | 0 |
| `development-mode` | 1～3 | 0 |
| `decision-direct-and-history` | 1～2 | 0 |
| `decision-paraphrases` | 1～4 | 0 |
| `decision-identity-boundary` | 1～3 | 0 |
| `decision-domain-limits` | 1～3 | 0 |

严格比较可复现：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_routing_baseline tests.test_decision_routing_baseline
```

## 2a规则登记与验证

[规则描述](../data/evaluation/intent-rule-baselines-v1.json)保留B的完整23文件摘要及规则身份于 `previous_rules`，B2另算当前源码摘要。旧304题、划分、逐题标注及manifest不变；不能套用B阈值锁/参考报告，测试覆盖从B到B2的拒绝，即使分类输出相同也不能绕过版本边界。旧报告可读，不改写历史成绩。

日期专项比较本地补认与完整模型提取的相同起止日和换算标记，包含最近、近期、近60日、近三个月和2026-09-01至2026-10-28；覆盖截短区间、中文数字、冲突/不支持/非法/超限条件、展望与指标隔离、原文外日期和trace无原文泄露。D20完整聊天回归只把准备模型的“最近”改为漏填，仍完成技术准备并形成正确对象和期限的研判，没有增加调用。

离线固定20题仍形成20/20、46次模型转发；使用人工资料和固定脚本，不代表B2真实模型质量。每题6次/总120次及20题固定分母不变。A4合入后真实成绩为19/20、¥0.570522、57次调用、截断0、删句73/675、修复4/15；指标达成仍需人工复核20条研判，详见[评估说明](decision-answer-evaluation-design.md)。B2合入后的真实成绩另记。
