# LAB-009 Readiness路径错误

Pod的Phase为Running并不代表它会接收Service流量。错误Readiness路径使Endpoint保持为空。

修复后验证Pod Ready、Endpoint数量和集群内HTTP 200三个层次。
