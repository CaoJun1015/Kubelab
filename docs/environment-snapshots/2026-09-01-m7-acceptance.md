# KubeLab M7 `0.4.0a0`无集群验收记录

验收日期：2026-09-01  
验收范围：专题学习路径、确定性推荐、知识卡、症状索引、专题成果和发布产物  
真实集成：关闭

## 产品契约

- 包内声明4条专题路径、21张实验知识卡和9类症状。
- 路径由概念、基线、固定变体、综合实验和专题复盘节点组成。
- 综合实验解锁只使用既有基线与变体成功事实。
- 节点状态、复习建议和专题成果不写入第二套进度表。
- 不增加随机练习、限时面试、评分、排名、浏览器终端或在线YAML编辑器。

## Windows质量门

- Python：3.11.0。
- pytest：571项通过，37项按设计跳过。
- 覆盖率：91.53%。
- Ruff、Ruff format、strict mypy、JavaScript语法和`git diff --check`通过。
- wheel与sdist构建成功，并通过统一产物检查。

## WSL2 Ubuntu质量门

- Python：3.11.16。
- pytest：573项通过，35项按设计跳过。
- 覆盖率：91.65%。
- Ruff、Ruff format、strict mypy、JavaScript语法和`git diff --check`通过。
- wheel与sdist在`/tmp`隔离目录构建成功，并通过统一产物检查。

## 产物检查

统一检查确认`0.4.0a0`产物包含：

- 21个实验族、12个固定变体和33个可执行场景；
- 4条专题路径、21张知识卡和9类症状的声明式目录；
- 13个Web模板与静态资源；
- `0001`至`0003`迁移和类型标记；
- 项目文档、教程和安全策略。

产物不包含虚拟环境、缓存、数据库、日志、Token、kubeconfig、私钥或开发机绝对路径。

## 安全说明

- `KUBELAB_RUN_INTEGRATION=0`。
- `KUBELAB_RUN_LAB_INTEGRATION=0`。
- 未执行真实`start`、`reset`或`cleanup`。
- 未访问或修改minikube、远程集群或生产集群。
- 页面和API测试使用Fake Application Service与临时SQLite。

