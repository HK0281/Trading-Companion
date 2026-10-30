# 技术研究共用流程：v5 → v6 的117轮差异

v1～v5、A2/A3差异及三个原观察器保持原字节内容（比较时仅统一换行）。v6以v5有效投影为父，完整记录每个字段的before/after；只替换UUID，不忽略文本、数组、计数或状态。

独立目标见 `tests/fixtures/request-routing-technical-expected-v1.json`（实现前编写）；额外调用/路由契约见 `tests/routing_technical_support.py`。原固定模型已经支持共用工具，本次不修改三个原观察器。

下列每轮列出所有变化路径；未变化轮次也保留。每条路径的准确旧值和新值位于 `tests/fixtures/request-routing-v6.json` 的对应场景/轮次 `fields`，其中 `present` 区分缺键和空值。`full_json` 是完整内容的SHA-256摘要及UTF-8字节数，原观察器保留完整诊断。

本批共有31/117轮出现差异，共382条字段变化；其余86轮逐字段不变。
## knowledge-ready

- 第1轮：无差异。
- 第2轮：无差异。

## knowledge-directory-missing

- 第1轮：无差异。

## knowledge-stale

- 第1轮：无差异。
- 第2轮：无差异。

## workflow-off

- 第1轮：无差异。
- 第2轮：无差异。

## workflow-off-fixed

- 第1轮：无差异。

## fixed-knowledge

- 第1轮：无差异。
- 第2轮：无差异。

## fixed-knowledge-directory-gate

- 第1轮：无差异。

## mixed-named-directory-gate

- 第1轮：无差异。
- 第2轮：无差异。

## screen-new-and-replace

- 第1轮：无差异。
- 第2轮：2个字段。
  - `/after/product_pending/edit`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
  - `/after/product_pending/scope`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
- 第3轮：2个字段。
  - `/before/product_pending/edit`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_pending/scope`：上一轮保存状态向本轮传播；数据和有效期不延长。

## screen-explicit-interval

- 第1轮：无差异。

## screen-no-source

- 第1轮：无差异。

## edits-no-list

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## edits-refresh-sample

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：4个字段。
  - `/after/product_saved/plan/full_json/sha256`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/product_saved/plan/full_json/utf8_bytes`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。

## edits-empty-list

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## edit-company-lookalikes

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## ordinal-no-list

- 第1轮：无差异。

## ordinal-selections

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：4个字段。
  - `/after/product_saved/plan/full_json/sha256`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/product_saved/plan/full_json/utf8_bytes`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。

## named-time-ordinals

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。
- 第5轮：无差异。
- 第6轮：无差异。

## company-pending-owns-replies

- 第1轮：无差异。
- 第2轮：2个字段。
  - `/after/product_pending/edit`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
  - `/after/product_pending/scope`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
- 第3轮：2个字段。
  - `/before/product_pending/edit`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_pending/scope`：上一轮保存状态向本轮传播；数据和有效期不延长。
- 第4轮：无差异。
- 第5轮：无差异。
- 第6轮：无差异。

## technical-pending

- 第1轮：无差异。
- 第2轮：12个字段。
  - `/after/pending`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/product_pending`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/pending`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/state`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第3轮：23个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-3/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/state`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第4轮：14个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-3/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-3/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
- 第5轮：12个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-3/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-3/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。

## indicator-pending

- 第1轮：17个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第2轮：22个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/pending_edit`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/product_pending`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/pending`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/state`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第3轮：20个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/pending_edit`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/product_pending`：范围约束与编辑上下文进入共用待澄清计划；默认字段也如实序列化。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/pending`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/state`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/pending_edit`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/state`：上一轮保存状态向本轮传播；数据和有效期不延长。
- 第4轮：29个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/market_contexts/<uuid-2>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-4/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/pending_edit`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/pending`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/state`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。

## company-technical-capabilities

- 第1轮：无差异。
- 第2轮：17个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第3轮：11个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
- 第4轮：9个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。

## company-disabled

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## knowledge-does-not-renew-list

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。

## current-pronoun-versus-list

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。

## company-followups-and-history

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。
- 第5轮：无差异。

## technical-edit-and-commands

- 第1轮：17个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第2轮：25个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/calls/daily_reads`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/analyses_used`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/parameters`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/status`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第3轮：14个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
- 第4轮：24个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/request_id`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/parameters/indicators`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第5轮：10个字段。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-1/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-1/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。

## replay-and-isolation

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。

## source-change

- 第1轮：无差异。
- 第2轮：无差异。

## lazy-clock-explicit-ordinal

- 第1轮：无差异。
- 第2轮：无差异。

## lazy-clock-named-member

- 第1轮：无差异。
- 第2轮：无差异。

## selected-technical-followup

- 第1轮：无差异。
- 第2轮：16个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
- 第3轮：25个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-3/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/confirmed`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/calls/daily_reads`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/analyses_used`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/parameters`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/status`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第4轮：16个字段。
  - `/after/market_contexts/<uuid-1>`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/product_saved/plan/full_json/sha256`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/product_saved/plan/full_json/utf8_bytes`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-3/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/technical/saved_result`：新技术任务保存在共用载体；旧技术状态不再被本轮写入。
  - `/before/market_contexts/<uuid-1>`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-3/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/confirmed`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/technical/saved_result`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。

## failed-research-consumes-current

- 第1轮：无差异。
- 第2轮：13个字段。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/calls/model_forwards`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/events`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/executor_chain`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
  - `/outcome/response/result/code`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/company_research`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/model_calls_attempted`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/runtime/responses_received`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/stage`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/route`：自然技术改用共用执行器：首次理解和合成各一次，编辑/身份确认后仅合成一次；保持取数范围。
- 第3轮：4个字段。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。
- 第4轮：4个字段。
  - `/after/model_calls_forwarded`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/after/records/turn-2/result_code`：共用执行后的预算、当前主体或保存状态变化；失败不恢复旧研究。
  - `/before/model_calls_forwarded`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/records/turn-2/result_code`：上一轮保存状态向本轮传播；数据和有效期不延长。

## cancelled-selection

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## development-mode

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## decision-direct-and-history

- 第1轮：无差异。
- 第2轮：无差异。

## decision-paraphrases

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。
- 第4轮：无差异。

## decision-identity-boundary

- 第1轮：无差异。
- 第2轮：无差异。
- 第3轮：无差异。

## decision-domain-limits

- 第1轮：4个字段。
  - `/after/product_saved/plan/full_json/sha256`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/product_saved/plan/full_json/utf8_bytes`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
- 第2轮：6个字段。
  - `/after/product_saved/plan/full_json/sha256`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/after/product_saved/plan/full_json/utf8_bytes`：保存程序范围约束；严格指标不补ATR及额外均线，按原要求保留日期/证券。
  - `/before/product_saved/plan/full_json/sha256`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved/plan/full_json/utf8_bytes`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/outcome/response/result/full_json/sha256`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
  - `/outcome/response/result/full_json/utf8_bytes`：v3自然正文及范围/排除说明改变输出；缺项仍保留，参考计划独立报告不可用。
- 第3轮：2个字段。
  - `/before/product_saved/plan/full_json/sha256`：上一轮保存状态向本轮传播；数据和有效期不延长。
  - `/before/product_saved/plan/full_json/utf8_bytes`：上一轮保存状态向本轮传播；数据和有效期不延长。

## 原40场景评估的版本差异

`data/evaluation/research-api-technical-delta-v1.json` 以原语料和B差异文件的摘要为父，仅覆盖C02两步、C03三步、D02一步的执行器、澄清所有者、结果码和离线脚本阶段；问题、股票、日期、指标、缺历史条件、质量与安全评分要求不变。共59步全量执行，模型调用由72降至68。

原20题决策评估仍独立执行，不改题库、期限、评分或固定分词；技术准备改走共用流程使调用从46降至44。真实模型评估和新增10题属于第二批，本批未执行。
