# LAB-008 ConfigMap缺失

Deployment通过`envFrom`引用不存在的ConfigMap，Pod因此出现`CreateContainerConfigError`。标准修复只创建缺失对象。

验证同时检查ConfigMap存在和Pod稳定Ready，不公开配置的实际值。
