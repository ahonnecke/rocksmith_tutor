"""Curriculum data model with YAML persistence."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

from .config import CURRICULUM_PATH


@dataclass
class Exercise:
    song_id: str
    song_display: str
    section_name: str
    section_number: int
    rationale: str
    techniques_practiced: list[str]


@dataclass
class Lesson:
    id: str
    name: str
    objectives: list[str]
    exercises: list[Exercise]
    notes: str = ""


@dataclass
class Module:
    id: str
    name: str
    order: int
    skill_level: str
    prerequisites: list[str]
    lessons: list[Lesson]


@dataclass
class Curriculum:
    version: int = 1
    generated_at: str = ""
    modules: list[Module] = field(default_factory=list)

    def save(self, path: Path | None = None) -> None:
        path = path or CURRICULUM_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    @classmethod
    def load(cls, path: Path | None = None) -> Curriculum:
        path = path or CURRICULUM_PATH
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text())
        if not data or not isinstance(data, dict):
            return cls()
        modules = []
        for m in data.get("modules", []):
            lessons = []
            for les in m.get("lessons", []):
                exercises = [Exercise(**ex) for ex in les.get("exercises", [])]
                lessons.append(Lesson(
                    id=les["id"],
                    name=les["name"],
                    objectives=les.get("objectives", []),
                    exercises=exercises,
                    notes=les.get("notes", ""),
                ))
            modules.append(Module(
                id=m["id"],
                name=m["name"],
                order=m.get("order", 0),
                skill_level=m.get("skill_level", ""),
                prerequisites=m.get("prerequisites", []),
                lessons=lessons,
            ))
        return cls(
            version=data.get("version", 1),
            generated_at=data.get("generated_at", ""),
            modules=modules,
        )
