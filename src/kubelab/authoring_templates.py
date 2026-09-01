"""Fixed, safe, immediately testable M8 author scaffolds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import yaml

from kubelab.lab_schema import LabDefinition

ScenarioType = Literal["baseline", "variant", "composite"]


def baseline_template(
    *,
    lab_id: str,
    title: str,
    category: str,
    difficulty: str,
    description: str,
) -> dict[str, bytes]:
    namespace = f"kubelab-{lab_id.removeprefix('lab-')}"
    lab = {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "Lab",
        "metadata": {
            "id": lab_id,
            "name": title,
            "description": description,
            "difficulty": difficulty,
            "durationMinutes": 25,
            "category": category,
            "tags": ["authoring", "deployment"],
        },
        "requirements": {
            "kubernetes": ">=1.28",
            "minimumCpu": 1,
            "minimumMemoryMiB": 1024,
            "addons": [],
        },
        "environment": {
            "namespace": namespace,
            "manifests": ["manifests/deployment.yaml"],
            "provisionTimeoutSeconds": 180,
        },
        "task": {
            "description": "调查Deployment副本数不足，并恢复声明的可用容量。",
            "completionDescription": "Deployment至少有两个可用副本。",
            "successMessage": "Deployment容量已经恢复。",
        },
        "initialChecks": [
            {
                "id": "deployment-fault-visible",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 1,
                "timeoutSeconds": 60,
                "unmetMessage": "Deployment尚未形成可观察的初始状态。",
            }
        ],
        "successChecks": [
            {
                "id": "deployment-capacity-restored",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 2,
                "timeoutSeconds": 90,
                "unmetMessage": "Deployment可用副本仍少于两个。",
            }
        ],
        "hints": [
            {"level": 1, "content": "先比较期望副本数与当前可用副本数。"},
            {"level": 2, "content": "kubectl get deployment web -o wide"},
            {"level": 3, "content": "故障方向是Deployment的replicas字段。"},
        ],
        "cleanup": {"deleteNamespace": True},
        "interview": {
            "questions": [
                "期望副本与可用副本分别说明什么？",
                "为什么修复后还要等待可用副本稳定？",
                "如何预防容量配置意外回退？",
            ]
        },
    }
    LabDefinition.model_validate(lab)
    manifest = _deployment(namespace, replicas=1)
    repair = _deployment(namespace, replicas=2)
    contract = _scaling_contract("baseline", composite=False)
    return {
        "lab.yaml": _yaml(lab),
        "README.md": _readme(title, description),
        "manifests/deployment.yaml": _yaml(manifest),
        "solutions/fix.yaml": _yaml(repair),
        "authoring.yaml": _yaml(contract),
    }


def variant_template(
    *,
    variant_id: str,
    sequence: int,
    name: str,
    description: str,
    namespace: str,
) -> dict[str, bytes]:
    variant = {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "LabVariant",
        "metadata": {
            "id": variant_id,
            "sequence": sequence,
            "name": name,
            "description": description,
        },
        "environment": {
            "manifests": ["manifests/deployment.yaml"],
            "provisionTimeoutSeconds": 180,
        },
        "task": {
            "description": "复练场景中的Deployment容量低于目标，请调查并恢复。",
            "completionDescription": "Deployment至少有两个可用副本。",
            "successMessage": "复练场景的容量故障已经修复。",
        },
        "initialChecks": [
            {
                "id": "variant-fault-visible",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 1,
                "timeoutSeconds": 60,
                "unmetMessage": "复练场景尚未形成可观察状态。",
            }
        ],
        "successChecks": [
            {
                "id": "variant-capacity-restored",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 2,
                "timeoutSeconds": 90,
                "unmetMessage": "Deployment可用副本仍少于两个。",
            }
        ],
        "hints": [
            {"level": 1, "content": "观察控制器声明容量与实际可用容量。"},
            {"level": 2, "content": "kubectl get deployment web -o wide"},
            {"level": 3, "content": "复查Deployment的replicas字段。"},
        ],
        "reveal": {
            "keyEvidence": "Deployment始终只有一个可用副本。",
            "rootCause": "声明的replicas低于成功目标。",
            "resolution": "把replicas恢复为2并等待滚动完成。",
            "prevention": "用策略和发布检查保护容量基线。",
        },
    }
    manifest = _deployment(namespace, replicas=1)
    repair = _deployment(namespace, replicas=2)
    contract = _scaling_contract("variant", composite=False)
    return {
        "variant.yaml": _yaml(variant),
        "README.md": _readme(name, description),
        "manifests/deployment.yaml": _yaml(manifest),
        "solutions/fix.yaml": _yaml(repair),
        "authoring.yaml": _yaml(contract),
    }


def composite_template(
    *,
    lab_id: str,
    title: str,
    category: str,
    difficulty: str,
    description: str,
) -> dict[str, bytes]:
    namespace = f"kubelab-{lab_id.removeprefix('lab-')}"
    lab = {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "Lab",
        "metadata": {
            "id": lab_id,
            "name": title,
            "description": description,
            "difficulty": difficulty,
            "durationMinutes": 50,
            "category": category,
            "tags": ["authoring", "deployment", "service"],
        },
        "requirements": {
            "kubernetes": ">=1.28",
            "minimumCpu": 1,
            "minimumMemoryMiB": 1024,
            "addons": [],
        },
        "environment": {
            "namespace": namespace,
            "manifests": ["manifests/resources.yaml"],
            "provisionTimeoutSeconds": 180,
        },
        "task": {
            "description": "依次恢复Deployment容量和Service选择器，证明两个根因均已解决。",
            "completionDescription": "Deployment有两个可用副本且Service存在Endpoint。",
            "successMessage": "容量与流量入口均已恢复。",
        },
        "initialChecks": [
            {
                "id": "deployment-fault-visible",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 1,
                "timeoutSeconds": 60,
                "unmetMessage": "Deployment初始状态不可观察。",
            },
            {
                "id": "endpoints-empty",
                "type": "service_endpoint_count",
                "name": "web",
                "exactly": 0,
                "timeoutSeconds": 30,
                "unmetMessage": "Service未呈现预期的空Endpoint。",
            },
        ],
        "successChecks": [
            {
                "id": "deployment-capacity-restored",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 2,
                "timeoutSeconds": 90,
                "unmetMessage": "Deployment容量尚未恢复。",
            },
            {
                "id": "endpoints-restored",
                "type": "service_endpoint_count",
                "name": "web",
                "minimum": 1,
                "timeoutSeconds": 45,
                "unmetMessage": "Service仍然没有Endpoint。",
            },
        ],
        "hints": [
            {"level": 1, "content": "先修复容量；每次变更后继续验证Service链路。"},
            {
                "level": 2,
                "content": "kubectl get deployment web -o wide && kubectl get service web -o yaml",
            },
            {"level": 3, "content": "两个方向分别是replicas和Service selector。"},
        ],
        "cleanup": {"deleteNamespace": True},
        "interview": {
            "questions": [
                "为什么修复第一个根因后不能停止验证？",
                "哪些证据区分容量故障与流量入口故障？",
                "如何为多根因事故设计分阶段验证？",
            ]
        },
    }
    manifest = [_deployment(namespace, replicas=1), _service(namespace, selector="wrong")]
    first = _deployment(namespace, replicas=2)
    full = [_deployment(namespace, replicas=2), _service(namespace, selector="authoring-web")]
    contract = _scaling_contract("composite", composite=True)
    return {
        "lab.yaml": _yaml(lab),
        "README.md": _readme(title, description),
        "manifests/resources.yaml": _yaml_all(manifest),
        "solutions/fix-stage-1.yaml": _yaml(first),
        "solutions/fix.yaml": _yaml_all(full),
        "authoring.yaml": _yaml(contract),
    }


def _deployment(namespace: str, *, replicas: int) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": namespace},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": "authoring-web"}},
            "template": {
                "metadata": {"labels": {"app": "authoring-web"}},
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 101,
                        "runAsGroup": 101,
                        "fsGroup": 101,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": [
                        {"name": "nginx-cache", "emptyDir": {}},
                        {"name": "nginx-run", "emptyDir": {}},
                    ],
                    "containers": [
                        {
                            "name": "web",
                            "image": "nginx:1.27-alpine",
                            "ports": [{"name": "http", "containerPort": 80}],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "250m", "memory": "256Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "volumeMounts": [
                                {"name": "nginx-cache", "mountPath": "/var/cache/nginx"},
                                {"name": "nginx-run", "mountPath": "/run"},
                            ],
                        }
                    ],
                },
            },
        },
    }


def _service(namespace: str, *, selector: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web", "namespace": namespace},
        "spec": {
            "selector": {"app": selector},
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    }


def _scaling_contract(scenario_type: ScenarioType, *, composite: bool) -> dict[str, Any]:
    def state(replicas: int, endpoints: int | None = None) -> dict[str, Any]:
        observations: dict[str, Any] = {
            "deployment-fault-visible" if scenario_type != "variant" else "variant-fault-visible": {
                "type": "deployment_available",
                "availableReplicas": replicas,
            },
            (
                "deployment-capacity-restored"
                if scenario_type != "variant"
                else "variant-capacity-restored"
            ): {"type": "deployment_available", "availableReplicas": replicas},
        }
        if endpoints is not None:
            observations["endpoints-empty"] = {
                "type": "service_endpoint_count",
                "count": endpoints,
            }
            observations["endpoints-restored"] = {
                "type": "service_endpoint_count",
                "count": endpoints,
            }
        return {"observations": observations}

    states: dict[str, Any] = {
        "faulted": state(1, 0 if composite else None),
        "repaired": state(2, 1 if composite else None),
        "reset": "faulted",
    }
    repairs: dict[str, Any] = {
        "full": {
            "manifest": "solutions/fix.yaml",
            "allowedChanges": [
                {
                    "resource": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "web",
                    },
                    "operation": "modify",
                    "paths": ["/spec/replicas"],
                }
            ],
        }
    }
    if composite:
        states["firstRepair"] = state(2, 0)
        repairs["first"] = {
            "manifest": "solutions/fix-stage-1.yaml",
            "allowedChanges": [
                {
                    "resource": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "web",
                    },
                    "operation": "modify",
                    "paths": ["/spec/replicas"],
                }
            ],
        }
        repairs["full"]["allowedChanges"].append(
            {
                "resource": {"apiVersion": "v1", "kind": "Service", "name": "web"},
                "operation": "modify",
                "paths": ["/spec/selector/app"],
            }
        )
    return {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "LabAuthoringContract",
        "scenarioType": scenario_type,
        "states": states,
        "repairs": repairs,
    }


def _readme(title: str, description: str) -> bytes:
    return (
        f"# {title}\n\n"
        f"## 是什么\n\n{description}\n\n"
        "## 为什么\n\n这个样例演示如何用可观察证据定义一个可重复的Kubernetes故障。\n\n"
        "## 怎么做\n\n先观察资源状态，再实施最小修复，并重新运行成功契约。\n"
    ).encode()


def _yaml(value: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def _yaml_all(values: Sequence[Mapping[str, Any]]) -> bytes:
    return yaml.safe_dump_all(values, allow_unicode=True, sort_keys=False, width=100).encode(
        "utf-8"
    )


__all__ = ["ScenarioType", "baseline_template", "composite_template", "variant_template"]
