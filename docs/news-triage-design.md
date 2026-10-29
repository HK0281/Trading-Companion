# Jev 新闻标题分诊：独立评估第一步

日期：2026-10-28。状态：已实现离线接线和自测，工作区收尾待 Claude 复审；Claude 已在 #128 逐条复核72条标注，题库状态为 reviewed。没有真实模型调用或效果数字，不接入公司研究、聊天或路由。

## 目的与边界

System 1 / System 2 是本实验对快速分类和综合推理的分工称呼，不是已经验证的能力结论。Jev 对一条标题做三个选择题，DeepSeek 用相同定义给标签；以人工参考答案评估，另有不读取答案的简单规则基线。只有后续实验支持时，才考虑让便宜分类器直接采用、低把握时交给综合模型。

第一步只提供评估。后续第二步的产品影子接入、第三步的内容过滤/标注和意图路由实验都没有实现。语气标签表示标题对目标公司的措辞倾向，不证明新闻真实性、投资价值或未来价格，不是交易信号。

## 题目与标注

[题目定义](../src/trading_companion/news_triage.py)版本 `news-triage-v1`，三个问题的 criteria 和共用说明同时供 Jev、DeepSeek 与人工复核使用；报告保存定义摘要、提示模板及代码摘要。

- relevance：primary（目标为主体）、mentioned（顺带提及或整体文章）、unrelated（同名/错配）。
- event_type：earnings、capital、governance、regulatory、operations、market、industry、other。按标题实际主事件分类，即使不相关也分类其实际事件。
- tone：positive、neutral、negative，始终针对目标公司；unrelated 必须 neutral。正负并列无主导、例行公告、单纯否认传闻为 neutral。行业情绪不能自动归于被点名的公司。

多事件按明确主线；无主次才按 regulatory > earnings > capital > governance > operations > market > industry > other。否认重组传闻仍为 capital/neutral，重组实际终止和损失另判。完整逐项标准通过运行计划的定义摘要及源码核查。

[合成题库](../data/evaluation/news-triage-v1.jsonl)共 72 条，每条含三个人工标签、理由、难例标签和 `synthetic: true`。配额为：relevance 44/16/12；tone 23/29/20；event_type 依上述顺序为 10/12/8/10/12/8/6/6，每类至少 3 条。难例标签可重叠：错配 12、否认 12、多事件 10、行业顺带提及 14、标题党 6、例行 11、正负混合 8。用户已于2026-10-28确认 N011 的回购用途描述和 N031 的问询程序均按中性标注。

按真实财经标题用法编写，避免在标题中直接声明分类答案。70 条标题含目标全称或代码，符合新浪适配器的标题字符串过滤；误配场景包括全称出现在园区、协会等其他实体名称中，或代码作为信托计划编号。仅 N021、N060 两条近似字题不满足该过滤，理由中明确为可能由人工送入的补充题，不能当作产品新闻流的频率样本。

**规则与题库由同一作者编写，规则分数可能偏高。** 本轮按审核要求改写标题，规则实现保持冻结；标题先定稿再计分，未按规则分数或模型输出挑选标题。报告的 JSON 计划和 Markdown 同时保留此限制，不以压低规则分数为目标。

12 家公司使用自然的虚构名称；990901..990912 仅为本题库字符串，不构造证券身份或请求行情。离线检查固定证券目录；本机存在真实目录时额外核对名称、别名和代码并记录目录摘要/条目数。无本地目录时明确未核对，绝不刷新；这不是对未来全部市场或其他金融品种的无碰撞保证。

[manifest](../data/evaluation/news-triage-v1.manifest.json)记录语料摘要和标注状态。Codex 起草时未参考模型输出；Claude 已于2026-10-28逐条核对72条标注（消息 #128），状态为 reviewed，reviewer为claude；reviewed_at记录日期，user_confirmed记录用户确认的N011、N031及其中性语气。live标注门禁现已满足，plan-only预览显示live_annotation_ready=true；这不代表已经执行真实调用或取得费用授权。后续修题仍应重新复核，有歧义交用户确认，不反向根据模型改答案。

## 命令与授权

在项目根目录，默认完全离线。使用已有虚拟环境，无需新依赖：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_news_triage.py --report-dir .agentbus/tmp/news-triage-offline
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_news_triage.py --item N001 --item N002 --repeat 2 --call-limit 4

# 仅预览：不读取密钥，不联网，不创建报告目录。
.\.venv\Scripts\python.exe -X utf8 -B scripts/evaluate_news_triage.py --mode live --classifier jev --classifier deepseek --deepseek-model MODEL_ID --call-limit 144 --prices data/evaluation/model-prices.json --assumed-input-tokens 800 --assumed-output-tokens 128 --plan-only
```

真实执行须先完成标注复核，再由 Claude 取得用户费用同意。去掉 `--plan-only` 才进入执行；本次没有执行真实命令。live 输出计划且通过标注检查后，才通过 [local_env](../src/trading_companion/local_env.py) 的独立窄函数合并所选密钥：Jev 仅读 `TYPESAFE_API_KEY`，DeepSeek 仅读 `DEEPSEEK_API_KEY`。进程环境优先（包括空值）；缺少时从固定源码根目录 `.env` 提取该变量，不向父目录搜索、不展开变量、不修改全局环境。`PYTHON_DOTENV_DISABLED` 可禁用文件读取。模型名仍由命令显式指定，不加载 `.env` 中的模型设置，也不扩展 `load_model_environment` 的提供方范围。offline 和 plan-only 完全不读 `.env` 或密钥；不打印密钥排错。

参数：mode 为 offline/live；classifier 可重复选 jev/deepseek，live 必须显式给；选择 DeepSeek 必须给 deepseek-model；Jev 请求别名固定 jev-latest。item 选择现有编号，repeat 为 1..3，timeout-seconds 为 1..60（默认 15）。prices 和整次 call-limit 在 live 必填。N 条标题×R 次重复×K 家分类器最多 N×R×K 次；三题合一请求。失败占额度，未运行保留，不重试、不重问、不换源。固定种子 42 打乱执行顺序，规则基线不消耗供应商额度。

计划在密钥读取和客户端构造前输出到 stderr，最终一份合法 JSON 写 stdout。报告目录使用新唯一子目录，不覆盖。退出码：完整执行或预览 0（分类错误仍是有效实验）、失败/未运行/框架问题使 partial 为 1、参数或 draft 禁止真实运行等 invalid_request 为 2。

## 适配器与隔离

[通用 Jev 模块](../src/trading_companion/typesafe_jev.py)不绑定新闻任务，按官方 [System One](https://docs.typesafe.ai/api/system-one)、[错误说明](https://docs.typesafe.ai/api/errors) 接口执行 Choice。只向官方固定 HTTPS 端点 POST，不重定向、不自动重试。model、answers、usage 和答案字段必填；未知元数据丢弃。概率键恰好等于选项集合，范围 0..1、有限且非 bool，总和与 1 相差不超过 0.02；不归一化；选择必须为最大概率项，完全并列时接受任一。confidence 独立检查，不强制等于最大概率。

input/output token 用量为非负有界整数；内部 total 是二者之和，不伪装为供应商另报字段。重复 JSON 键、BOM、非有限数、超深/超大响应拒绝。固定错误码覆盖鉴权、限流、过载、重定向、HTTP 错误、超时、连接和格式失败；不保存错误正文。答案无效时仍保留可单独严格验证的模型/用量，已发生费用不会因分类无效消失。

[DeepSeek 评估接线](../src/trading_companion/news_triage_models.py)复用 DeepSeekConfig 和 parse_chat_response，独立有界 POST；不改研究回答使用的 deepseek_model.py。temperature=0、thinking 关闭、JSON 对象输出、固定输出上限；只接受三个合法标签字符串，拒绝多余标签字段和 Markdown 围栏。它不提供校准概率，confidence/probabilities 为 null。

单次请求采用连接/读取 timeout，流式每块检查单调时钟截止和 64 KiB 解压后响应上限，并关闭响应。DNS、慢速分块等极端情况不保证绝对墙钟上限，也不保证远端停止计费；没有子进程、线程池或“硬中断”的承诺。

offline 的独立假传输只读三字段输入，以固定哈希产生协议响应，不读取 gold，输出故意不全对。用量和置信度为人工脚本，不能解读为真实测量。离线执行清除凭据环境并封锁 DNS/connect；plan-only 不构造客户端。live 仅所选固定 POST 通道可联网，禁止其他模型、行情、新闻外连；被捕获的越界仍导致 partial。

## 指标、费用与级联

[指标实现](../src/trading_companion/news_triage_metrics.py)保留计划分母 N，失败/未运行进入 missing 列；每题准确率 correct/N，另列有效回答准确率与覆盖。三题合计 correct/(3N)，联合准确率为三题全对标题数/N。

每类 F1=2TP/(2TP+FP+FN)，分母 0 时取 0 并列 support；missing 对 gold 类计 FN。每题 macro-F1 为各类均值，合计 macro-F1 对带题目名的 14 类求均值，另列三个问题的等权宏平均。合计混淆矩阵用题目名区分类别，不合并不同任务的标签。

Jev 每题及合计按十个等宽置信度桶统计，最后一桶包含 1；ECE=sum(桶样本数/有效样本数×|桶准确率−桶平均置信度|)。缺置信度不补 0，ECE 显示覆盖，无有效样本为 null。DeepSeek 和规则不适用 ECE。

阈值预先固定 0.50..0.95、间隔 0.05。每题独立给覆盖和选择准确率；整条标题则三题均有效且 confidence 均达到阈值才采用 Jev，否则采用已记录的 DeepSeek 三题。门槛取最小置信度，但它不是“三题全对”的校准概率。没有所需 DeepSeek 结果时保留 missing，不补调用。

同次同时选两家时才有级联模拟，不合并外部报告。模拟成本=全部 Jev 请求费用+需要的 DeepSeek 请求费用，每条三题只算一次；与实际实验已支付两家完整请求的费用分开。串行延迟用已有观测相加，仅为假设，不是生产测量。p50/p95 为最近秩，空样本 null，成功/失败/超时分别列数量，未发请求不补 0。

[价格文件](../data/evaluation/model-prices.json)的 Jev 空条目不代表已核对价格；DeepSeek 也需已核对的价格。只据已报告 token 和 Decimal 单价计算；未知输入、价格、模型身份或缓存分档保留未知，列已知小计和未知请求数。别名可能返回其他实际模型；没有明确适用的核对价格时费用未知，不擅自套价。USD/CNY 分列，不猜汇率、不合加。offline 费用标注为人工用量算例，不是实际开销。

总表和完整报告分别提供范围/日期/模型版本、三题指标、置信度桶、选择曲线、逐条结果、失败、费用覆盖与标注理由。不能在此小型合成集上挑最好阈值就说可以上线；正式上线判断还需独立真实分布样本、稳定性和产品风险约束。

## 验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m pytest tests/test_typesafe_jev.py tests/test_news_triage.py tests/test_news_triage_models.py tests/test_news_triage_metrics.py tests/test_news_triage_evaluation.py -q
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
```

自动验证全部使用模拟传输；包含手算 F1/ECE/级联、失败分母、别名费用未知、缺用量、协议边界、相同输入/定义、gold 隔离、固定顺序、额度、报告不覆盖，以及离线/预览的文件/凭据/外连守卫。真实模型效果和第三方条款/费用授权仍由 Claude 在真实执行前单独确认。
