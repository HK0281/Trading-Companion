# 持有期限功能的117轮差异

父级为v6及回答核对覆盖层；v1～v6、原问题和三个历史观察器均保持原字节。
缺期限的价位问题先澄清，后续原消息照实受待确认状态影响，未插入额外回答。
其余差异为新方法标记、无默认期限的研判、价格来源核对及序列化摘要。续接成功由独立期限测试验证。

字段解释（每轮完整before/after值见`tests/fixtures/request-routing-v7.json`；原始观察器不变）：

| 字段路径 | 差异原因 |
| --- | --- |
| `*/applied_defaults` | 新解释计划显式记录v2方法；移除默认20交易日；明确期限另记来源与参数档位。 |
| `*/objective`、`*/horizon` | 无期限的方向/纯分析保留空期限；价位问题先进入期限澄清。 |
| `*/product_pending/stage`、`*/product_pending/securities` | 待确认状态新增股票/期限阶段标记及已确认身份；身份澄清也有默认阶段字段。 |
| `*/code`、`*/status`、`*/message`、`*/route`、`*/executor_chain` | 下列6个明确列出的原轮改为取数前澄清，历史请求无新结果；后续原轮继续由待确认任务处理。 |
| `*/calls`、`*/model_calls*`、`*/analyses*`、`*/daily*` | 缺期限时只保留最初理解调用，不取资料；纯期限续接不新增理解调用。 |
| `*/full_json/*`、`*/sha256`、`*/utf8_bytes`及保存结果摘要 | 公开计划、空期限、v2参数及新增待确认状态的序列化变化；内容摘要相应变化，未重写历史父级。 |
| `*/before/*`、`*/after/*`中的其余状态 | 以上行为在前后会话状态中的延续；未插入补充回答来改变原消息序列。 |

- knowledge-ready/1：无差异。
- knowledge-ready/2：无差异。
- knowledge-directory-missing/1：无差异。
- knowledge-stale/1：无差异。
- knowledge-stale/2：无差异。
- workflow-off/1：无差异。
- workflow-off/2：无差异。
- workflow-off-fixed/1：无差异。
- fixed-knowledge/1：无差异。
- fixed-knowledge/2：无差异。
- fixed-knowledge-directory-gate/1：无差异。
- mixed-named-directory-gate/1：无差异。
- mixed-named-directory-gate/2：无差异。
- screen-new-and-replace/1：无差异。
- screen-new-and-replace/2：`/after/product_pending/applied_defaults`, `/after/product_pending/securities`, `/after/product_pending/stage`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- screen-new-and-replace/3：`/before/product_pending/applied_defaults`, `/before/product_pending/securities`, `/before/product_pending/stage`。
- screen-explicit-interval/1：无差异。
- screen-no-source/1：无差异。
- edits-no-list/1：无差异。
- edits-no-list/2：无差异。
- edits-no-list/3：无差异。
- edits-refresh-sample/1：无差异。
- edits-refresh-sample/2：无差异。
- edits-refresh-sample/3：无差异。
- edits-refresh-sample/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- edits-empty-list/1：无差异。
- edits-empty-list/2：无差异。
- edits-empty-list/3：无差异。
- edit-company-lookalikes/1：无差异。
- edit-company-lookalikes/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- edit-company-lookalikes/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- ordinal-no-list/1：无差异。
- ordinal-selections/1：无差异。
- ordinal-selections/2：`/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- ordinal-selections/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- named-time-ordinals/1：无差异。
- named-time-ordinals/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- named-time-ordinals/3：`/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- named-time-ordinals/4：无差异。
- named-time-ordinals/5：无差异。
- named-time-ordinals/6：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-pending-owns-replies/1：无差异。
- company-pending-owns-replies/2：`/after/product_pending/applied_defaults`, `/after/product_pending/securities`, `/after/product_pending/stage`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-pending-owns-replies/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_pending/applied_defaults`, `/before/product_pending/securities`, `/before/product_pending/stage`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-pending-owns-replies/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- company-pending-owns-replies/5：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- company-pending-owns-replies/6：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-pending/1：无差异。
- technical-pending/2：`/after/product_pending/applied_defaults`, `/after/product_pending/securities`, `/after/product_pending/stage`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-pending/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_pending/applied_defaults`, `/before/product_pending/securities`, `/before/product_pending/stage`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-pending/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- technical-pending/5：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- indicator-pending/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- indicator-pending/2：`/after/product_pending/applied_defaults`, `/after/product_pending/edit/plan/applied_defaults`, `/after/product_pending/edit/plan/context_created_at`, `/after/product_pending/edit/plan/context_expires_at`, `/after/product_pending/securities`, `/after/product_pending/stage`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- indicator-pending/3：`/after/product_pending/applied_defaults`, `/after/product_pending/edit/plan/applied_defaults`, `/after/product_pending/edit/plan/context_created_at`, `/after/product_pending/edit/plan/context_expires_at`, `/after/product_pending/securities`, `/after/product_pending/stage`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_pending/applied_defaults`, `/before/product_pending/edit/plan/applied_defaults`, `/before/product_pending/edit/plan/context_created_at`, `/before/product_pending/edit/plan/context_expires_at`, `/before/product_pending/securities`, `/before/product_pending/stage`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- indicator-pending/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_pending/applied_defaults`, `/before/product_pending/edit/plan/applied_defaults`, `/before/product_pending/edit/plan/context_created_at`, `/before/product_pending/edit/plan/context_expires_at`, `/before/product_pending/securities`, `/before/product_pending/stage`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-technical-capabilities/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-technical-capabilities/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-technical-capabilities/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-technical-capabilities/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-disabled/1：无差异。
- company-disabled/2：无差异。
- company-disabled/3：无差异。
- knowledge-does-not-renew-list/1：无差异。
- knowledge-does-not-renew-list/2：无差异。
- knowledge-does-not-renew-list/3：无差异。
- knowledge-does-not-renew-list/4：无差异。
- current-pronoun-versus-list/1：无差异。
- current-pronoun-versus-list/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- current-pronoun-versus-list/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- current-pronoun-versus-list/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-followups-and-history/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-followups-and-history/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-followups-and-history/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-followups-and-history/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- company-followups-and-history/5：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-edit-and-commands/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-edit-and-commands/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-edit-and-commands/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- technical-edit-and-commands/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- technical-edit-and-commands/5：`/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`。
- replay-and-isolation/1：无差异。
- replay-and-isolation/2：无差异。
- replay-and-isolation/3：无差异。
- replay-and-isolation/4：无差异。
- source-change/1：无差异。
- source-change/2：无差异。
- lazy-clock-explicit-ordinal/1：无差异。
- lazy-clock-explicit-ordinal/2：无差异。
- lazy-clock-named-member/1：无差异。
- lazy-clock-named-member/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- selected-technical-followup/1：无差异。
- selected-technical-followup/2：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- selected-technical-followup/3：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- selected-technical-followup/4：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- failed-research-consumes-current/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- failed-research-consumes-current/2：`/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- failed-research-consumes-current/3：无差异。
- failed-research-consumes-current/4：无差异。
- cancelled-selection/1：无差异。
- cancelled-selection/2：无差异。
- cancelled-selection/3：无差异。
- development-mode/1：无差异。
- development-mode/2：无差异。
- development-mode/3：无差异。
- decision-direct-and-history/1：期限状态变化 ('decision_horizon_required', 1, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved`, `/after/records/turn-1/result_code`, `/calls/company_reads`, `/calls/daily_reads`, `/calls/model_forwards`, `/events`, `/outcome/response/result/analyses_used`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/model_calls_attempted`, `/outcome/response/result/parameters`, `/outcome/response/result/runtime/responses_received`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-direct-and-history/2：期限状态变化 ('product_result_unavailable', 0, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved`, `/after/records/turn-1/result_code`, `/after/records/turn-2/result_code`, `/before/model_calls_forwarded`, `/before/product_pending`, `/before/product_saved`, `/before/records/turn-1/result_code`, `/events`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/parameters`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-paraphrases/1：`/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- decision-paraphrases/2：期限状态变化 ('decision_horizon_required', 1, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/kinds`, `/after/product_saved/plan/objective/horizon`, `/after/product_saved/plan/objective/kind`, `/after/records/turn-2/result_code`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/calls/company_reads`, `/calls/daily_reads`, `/calls/model_forwards`, `/events`, `/outcome/response/result/analyses_used`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/model_calls_attempted`, `/outcome/response/result/parameters`, `/outcome/response/result/runtime/responses_received`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-paraphrases/3：期限状态变化 ('decision_horizon_required', 0, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/kinds`, `/after/product_saved/plan/objective/horizon`, `/after/product_saved/plan/objective/kind`, `/after/records/turn-2/result_code`, `/after/records/turn-3/result_code`, `/before/model_calls_forwarded`, `/before/product_pending`, `/before/product_saved/answer/full_json/sha256`, `/before/product_saved/answer/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/kinds`, `/before/product_saved/plan/objective/horizon`, `/before/product_saved/plan/objective/kind`, `/before/records/turn-2/result_code`, `/calls/company_reads`, `/calls/daily_reads`, `/calls/model_forwards`, `/events`, `/outcome/response/result/analyses_used`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/model_calls_attempted`, `/outcome/response/result/parameters`, `/outcome/response/result/runtime/responses_received`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-paraphrases/4：期限状态变化 ('decision_horizon_required', 0, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/kinds`, `/after/product_saved/plan/objective/horizon`, `/after/product_saved/plan/objective/kind`, `/after/records/turn-2/result_code`, `/after/records/turn-3/result_code`, `/after/records/turn-4/result_code`, `/before/model_calls_forwarded`, `/before/product_pending`, `/before/product_saved/answer/full_json/sha256`, `/before/product_saved/answer/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/kinds`, `/before/product_saved/plan/objective/horizon`, `/before/product_saved/plan/objective/kind`, `/before/records/turn-2/result_code`, `/before/records/turn-3/result_code`, `/calls/company_reads`, `/calls/daily_reads`, `/calls/model_forwards`, `/events`, `/outcome/response/result/analyses_used`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/model_calls_attempted`, `/outcome/response/result/parameters`, `/outcome/response/result/runtime/responses_received`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-identity-boundary/1：无差异。
- decision-identity-boundary/2：期限状态变化 ('decision_horizon_required', 1, 0)；`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved`, `/after/records/turn-2/result_code`, `/calls/company_reads`, `/calls/daily_reads`, `/calls/model_forwards`, `/events`, `/outcome/response/result/analyses_used`, `/outcome/response/result/code`, `/outcome/response/result/company_research`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`, `/outcome/response/result/model_calls_attempted`, `/outcome/response/result/parameters`, `/outcome/response/result/runtime/responses_received`, `/outcome/response/result/stage`, `/outcome/response/result/status`。
- decision-identity-boundary/3：`/after/model_calls_forwarded`, `/after/product_pending`, `/after/product_saved`, `/after/records/turn-2/result_code`, `/before/model_calls_forwarded`, `/before/product_pending`, `/before/product_saved`, `/before/records/turn-2/result_code`, `/events`。
- decision-domain-limits/1：`/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/objective/horizon`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- decision-domain-limits/2：`/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/objective/horizon`, `/before/product_saved/answer/full_json/sha256`, `/before/product_saved/answer/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/objective/horizon`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
- decision-domain-limits/3：`/after/product_saved/answer/full_json/sha256`, `/after/product_saved/answer/full_json/utf8_bytes`, `/after/product_saved/plan/full_json/sha256`, `/after/product_saved/plan/full_json/utf8_bytes`, `/after/product_saved/plan/objective/horizon`, `/before/product_saved/answer/full_json/sha256`, `/before/product_saved/answer/full_json/utf8_bytes`, `/before/product_saved/plan/full_json/sha256`, `/before/product_saved/plan/full_json/utf8_bytes`, `/before/product_saved/plan/objective/horizon`, `/outcome/response/result/full_json/sha256`, `/outcome/response/result/full_json/utf8_bytes`。
