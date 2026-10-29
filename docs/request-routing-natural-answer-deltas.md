# 自然语言回答 A 批：路由基线逐字段差异

v1/v2/v3、原观察器和路由契约保持原字节。v4 是显式字段覆盖，先核对每个旧值，再应用新值；每一轮仍严格比较完整观察投影。

人工预期：只允许回答接受码 product_research_answered → answer_ready，以及新回答序列化造成的完整回答/结果 SHA-256 和 UTF-8 字节数变化。之前保存的回答会在后续轮次 before/after 中传播。
路由、意图、对象、领域、来源、日期、采集/模型调用次数、执行事件和技术状态均不允许变化。105 轮旧场景加 12 轮决策场景；未列出的轮次字段差异为空。结构修正新增调用另由工作流 R01 专项覆盖，不改变这 117 轮的调用契约。

差异位于 tests/fixtures/request-routing-v4.json（逐字段保存旧值/新值）；原方案中的 B 批路由变化后续另记 v5。

## edits-refresh-sample 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `dece7ecce0420dff0563d4a54cd46b407c18bea1ec07071fe6c3b08c6b8df6fd` | `c22f3828f4950b8eee2dc83f8b2b31aac5067d3dcf6bbae555e7bca1ef8cca86` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/outcome/response/result/full_json/sha256` | `240ba9e7ad8d1d3fd7e516018e1782480bba588e804ca1223eb79c945ebdfc52` | `be9c07838b5f99926d0bef084047fe6665af9f43b5f46df42f2fa6d74be34c7b` |
| `/outcome/response/result/full_json/utf8_bytes` | `3020` | `3706` |

## edit-company-lookalikes 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `1d9b6808627c1dbbe3c71660fbcb7cc94749158998844d9b1fe213bb84d14f09` | `903f5bd750477cf30e88637663f1380cc0957c51732eb587c8745fa059560fab` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `40f4869f5b8bea273ccbb04e7426f9eaa63fe482159744c9b3136aab28e9a3a9` | `317350cf9501c3819e67252bab274db6723bc2512469e53e65cfe825b6207b8b` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## edit-company-lookalikes 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `e61defe16a72744b5d85fc63612b4c28ce10d620b514f3e81d6c9a1a18c638bd` | `b7eaa882b2ac3b8ebd3650aad1b9a79797d7dc7623d0de6b285d476f0efaa7d4` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1852` | `99872` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `1d9b6808627c1dbbe3c71660fbcb7cc94749158998844d9b1fe213bb84d14f09` | `903f5bd750477cf30e88637663f1380cc0957c51732eb587c8745fa059560fab` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `5cc8de91790755b02933ae8735bf23142ea0f774e5120f75d7156903fe724bf8` | `619d51a0e3280d1711cd3eee4ee9b5c501898fd11cbd8865d9d82569bc3d314a` |
| `/outcome/response/result/full_json/utf8_bytes` | `7573` | `105593` |

## ordinal-selections 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `baeb07d4238233568e3f77140dbede205dc7f1b92755487a76788fc6816dc5ca` | `5e1d6295cbc1c5a13d29b715c1a5a289e4e06d5e7bd96c01abc00a4657940f41` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/outcome/response/result/full_json/sha256` | `a97ac77a770f47c6bb7c35cf290db37e41932a44bcf85cdc346e732ffeecba15` | `82dd7f1fe6266942c4aa2adcce2e142cdc0749284145ddcf842be66d77b34707` |
| `/outcome/response/result/full_json/utf8_bytes` | `3020` | `3706` |

## named-time-ordinals 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `1d9b6808627c1dbbe3c71660fbcb7cc94749158998844d9b1fe213bb84d14f09` | `903f5bd750477cf30e88637663f1380cc0957c51732eb587c8745fa059560fab` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `40f4869f5b8bea273ccbb04e7426f9eaa63fe482159744c9b3136aab28e9a3a9` | `317350cf9501c3819e67252bab274db6723bc2512469e53e65cfe825b6207b8b` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## named-time-ordinals 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `1d9b6808627c1dbbe3c71660fbcb7cc94749158998844d9b1fe213bb84d14f09` | `903f5bd750477cf30e88637663f1380cc0957c51732eb587c8745fa059560fab` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |

## named-time-ordinals 第 6 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `1d9b6808627c1dbbe3c71660fbcb7cc94749158998844d9b1fe213bb84d14f09` | `903f5bd750477cf30e88637663f1380cc0957c51732eb587c8745fa059560fab` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `40f4869f5b8bea273ccbb04e7426f9eaa63fe482159744c9b3136aab28e9a3a9` | `317350cf9501c3819e67252bab274db6723bc2512469e53e65cfe825b6207b8b` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## company-pending-owns-replies 第 6 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `edbade52e64ba2494a8f7e641ff08d588e559ffc23a9b9c8da57718ffe960a48` | `2c2eea6bce55d120b8690a075ec71049f797b780c73a2f8a94d2dcc5b26abd39` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## company-technical-capabilities 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `064729c65140dcf02c1cc074a30eb5000c691fa5045f5f42092caa5d5b87beb4` | `38c06cc3d1cc1de8ed80d663bb0920c1ee1e153d036e6941c295a1827be1da41` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## company-technical-capabilities 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |

## company-technical-capabilities 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `960ef1f7477ec81cb5f4d540daeb8f45256ae732a5073ee2c7796b41a0434612` | `a5cf04aba1eaa15d64989c382fe5f1d5cbf5b29b992e240d5714dbc45e7513d7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1337` | `49069` |
| `/outcome/response/result/full_json/sha256` | `1dd87f211ae607c5706ed57818aeeb8043f873666cedae8763f90cf2f801ae2a` | `c1a4bccfb6e108d5f5aff725a39d2731189b67db320880824dfdd058bdaa9fbd` |
| `/outcome/response/result/full_json/utf8_bytes` | `56472` | `104217` |

## company-technical-capabilities 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `1fd21eab86a08af4621dcde7fad2c720eaf19179800c987b66119057cd97d8dd` | `296907a3608d711790584f388ffb9c381d11e3ed8b414b70620207bcbb58c108` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1337` | `43922` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `960ef1f7477ec81cb5f4d540daeb8f45256ae732a5073ee2c7796b41a0434612` | `a5cf04aba1eaa15d64989c382fe5f1d5cbf5b29b992e240d5714dbc45e7513d7` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1337` | `49069` |
| `/outcome/response/result/full_json/sha256` | `a581d4093c81018b063f42b74825e4fc2d618ec7fcab8fc377e13925717aeebe` | `9114db0ca36133c0f1dbebe0682f7fe94cc8f7403d48fd7d776d82eae27d5715` |
| `/outcome/response/result/full_json/utf8_bytes` | `50251` | `92849` |

## current-pronoun-versus-list 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `37bc2bf85278ba6cebafac560efb1e542b0242fb31ffdac50a49ece839baf11c` | `656428bb9fb9053ce3665042554d4e768802761bc87a536973e21fc51da3f14b` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `5925163d4dae5a747296652bf2d7e994b84fa3d20c7280a9fd6e7c47ebf32984` | `dd8312e8653af5934ad945382470b4f39ee8bfa84b4daaeab628c65a314037d8` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## current-pronoun-versus-list 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `baeb07d4238233568e3f77140dbede205dc7f1b92755487a76788fc6816dc5ca` | `5e1d6295cbc1c5a13d29b715c1a5a289e4e06d5e7bd96c01abc00a4657940f41` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `37bc2bf85278ba6cebafac560efb1e542b0242fb31ffdac50a49ece839baf11c` | `656428bb9fb9053ce3665042554d4e768802761bc87a536973e21fc51da3f14b` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `b10ccc591d190f813ea37f11779a33925b918b28230f13e5f4a7f315c25fcf37` | `bb934f33799d19b99d2e36baa5d85d019093b25529138b0595bc95b8bb426ee2` |
| `/outcome/response/result/full_json/utf8_bytes` | `2989` | `3675` |

## current-pronoun-versus-list 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `ac250206409266a21e65e71fd1d208e1668cccf31179ecce6651b59cee41a94e` | `f5fde7813b9593646ab2b85c935eea92774d81fc742de7551461404d10a527cf` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1293` | `3006` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `baeb07d4238233568e3f77140dbede205dc7f1b92755487a76788fc6816dc5ca` | `5e1d6295cbc1c5a13d29b715c1a5a289e4e06d5e7bd96c01abc00a4657940f41` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/outcome/response/result/full_json/sha256` | `444013ce1137fc06ceaba2edf06537931fff4d6e2342888fda7169cf7f71345c` | `cb550fe7af97b9cf84ca361359c43158d99543ca3ee189d6eae9d3aa1a32dcbf` |
| `/outcome/response/result/full_json/utf8_bytes` | `3185` | `4911` |

## company-followups-and-history 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `064729c65140dcf02c1cc074a30eb5000c691fa5045f5f42092caa5d5b87beb4` | `38c06cc3d1cc1de8ed80d663bb0920c1ee1e153d036e6941c295a1827be1da41` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## company-followups-and-history 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `2ae1f80feb40e083b8f1aff85da1ede83c12da76a934f6bbbed9fa4dd7e4573c` | `324425520f0de273b521aebd781a3debbfa02fce80f0399e5d230ea8bc155158` |
| `/outcome/response/result/full_json/utf8_bytes` | `3232` | `4961` |

## company-followups-and-history 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `54973ba55c6ee42605699dd86fb78a9156c13535bb19a708464cd65445c8b9f1` | `d58a5898a7ba21ac14e96e31d9db3ace07ca685e2c0e138c735c7872b80a0a90` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `9f802dfaa79df456ad8107c59677a2abb850efdad4133444d5995c4a61a94ef7` | `d5fc0a6217f2941f1b88238cbf0cd5c42565ea2ceb74d3b1ccbca97cfebd5ea4` |
| `/outcome/response/result/full_json/utf8_bytes` | `2989` | `3675` |

## company-followups-and-history 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `54973ba55c6ee42605699dd86fb78a9156c13535bb19a708464cd65445c8b9f1` | `d58a5898a7ba21ac14e96e31d9db3ace07ca685e2c0e138c735c7872b80a0a90` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `54973ba55c6ee42605699dd86fb78a9156c13535bb19a708464cd65445c8b9f1` | `d58a5898a7ba21ac14e96e31d9db3ace07ca685e2c0e138c735c7872b80a0a90` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/outcome/response/result/full_json/sha256` | `d6c5ed1d735db90125f2e16ecbf56aeb374a9554f740b940338da035c3220d85` | `cefe43c729d55e928777eb86ce78b678c29827fdbdb8a5ccdaf15cff5c11838f` |
| `/outcome/response/result/full_json/utf8_bytes` | `2997` | `3683` |

## company-followups-and-history 第 5 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `54973ba55c6ee42605699dd86fb78a9156c13535bb19a708464cd65445c8b9f1` | `d58a5898a7ba21ac14e96e31d9db3ace07ca685e2c0e138c735c7872b80a0a90` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `54973ba55c6ee42605699dd86fb78a9156c13535bb19a708464cd65445c8b9f1` | `d58a5898a7ba21ac14e96e31d9db3ace07ca685e2c0e138c735c7872b80a0a90` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1304` | `1977` |
| `/outcome/response/result/full_json/sha256` | `d9c546eb74e480842286e6f01722aba8d5ee2a9c045029cad7da5b213f9298a3` | `a44c7c1e2abde7eae57f6f145f681dc4f980bed74e9cc450c8b6243f503df974` |
| `/outcome/response/result/full_json/utf8_bytes` | `2997` | `3683` |

## lazy-clock-named-member 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `8c756623e3dc0ebe21326673444e6aa9df5040bdce5702afd1be90094ea44284` | `94dae0b9eb040e7d607a87f8ef6061b0a119d6a0b709b633b712e285a53c5ae1` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `4b7b9595724e3412ea07fa978bc9b3013e82e83258a06c1df9c584953670eb54` | `aa80ae64802e505420a1a09d69e8b0919cdd55b763f3c870ecbdd3b02b6bcace` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## selected-technical-followup 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `c2d481d5441446a9ddbdd2f090be7fddc87580a4ae4a6a8a49bec05a373f09c2` | `0f260f19f6b0cde10f970866c0de5f4c37fbc0a3cb52b2cc320cfdd509511af6` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1292` | `1962` |
| `/outcome/response/result/full_json/sha256` | `4613d6be8b591561183ebb252813b8901c1f35e01271c6f562aaad4bb364124b` | `6f5f18ddc3d1a469b85baa1004084d2b3b10c487472dda7d869fdc7156ef3337` |
| `/outcome/response/result/full_json/utf8_bytes` | `2999` | `3682` |

## failed-research-consumes-current 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `064729c65140dcf02c1cc074a30eb5000c691fa5045f5f42092caa5d5b87beb4` | `38c06cc3d1cc1de8ed80d663bb0920c1ee1e153d036e6941c295a1827be1da41` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## failed-research-consumes-current 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |

## decision-direct-and-history 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/outcome/response/result/full_json/sha256` | `19a6051e2ece04d81bbc899dba9ef5deafcf0a0ed1811324947ffdd37a34af16` | `6c2b49b21cab28e2afcad1c65945abdd75f130aab16341739111179182292f1e` |
| `/outcome/response/result/full_json/utf8_bytes` | `62525` | `113518` |

## decision-direct-and-history 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/outcome/response/result/full_json/sha256` | `d746d94861f713b50374f514c96dc53b9f32ea564dee2a3272bf6545a7efd604` | `87f1022ee2f282760682b73b5276ca55a00dbbd60c1599a3c68b0cd323e10edd` |
| `/outcome/response/result/full_json/utf8_bytes` | `62533` | `113526` |

## decision-paraphrases 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `064729c65140dcf02c1cc074a30eb5000c691fa5045f5f42092caa5d5b87beb4` | `38c06cc3d1cc1de8ed80d663bb0920c1ee1e153d036e6941c295a1827be1da41` |
| `/outcome/response/result/full_json/utf8_bytes` | `3200` | `4929` |

## decision-paraphrases 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `76c81948b7eead722e9f709c06b0e8558d141da949ec41b6944c1bbb1ba1d37b` | `c7ac546613ec4e6f91c463494d2d453363ecd8bb1127cc194945acaac34b9e32` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `1299` | `3015` |
| `/outcome/response/result/full_json/sha256` | `19a6051e2ece04d81bbc899dba9ef5deafcf0a0ed1811324947ffdd37a34af16` | `6c2b49b21cab28e2afcad1c65945abdd75f130aab16341739111179182292f1e` |
| `/outcome/response/result/full_json/utf8_bytes` | `62525` | `113518` |

## decision-paraphrases 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `c45b19b41c9a3c8e38042e00f1af3b93e07393bc037af9fbe047c46bf05ad0c9` | `68ffe2a877bed8296dde89760ec94d71a908ff181784e531e52eec2bdd18f2fc` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/outcome/response/result/full_json/sha256` | `8833f31aea91fcc3aa9351660ed7be1c41f9d299ac8464548506f028b6576fa4` | `c2bb06eb24cbeee8a68f024ef42a0db089ed2ff154bad2e953f2e58c8d65a632` |
| `/outcome/response/result/full_json/utf8_bytes` | `62525` | `113518` |

## decision-paraphrases 第 4 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `f3a884a38322cb64d8f2d959d2104ad821606a3faf62d624fbdd1d7f6883432b` | `2f6959ac63bb41ce07134145eddec2df01b79dd537bf1609ab3cd8e8e38c7ea4` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `c45b19b41c9a3c8e38042e00f1af3b93e07393bc037af9fbe047c46bf05ad0c9` | `68ffe2a877bed8296dde89760ec94d71a908ff181784e531e52eec2bdd18f2fc` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/outcome/response/result/full_json/sha256` | `b7ba33a1cde80f65d45129c43c2d1a3b5343082bf353d8671533651f27fd6875` | `c4142961b24fbb00b78a1aad1c7b7f12ff8ceed8287c9d8a7d06cdea5557c5de` |
| `/outcome/response/result/full_json/utf8_bytes` | `62494` | `113487` |

## decision-identity-boundary 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/outcome/response/result/full_json/sha256` | `19a6051e2ece04d81bbc899dba9ef5deafcf0a0ed1811324947ffdd37a34af16` | `6c2b49b21cab28e2afcad1c65945abdd75f130aab16341739111179182292f1e` |
| `/outcome/response/result/full_json/utf8_bytes` | `62525` | `113518` |

## decision-identity-boundary 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `806c6a87c14895b99f091f71b7bf6085a4763575b89e70002c37815fb5e8b971` | `56055063e78db5fba7d22968722cd7cd3fab407339f60362089ba208f7b44da7` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `6614` | `57594` |

## decision-domain-limits 第 1 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `5f231b7de6bebf3ca7e8d8912a37944464fbc51dd05024c8960817453847a545` | `5a70fa8444cb6d510eec52526728cd6cfbd81d3ce3760b471e01e1f4a2ed1089` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `3947` | `4656` |
| `/outcome/response/result/full_json/sha256` | `877670685f10ed9d68cb2ef55d0cd9470ca99d5065b9e639d4492268137fa6f0` | `d3a5e14614c92b662557941f20726c7eed29a529af2b4b78c9691eddc3ad991a` |
| `/outcome/response/result/full_json/utf8_bytes` | `5729` | `6451` |

## decision-domain-limits 第 2 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `634a810ada159dede95a94ae3454d031b78b2a2daaea8158f8042f01050f0bc0` | `e4fd677363d96e71c68377b0bdefcfb83c2edeb77692fc4c1412cd89ec68a96c` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `4196` | `52880` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `5f231b7de6bebf3ca7e8d8912a37944464fbc51dd05024c8960817453847a545` | `5a70fa8444cb6d510eec52526728cd6cfbd81d3ce3760b471e01e1f4a2ed1089` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `3947` | `4656` |
| `/outcome/response/result/full_json/sha256` | `f76deb1350f02c81e552b4a0ffb290892183e8bbda2d784b38a7d89dec416567` | `d22024433d5f4fa220bb2fffef78821c62cdd260413f6c8a55e50584638a43b1` |
| `/outcome/response/result/full_json/utf8_bytes` | `57706` | `106403` |

## decision-domain-limits 第 3 轮

原因：本轮生成或持有新 v3 回答；接受状态、模型/采集调用与路由保持原契约。

| 字段 | 原值 | 新值 |
| --- | --- | --- |
| `/after/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/after/product_saved/answer/full_json/sha256` | `80259fe200720004350563284b328bb56321163973332c176608bda77f588b64` | `a4a06fe20e222e286d48a8b8c4243ddbbe86946015e795bedc17b7a2fb4ea9d6` |
| `/after/product_saved/answer/full_json/utf8_bytes` | `4061` | `54454` |
| `/before/product_saved/answer/code` | `product_research_answered` | `answer_ready` |
| `/before/product_saved/answer/full_json/sha256` | `634a810ada159dede95a94ae3454d031b78b2a2daaea8158f8042f01050f0bc0` | `e4fd677363d96e71c68377b0bdefcfb83c2edeb77692fc4c1412cd89ec68a96c` |
| `/before/product_saved/answer/full_json/utf8_bytes` | `4196` | `52880` |
| `/outcome/response/result/full_json/sha256` | `9b18b1d57fc1405faf1625ba680f2de9002d50cac16c6536af64daf6739dd60e` | `1a7b3e18234bec0d3d4e113f1bb6515b7278f3ec28bf94134a6dd25b7377b03b` |
| `/outcome/response/result/full_json/utf8_bytes` | `59972` | `110378` |
