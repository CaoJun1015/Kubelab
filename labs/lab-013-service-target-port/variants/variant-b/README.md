# Service命名端口错配

在受限Workspace中调查Service与Pod端口声明。标准修复只修改`targetPort`，不修改Deployment、Selector或标签。

