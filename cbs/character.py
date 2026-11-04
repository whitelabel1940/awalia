from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

from cbs.config import PersonaConfig
from cbs.paths import resolve_paths

Status = Literal["open", "tentative", "supported", "well_supported", "contested"]


class SeedElement(BaseModel):
    model_config = ConfigDict(extra="allow")


class Signature(SeedElement):
    id: str
    when: str
    tends_to: str
    status: Status
    evidence: list[str] = Field(default_factory=list)
    origin: Literal["longstanding", "acquired", "unknown"]


class Domain(SeedElement):
    id: str
    label: str
    status: Status
    known: str | None = None
    evidence: list[str] = Field(default_factory=list)
    related_signatures: list[str] = Field(default_factory=list)


class LifeHistory(SeedElement):
    phases: list[dict] = Field(default_factory=list)
    relationships: list[dict] = Field(default_factory=list)
    transitions: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    trajectories: list[dict] = Field(default_factory=list)
    past_detail_policy: dict = Field(default_factory=dict)

    def elements(self) -> list[dict]:
        return self.phases + self.relationships + self.transitions + self.events


class CharacterModel(BaseModel):
    version: int
    character_id: str
    display_name: str
    summary: list[str]
    signatures: list[Signature]
    domains: list[Domain]
    life_history: LifeHistory
    tensions: list[dict] = Field(default_factory=list)


class Voice(BaseModel):
    first_person: str
    second_person: dict[str, str]
    speech_register: str = Field(alias="register")
    length: dict[str, str]
    sentence_endings: list[str] = Field(default_factory=list)
    # 話し方の癖と、実際に話した言葉の例。会話モデルが替わっても話し方を保つための手本
    habits: list[str] = Field(default_factory=list)
    sample_lines: list[str] = Field(default_factory=list)
    allow_disagreement_and_negative_emotion: bool


class Expression(BaseModel):
    voice: Voice
    demeanor: list[str]
    preferences: list[str]
    relationship_context: dict
    not_adopted_as_goal: list[str]
    existence: dict
    items: list[dict] = Field(default_factory=list)
    current_life: list[dict] = Field(default_factory=list)
    elaborate_default: Literal['keep', 'may_elaborate'] = 'may_elaborate'


class Character(BaseModel):
    model: CharacterModel
    expression: Expression
    sources: dict[str, str]
    persona: PersonaConfig | None = None
    interests: list[dict] = Field(default_factory=list)
    seed_memories: list[dict] = Field(default_factory=list)
    bootstrap_meta: dict = Field(default_factory=dict)
    reveal: "RevealDefinition" = Field(default_factory=lambda: RevealDefinition())
    # World frame (ROADMAP §2.0): era / region / reality / being / contact. Default = modern Japan, human, same world.
    world: dict = Field(default_factory=lambda: {"era": "現代", "region": "日本", "reality": "現実どおり",
                                                  "being": "人間", "contact": "B1"})


class RevealGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: int = Field(default=0, ge=0)
    days: int = Field(default=0, ge=0)
    deep_moments: int = Field(default=0, ge=0)
    user_disclosures: int = Field(default=0, ge=0)


class SecretDefinition(BaseModel):
    id: str
    life_ref: str
    about: str
    constraints: list[str] = Field(default_factory=list)


class RevealDefinition(BaseModel):
    generation: Literal["auto", "author_approval"] = "auto"
    gates: dict[Literal["L1", "L2", "L3"], RevealGate] = Field(default_factory=dict)
    secrets: list[SecretDefinition] = Field(default_factory=list)


def load_character(character_id: str, root: Path | None = None) -> Character:
    import re
    if not re.fullmatch(r'[a-z][a-z0-9_\-]{0,63}', character_id):
        raise ValueError('Invalid character id')
    directory = (Path(root) if root is not None else resolve_paths()[1]) / character_id
    return Character(
        model=CharacterModel.model_validate(yaml.safe_load((directory / "model.yaml").read_text(encoding="utf-8"))),
        expression=Expression.model_validate(yaml.safe_load((directory / "expression.yaml").read_text(encoding="utf-8"))),
        sources={path.name: path.read_text(encoding="utf-8") for path in sorted((directory / "source").glob("*.md"))},
        persona=PersonaConfig.model_validate(yaml.safe_load((directory / 'persona.yaml').read_text(encoding='utf-8')))
                if (directory / 'persona.yaml').is_file() else None,
        **{name: yaml.safe_load((directory / f'{name}.yaml').read_text(encoding='utf-8'))
           for name in ('interests', 'seed_memories', 'bootstrap_meta') if (directory / f'{name}.yaml').exists()},
        reveal=RevealDefinition.model_validate(yaml.safe_load((directory / "reveal.yaml").read_text(encoding="utf-8")))
               if (directory / "reveal.yaml").exists() else RevealDefinition(),
        **({"world": yaml.safe_load((directory / "world.yaml").read_text(encoding="utf-8"))}
           if (directory / "world.yaml").exists() else {}),
    )


def effective_persona(character: Character, config) -> PersonaConfig:
    """Read-only migration: missing partner settings use the legacy global value."""
    return character.persona if character.persona is not None else config.persona


def write_persona(directory: Path, persona: PersonaConfig):
    """Create a partner override only on an explicit save, using an atomic replace."""
    path = directory / 'persona.yaml'
    temporary = path.with_suffix('.yaml.tmp')
    temporary.write_text(yaml.safe_dump(persona.model_dump(exclude_none=True), sort_keys=False), encoding='utf-8')
    temporary.replace(path)
