# LAB-011 Ingress后端端口错误

后端Deployment和Service均正常，但Ingress引用了Service未暴露的81端口。实验要求先确认`ingress` addon可用。

HTTP验证只访问平台固定的集群内Ingress Controller Service，并设置实验定义中的Host，不接受任意URL。
