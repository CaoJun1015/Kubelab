# LAB-016 StatefulSet无头服务错配

StatefulSet创建的nginx Pod正常运行，但`web-headless` Service使用了错误的Selector，导致EndpointSlice为空。实验把工作负载健康与稳定网络发现分开验证。

标准修复只调整Headless Service Selector，不修改StatefulSet、Pod标签或`clusterIP: None`属性。完成条件包含Endpoint和HTTP业务探测。
