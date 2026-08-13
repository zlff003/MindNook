from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


REQUIRED_SKILLS = (
    "supportive_response_baseline",
    "referral_resource_guidance",
    "anxiety_grounding_support",
    "sleep_routine_support",
    "academic_stress_planning",
    "high_risk_safety_plan",
    "counselor_handoff_summary",
)
ALLOWED_FRONTMATTER = {"name", "description"}
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
WORKFLOW_PATTERN = re.compile(r"(?m)^##[ \t]+Workflow[ \t]*$")
HANDOFF_TEMPLATE_PATTERN = re.compile(r"`(报告ID：\{report_id\}[^`]+)`")

ANXIETY_MARKERS = (
    "焦虑", "惊恐", "恐慌", "崩溃", "呼吸紧张", "喘不过气", "脑子停不下来",
    "紧张", "panic", "anxiety",
)
SLEEP_MARKERS = ("失眠", "睡不着", "熬夜", "作息", "睡眠", "彻夜")
ACADEMIC_MARKERS = (
    "考试", "挂科", "绩点", "论文", "作业", "学业", "复习", "课程", "成绩",
    "导师", "答辩",
)


class SkillStatus(StrEnum):
    READY = "READY"
    WARN = "WARN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SkillDefinition:
    directory_name: str
    path: Path
    name: str | None
    description: str | None
    workflow: str
    content: str
    status: SkillStatus
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def loadable(self) -> bool:
        return self.status != SkillStatus.FAILED

    def to_public_dict(self) -> dict:
        return {
            "name": self.name or self.directory_name,
            "directory": self.directory_name,
            "description": self.description or "",
            "status": self.status.value,
            "issues": list(self.issues),
            "path": str(self.path),
        }


def _parse_frontmatter(source: str) -> tuple[dict[str, str], str]:
    normalized = source.lstrip("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("缺少 YAML frontmatter")
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise ValueError("YAML frontmatter 未闭合")
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"无法解析 frontmatter 行：{line.strip()}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError("frontmatter 包含空字段名")
        values[key] = value.strip().strip('"').strip("'")
    return values, "\n".join(lines[closing + 1 :]).strip()


class SkillRegistry:
    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir).resolve()
        self._skills: dict[str, SkillDefinition] = {}

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    def scan(self) -> tuple[SkillDefinition, ...]:
        directory_names = set(REQUIRED_SKILLS)
        if self.skills_dir.is_dir():
            directory_names.update(
                item.name for item in self.skills_dir.iterdir() if item.is_dir()
            )
        self._skills = {
            name: self._load_directory(name) for name in sorted(directory_names)
        }
        return self.skills

    def _failed(self, directory_name: str, path: Path, issue: str) -> SkillDefinition:
        return SkillDefinition(
            directory_name=directory_name,
            path=path,
            name=None,
            description=None,
            workflow="",
            content="",
            status=SkillStatus.FAILED,
            issues=(issue,),
        )

    def _load_directory(self, directory_name: str) -> SkillDefinition:
        path = self.skills_dir / directory_name / "SKILL.md"
        if not SKILL_NAME_PATTERN.fullmatch(directory_name):
            return self._failed(directory_name, path, "目录名必须使用小写字母、数字和下划线")
        if not path.is_file():
            return self._failed(directory_name, path, "缺少 SKILL.md")
        try:
            source = path.read_text(encoding="utf-8")
            metadata, body = _parse_frontmatter(source)
        except (OSError, UnicodeError, ValueError) as exc:
            return self._failed(directory_name, path, str(exc))

        errors: list[str] = []
        warnings: list[str] = []
        name = metadata.get("name")
        description = metadata.get("description")
        if name is None:
            errors.append("frontmatter 缺少 name")
        elif name != directory_name:
            errors.append("目录名与 skill name 不一致")
        if description is None:
            errors.append("frontmatter 缺少 description")
        elif not description.strip():
            warnings.append("description 为空")
        unknown = sorted(set(metadata) - ALLOWED_FRONTMATTER)
        if unknown:
            warnings.append(f"存在未知 frontmatter 字段：{', '.join(unknown)}")

        workflow_match = WORKFLOW_PATTERN.search(body)
        workflow = ""
        if not workflow_match:
            errors.append("缺少 ## Workflow")
        else:
            workflow = body[workflow_match.end() :].strip()
            if not workflow:
                warnings.append("Workflow 内容为空")

        issues = tuple(errors + warnings)
        status = (
            SkillStatus.FAILED
            if errors
            else SkillStatus.WARN if warnings else SkillStatus.READY
        )
        return SkillDefinition(
            directory_name=directory_name,
            path=path,
            name=name,
            description=description,
            workflow=workflow,
            content=body,
            status=status,
            issues=issues,
        )

    def get(self, name: str) -> SkillDefinition | None:
        item = self._skills.get(name)
        return item if item and item.loadable else None

    def select_response_skills(
        self,
        *,
        risk_level: str,
        message: str,
        topics: list[str] | tuple[str, ...] = (),
    ) -> tuple[SkillDefinition, ...]:
        if risk_level == "CHAT":
            return ()
        if risk_level == "RISK":
            names = ("referral_resource_guidance", "high_risk_safety_plan")
        else:
            names_list = [
                "supportive_response_baseline",
                "referral_resource_guidance",
            ]
            haystack = f"{message} {' '.join(topics)}".lower()
            if any(marker in haystack for marker in ANXIETY_MARKERS):
                names_list.append("anxiety_grounding_support")
            if any(marker in haystack for marker in SLEEP_MARKERS):
                names_list.append("sleep_routine_support")
            if any(marker in haystack for marker in ACADEMIC_MARKERS):
                names_list.append("academic_stress_planning")
            names = tuple(names_list)
        return tuple(item for name in names if (item := self.get(name)))

    def render_handoff_summary(
        self,
        *,
        report_id: str,
        student_identity: str,
        risk_level: str,
        emotion_summary: str,
        confidence: float,
        reason_summary: str,
        student_expression: str,
    ) -> str:
        skill = self.get("counselor_handoff_summary")
        template_match = HANDOFF_TEMPLATE_PATTERN.search(skill.content) if skill else None
        template = (
            template_match.group(1)
            if template_match
            else "报告ID：{report_id}；学生：{student_identity}；风险等级：{risk_level}；"
            "情绪标签：{emotion_summary}；置信度：{confidence}；模型摘要：{reason_summary}；"
            "建议跟进：{follow_up_action}；原始表达摘录：{student_expression_excerpt}"
        )
        follow_up_action = (
            "优先核实当前安全、是否有人陪伴及是否已连接现实支持。"
            if risk_level == "RISK"
            else "结合实际需要复核当前困扰、日常影响与支持需求。"
        )
        excerpt = " ".join(student_expression.split())[:240] or "无可用表达"
        return template.format(
            report_id=report_id,
            student_identity=student_identity,
            risk_level=risk_level,
            emotion_summary=emotion_summary,
            confidence=f"{confidence:.2f}",
            reason_summary=reason_summary,
            follow_up_action=follow_up_action,
            student_expression_excerpt=excerpt,
        )
