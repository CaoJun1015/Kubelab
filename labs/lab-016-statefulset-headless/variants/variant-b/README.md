# StatefulSet治理服务名称错配

`serviceName`不可变。先删除StatefulSet（不要删除Namespace），再应用标准修复；Headless Service无需修改。

