# 前端展示约定

“本次对话”条目保留可聚焦链接，鼠标或键盘激活时由页面只滚动消息区。禁止让浏览器原生锚点跳转或 `scrollIntoView` 同时滚动页面外壳；移动端选择条目后关闭导航抽屉。外壳使用 `overflow: clip`，消息区保留独立滚动。

指数段落不显示“依据：……”小字；个股段落不显示“依据 N”，研判卡片不显示“研判依据”及来源明细。评级、方向、期限、价位、研判失效或重评条件继续显示。部分研判理由被移除时保留简短核对提示。

依据、段落引用及研判理由的数据仍由接口提供并通过原有前端显示校验。隐藏展示不放宽跨证券、未知引用、数据结构或计划绑定检查。知识问答的来源展示继续使用共享的安全链接校验及来源样式。

从仓库根目录验证：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py python
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py web
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py bundle
```

`web`包含前端测试、TypeScript检查及生产构建；`bundle`应在本次构建成功后检查服务和安装包中的产物。浏览器复测应覆盖桌面及窄屏的侧栏跳转、键盘激活和抽屉关闭，确认文档、根节点与外壳位置不变，只有消息区滚动。
