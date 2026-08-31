"""Documentation contracts for the learner-facing baseline tutorial."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
TUTORIAL = ROOT / "docs" / "TUTORIAL.md"


def test_tutorial_covers_every_baseline_once_with_learning_structure() -> None:
    content = TUTORIAL.read_text(encoding="utf-8")
    headings = re.findall(r"^### (LAB-(\d{3}) .+)$", content, flags=re.MULTILINE)

    assert [number for _, number in headings] == [f"{number:03d}" for number in range(1, 22)]
    assert content.count("#### 是什么") == 21
    assert content.count("#### 为什么") == 21
    assert content.count("#### 怎么做") == 21
    assert content.count("```") % 2 == 0


def test_tutorial_preserves_blind_repeat_and_workspace_boundaries() -> None:
    content = TUTORIAL.read_text(encoding="utf-8")

    assert "不会提前公开它们的答案" in content
    assert "solutions/fix.yaml" not in content
    assert "kubelab workspace enter" in content
    assert "不要信任远程、公司或生产Context" in content
    assert "不要手动删除Namespace" in content
    assert "/mnt/" not in content
    assert ":\\" not in content


def test_readme_links_tutorial_and_distribution_requires_it() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "verify_distribution.py").read_text(encoding="utf-8")

    assert "[《KubeLab 21个实验操作教程》](docs/TUTORIAL.md)" in readme
    assert '"docs/TUTORIAL.md"' in verifier
