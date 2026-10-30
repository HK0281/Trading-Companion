# 批次B：117轮路由与状态差异

人工预期先写入[预期表](../tests/fixtures/request-routing-b-expectations.json)，然后才实施B并捕获候选。基于提交1a8fd77的v4+A2+A3，原v1～v4、A2/A3和原观察器均未重录。
39场景、105+12共117轮：7轮变化，110轮逐字段严格相等；7轮共132处字段差异。完整旧值/新值及所有零差异轮在[v5夹具](../tests/fixtures/request-routing-v5.json)。父文件和人工预期文件使用LF规范化摘要锁定。
唯一新增澄清行为是接收有界序号与合法条件；新内部对象记录不加入旧公开序列化。以下逐轮同时说明后续影响，不能把缓存摘要传播算成新的路由变化。

| 场景/轮次 | 原因及连带影响 |
| --- | --- |
| company-pending-owns-replies / 3 | 序号唯一选择原候选，完成旧公司研究 |
| company-pending-owns-replies / 4 | 原候选研究消费旧名单，筛选编辑不再被旧澄清截获 |
| company-pending-owns-replies / 5 | 前轮已无旧pending；固定知识只读 |
| company-pending-owns-replies / 6 | 原pending已在第3轮完成；具名对象按新公司研究 |
| technical-pending / 3 | 序号唯一选择原技术候选 |
| technical-pending / 4 | 前轮研究成功后的状态传播；固定知识不刷新研究对象 |
| technical-pending / 5 | 原pending已完成；裸明确名称按新公司研究 |

调查结论：7轮均在预先标记的公司/技术pending场景中。company第5轮与technical第4轮仅携带前轮结果/计数状态；company第6轮与technical第5轮不再恢复已在第3轮完成的旧任务。初始人工预期还将indicator-pending第3/4轮标为变化：实施中核查既有知识插问契约后，决定保留知识不消费待澄清的语义，因此这两轮最终严格相等。原人工预期表保留，不能按候选输出倒填；这项主动纠正与完整新研究成功替换旧pending分开。
company-followups-and-history第5轮在预期阶段重点核查，但实际无变化：历史查看仍不重新取数。其他场景无未知差异，失败研究仍不把旧数值重新当当前结果。
另一个独立工作流语料research-api-v1保持原文，仅[版本化B差异](../data/evaluation/research-api-b-delta-v1.json)覆盖C05第2/3步：第一个候选完成原基本面请求，随后裸名称启动新公司研究。覆盖层核对父摘要及完整旧步骤，报告写入父/有效语料与差异摘要及规则版本，跨版本比较不相容。40场景59步保留，固定调用从70增至72；2a的304题及其manifest完全未改。

## 逐字段清单

下表长JSON为摘要，完整有序值见同场景/轮次夹具。回答指纹变化来自本轮重新执行计划和实际股票/资料/状态，不是改了A3回答核对器。

### company-pending-owns-replies 第3轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/model_calls_forwarded` | 2 | 3 |
| `/after/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":["000001.SZ","601318.SH"],"c…（完整值见夹具） | null |
| `/after/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/after/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/after/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/calls/company_reads` | 0 | 1 |
| `/calls/model_forwards` | 0 | 1 |
| `/events` | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"at":"2026-09-26T12:00:00+08:00","event":"directory_load","mode":"ready"},{"…（完整值见夹具） | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"event":"directory_load","at":"2026-09-26T12:00:00+08:00","mode":"ready"},{"…（完整值见夹具） |
| `/outcome/response/result/code` | "product_explicit_selection_required" | "company_research_completed" |
| `/outcome/response/result/company_research` | {"present":false,"value":null} | {"present":true,"value":{"securities":[{"instrument_id":"000001.SZ","name":"平安银行"}],"daily_cutoff":"2026-09-25","explicit_historical":fal…（完整值见夹具） |
| `/outcome/response/result/full_json/sha256` | "fff2e31e23f27f16fef96a591c99983b3091edd0179d72a0e7eb0ccf3f13a308" | "e29165f37fa76e145e2dc8e48ee0eed2d65f01bf47dd04e150a665b231b15fda" |
| `/outcome/response/result/full_json/utf8_bytes` | 1245 | 5116 |
| `/outcome/response/result/model_calls_attempted` | 0 | 1 |
| `/outcome/response/result/runtime/responses_received` | 0 | 1 |
| `/outcome/response/result/stage` | "clarification" | "company_research" |
| `/outcome/response/result/status` | "needs_clarification" | "completed" |

### company-pending-owns-replies 第4轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":[],"created_at":"2026-09-26T…（完整值见夹具） | null |
| `/after/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/after/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/after/records/turn-4/result_code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/after/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/before/model_calls_forwarded` | 2 | 3 |
| `/before/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":["000001.SZ","601318.SH"],"c…（完整值见夹具） | null |
| `/before/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/before/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/before/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/calls/model_forwards` | 1 | 0 |
| `/events` | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"at":"2026-09-26T12:00:00+08:00","event":"directory_load","mode":"ready"},{"…（完整值见夹具） | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"event":"directory_load","at":"2026-09-26T12:00:00+08:00","mode":"ready"},{"…（完整值见夹具） |
| `/executor_chain` | ["company_research"] | ["screening_edit"] |
| `/outcome/response/result/code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/outcome/response/result/full_json/sha256` | "78b09bd7ebce93b402f1578712552e664bbe25c5f22ec5db93306cf131abc46d" | "779d49ec9a5a4e5b50c9093a55190d3a97e5b8115f48956001d4d50d6e6ef003" |
| `/outcome/response/result/full_json/utf8_bytes` | 1135 | 1118 |
| `/outcome/response/result/model_calls_attempted` | 1 | 0 |
| `/outcome/response/result/runtime/responses_received` | 1 | 0 |
| `/outcome/response/result/stage` | "clarification" | "screening" |
| `/route` | "company_research" | "screening_edit" |

### company-pending-owns-replies 第5轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":[],"created_at":"2026-09-26T…（完整值见夹具） | null |
| `/after/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/after/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/after/records/turn-4/result_code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/after/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/before/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":[],"created_at":"2026-09-26T…（完整值见夹具） | null |
| `/before/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/before/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/before/records/turn-4/result_code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/before/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |

### company-pending-owns-replies 第6轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/model_calls_forwarded` | 4 | 5 |
| `/after/product_saved/answer/full_json/sha256` | "3ce836ba333b26b35ec057e1b3d7815f900ee6dbde585ad88f95b5ab52187c5e" | "68b8ecb7fbce73ad0266d705a1ba020c73c61652da8d05a7d32c6ed258580fe0" |
| `/after/product_saved/answer/full_json/utf8_bytes` | 3202 | 51917 |
| `/after/product_saved/plan/full_json/sha256` | "1dd509cfe1c4f62a13e1c4e040dfa9e617a59b594c598856a3ba2252787aeb41" | "0da2389824228b88b6fc1c2ce944ff8b64a750c428da74b75f2c11ffda10497d" |
| `/after/product_saved/plan/full_json/utf8_bytes` | 943 | 978 |
| `/after/product_saved/plan/kinds` | ["profile","financial"] | ["profile","financial","news","announcements","technical"] |
| `/after/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/after/records/turn-4/result_code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/before/product_pending` | {"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"bindings":[],"candidate_ids":[],"created_at":"2026-09-26T…（完整值见夹具） | null |
| `/before/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/before/records/turn-3/result_code` | "product_explicit_selection_required" | "company_research_completed" |
| `/before/records/turn-4/result_code` | "missing_product_instrument" | "screening_list_unavailable" |
| `/before/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/calls/daily_reads` | 0 | 1 |
| `/calls/model_forwards` | 1 | 2 |
| `/events` | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"at":"2026-09-26T12:00:00+08:00","event":"directory_load","mode":"ready"},{"…（完整值见夹具） | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"event":"directory_load","at":"2026-09-26T12:00:00+08:00","mode":"ready"},{"…（完整值见夹具） |
| `/outcome/response/result/analyses_used` | 0 | 1 |
| `/outcome/response/result/full_json/sha256` | "e29165f37fa76e145e2dc8e48ee0eed2d65f01bf47dd04e150a665b231b15fda" | "4698b118eeb447913e8d338992b713a154fbb1f81d8a2084c418839fb42b613b" |
| `/outcome/response/result/full_json/utf8_bytes` | 5116 | 107775 |
| `/outcome/response/result/model_calls_attempted` | 1 | 2 |
| `/outcome/response/result/parameters` | null | {"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":20},{"kind":"sma","…（完整值见夹具） |
| `/outcome/response/result/runtime/responses_received` | 1 | 2 |

### technical-pending 第3轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/market_contexts/<uuid-1>` | {"present":false,"value":null} | {"present":true,"value":{"version":"research-market-context-v1","status":"unavailable","code":"context_not_configured"}} |
| `/after/model_calls_forwarded` | 2 | 4 |
| `/after/pending` | {"candidates":[{"aliases":["平安"],"instrument_id":"000001.SZ","name":"平安银行"},{"aliases":["平安"],"instrument_id":"601318.SH","name":"中国平安"}]…（完整值见夹具） | null |
| `/after/records/turn-3/result_code` | "clarification_reply_not_specific" | "research_completed" |
| `/after/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/after/technical/confirmed` | null | {"parameters":{"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":3}],"…（完整值见夹具） |
| `/after/technical/pending` | {"candidate_count":2,"candidates":[{"instrument_id":"000001.SZ","name":"平安银行"},{"instrument_id":"601318.SH","name":"中国平安"}],"candidates_t…（完整值见夹具） | null |
| `/after/technical/saved_result` | null | {"request_id":"<uuid-1>","security":{"instrument_id":"000001.SZ","name":"平安银行"},"start_date":"2025-09-26","as_of_date":"2026-09-25","late…（完整值见夹具） |
| `/after/technical/state` | "awaiting_instrument" | "idle" |
| `/calls/daily_reads` | 0 | 1 |
| `/calls/model_forwards` | 0 | 2 |
| `/events` | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"at":"2026-09-26T12:00:00+08:00","event":"directory_load","mode":"ready"},{"…（完整值见夹具） | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"event":"directory_load","at":"2026-09-26T12:00:00+08:00","mode":"ready"},{"…（完整值见夹具） |
| `/outcome/response/result/analyses_used` | 0 | 1 |
| `/outcome/response/result/code` | "clarification_reply_not_specific" | "research_completed" |
| `/outcome/response/result/full_json/sha256` | "3623e38ebee75eb61298751408438532da155b38932bd25c701e284388fc0616" | "8ae59dc99bdf63a2cc87a1fa06dcc682de4088b7d1d935b6a88a7af280df67fc" |
| `/outcome/response/result/full_json/utf8_bytes` | 1349 | 20984 |
| `/outcome/response/result/model_calls_attempted` | 0 | 2 |
| `/outcome/response/result/parameters` | null | {"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":3}],"source":"tence…（完整值见夹具） |
| `/outcome/response/result/runtime/responses_received` | 0 | 2 |
| `/outcome/response/result/stage` | "clarification" | "explanation" |
| `/outcome/response/result/status` | "needs_clarification" | "completed" |

### technical-pending 第4轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/market_contexts/<uuid-1>` | {"present":false,"value":null} | {"present":true,"value":{"version":"research-market-context-v1","status":"unavailable","code":"context_not_configured"}} |
| `/after/model_calls_forwarded` | 2 | 4 |
| `/after/pending` | {"candidates":[{"aliases":["平安"],"instrument_id":"000001.SZ","name":"平安银行"},{"aliases":["平安"],"instrument_id":"601318.SH","name":"中国平安"}]…（完整值见夹具） | null |
| `/after/records/turn-3/result_code` | "clarification_reply_not_specific" | "research_completed" |
| `/after/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/after/technical/confirmed` | null | {"parameters":{"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":3}],"…（完整值见夹具） |
| `/after/technical/pending` | {"candidate_count":2,"candidates":[{"instrument_id":"000001.SZ","name":"平安银行"},{"instrument_id":"601318.SH","name":"中国平安"}],"candidates_t…（完整值见夹具） | null |
| `/after/technical/saved_result` | null | {"request_id":"<uuid-1>","security":{"instrument_id":"000001.SZ","name":"平安银行"},"start_date":"2025-09-26","as_of_date":"2026-09-25","late…（完整值见夹具） |
| `/after/technical/state` | "awaiting_instrument" | "idle" |
| `/before/market_contexts/<uuid-1>` | {"present":false,"value":null} | {"present":true,"value":{"version":"research-market-context-v1","status":"unavailable","code":"context_not_configured"}} |
| `/before/model_calls_forwarded` | 2 | 4 |
| `/before/pending` | {"candidates":[{"aliases":["平安"],"instrument_id":"000001.SZ","name":"平安银行"},{"aliases":["平安"],"instrument_id":"601318.SH","name":"中国平安"}]…（完整值见夹具） | null |
| `/before/records/turn-3/result_code` | "clarification_reply_not_specific" | "research_completed" |
| `/before/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/before/technical/confirmed` | null | {"parameters":{"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":3}],"…（完整值见夹具） |
| `/before/technical/pending` | {"candidate_count":2,"candidates":[{"instrument_id":"000001.SZ","name":"平安银行"},{"instrument_id":"601318.SH","name":"中国平安"}],"candidates_t…（完整值见夹具） | null |
| `/before/technical/saved_result` | null | {"request_id":"<uuid-1>","security":{"instrument_id":"000001.SZ","name":"平安银行"},"start_date":"2025-09-26","as_of_date":"2026-09-25","late…（完整值见夹具） |
| `/before/technical/state` | "awaiting_instrument" | "idle" |

### technical-pending 第5轮

| 字段路径 | 旧值 | 新值 |
| --- | --- | --- |
| `/after/model_calls_forwarded` | 4 | 6 |
| `/after/product_saved` | null | {"saved_at":"2026-09-26T12:00:00+08:00","plan":{"status":"ready","code":"product_plan_ready","securities":[{"instrument_id":"000001.SZ","…（完整值见夹具） |
| `/after/records/turn-3/result_code` | "clarification_reply_not_specific" | "research_completed" |
| `/after/records/turn-5/result_code` | "research_completed" | "company_research_completed" |
| `/after/technical/confirmed` | {"conditions":{"applied_defaults":["cutoff:previous_shanghai_day","range:365_calendar_days"],"as_of_date":"2026-09-25","indicators":[{"ki…（完整值见夹具） | null |
| `/after/technical/saved_result/is_current` | true | false |
| `/before/market_contexts/<uuid-1>` | {"present":false,"value":null} | {"present":true,"value":{"version":"research-market-context-v1","status":"unavailable","code":"context_not_configured"}} |
| `/before/model_calls_forwarded` | 2 | 4 |
| `/before/pending` | {"candidates":[{"aliases":["平安"],"instrument_id":"000001.SZ","name":"平安银行"},{"aliases":["平安"],"instrument_id":"601318.SH","name":"中国平安"}]…（完整值见夹具） | null |
| `/before/records/turn-3/result_code` | "clarification_reply_not_specific" | "research_completed" |
| `/before/screening_saved` | {"expires_at":"2026-09-26T12:15:00+08:00","is_fixture":false,"request":{"conditions":[{"field":"price","operator":"gt","value":0}],"limit…（完整值见夹具） | null |
| `/before/technical/confirmed` | null | {"parameters":{"instrument_id":"000001.SZ","start_date":"2025-09-26","as_of_date":"2026-09-25","indicators":[{"kind":"sma","window":3}],"…（完整值见夹具） |
| `/before/technical/pending` | {"candidate_count":2,"candidates":[{"instrument_id":"000001.SZ","name":"平安银行"},{"instrument_id":"601318.SH","name":"中国平安"}],"candidates_t…（完整值见夹具） | null |
| `/before/technical/saved_result` | null | {"request_id":"<uuid-1>","security":{"instrument_id":"000001.SZ","name":"平安银行"},"start_date":"2025-09-26","as_of_date":"2026-09-25","late…（完整值见夹具） |
| `/before/technical/state` | "awaiting_instrument" | "idle" |
| `/calls/company_reads` | 0 | 1 |
| `/events` | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"at":"2026-09-26T12:00:00+08:00","event":"directory_load","mode":"ready"},{"…（完整值见夹具） | [{"event":"wall_clock","value":"2026-09-26T12:00:00+08:00"},{"event":"directory_load","at":"2026-09-26T12:00:00+08:00","mode":"ready"},{"…（完整值见夹具） |
| `/executor_chain` | ["technical_research"] | ["company_research"] |
| `/outcome/response/result/code` | "research_completed" | "company_research_completed" |
| `/outcome/response/result/company_research` | {"present":false,"value":null} | {"present":true,"value":{"securities":[{"instrument_id":"000001.SZ","name":"平安银行"}],"daily_cutoff":"2026-09-25","explicit_historical":fal…（完整值见夹具） |
| `/outcome/response/result/full_json/sha256` | "8ae59dc99bdf63a2cc87a1fa06dcc682de4088b7d1d935b6a88a7af280df67fc" | "452f14cddaea550cc58dce3363a98af3bf80f6bd1fbdae009b7bddadb5771d2e" |
| `/outcome/response/result/full_json/utf8_bytes` | 20984 | 107775 |
| `/outcome/response/result/parameters/indicators` | [{"kind":"sma","window":3}] | [{"kind":"sma","window":20},{"kind":"sma","window":60},{"kind":"ema","window":20},{"kind":"macd","fast":12,"slow":26,"signal":9},{"kind":…（完整值见夹具） |
| `/outcome/response/result/stage` | "explanation" | "company_research" |
| `/route` | "technical_research" | "company_research" |

新增对象发布顺序、取消/失败、多对象、候选附带日期与D05/D20在独立专项覆盖，不修改旧题来掩盖新增要求。
B固定离线20题结果为20/20、46次模型转发；D05/D20经过实际ResearchApiService.message入口，检查对象、期限、评级、依据及准备上下文。这是固定模型/人工数据的协议验证，不是真实模型质量。
2a使用contextual-routing-b-v1规则版本，旧阈值锁不可套用。附录未完成项见[处置表](request-routing-b-disposition.md)。
