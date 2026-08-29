# LAB-014 ConfigMap键缺失

`app-settings` ConfigMap已经存在，但Deployment通过`configMapKeyRef`引用的`APP_MODE`键缺失。Pod因此停留在`CreateContainerConfigError`，容易被误判为ConfigMap对象不存在。

标准修复只补充`APP_MODE=production`并保留已有配置。验证不会向Web或公开结果返回配置实际值，只公开检查状态和脱敏消息。
