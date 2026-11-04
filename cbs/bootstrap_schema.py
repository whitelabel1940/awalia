"""Bootstrap v2 input and bounded LLM output schemas."""
from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Elaboration = Literal['keep', 'may_elaborate']
OpenChoice = {'unknown', 'leave_open'}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        def require_properties(node):
            if isinstance(node, dict):
                if 'const' in node:
                    node['enum'] = [node.pop('const')]
                if node.get('type') == 'object' and 'properties' in node:
                    node['required'] = list(node['properties'])
                for child in node.values():
                    require_properties(child)
            elif isinstance(node, list):
                for child in node:
                    require_properties(child)
        require_properties(schema)
        return schema


class Basics(Strict):
    name: str = Field(min_length=1)
    age: str | None = None
    gender: str | None = None
    first_person: str | None = None
    second_person: str | None = None
    speech_register: str | None = Field(default=None, alias='register')
    length: str | None = None


class WorldInput(Strict):
    preset: str | None = '現代日本'
    era: str | None = None
    region: str | None = None
    reality: Literal['現実どおり', '少し不思議', 'ファンタジー', 'SF'] | None = None
    being: str | None = None
    contact: Literal['A', 'B1', 'B2', 'C'] = 'A'
    description: str | None = None


class LikeInput(Strict):
    topic: str = Field(min_length=1)
    kind: Literal['like', 'dislike', 'curiosity', 'into', 'opinion'] = 'like'
    detail: str | None = None
    elaborate: Elaboration | None = None


class Answer(Strict):
    answer: list[str] = Field(default_factory=lambda: ['leave_open'])
    text: str | None = None
    relationship_grouping: Literal['same', 'different'] | None = None


AnswerValue = list[str] | str | Answer


class NowInput(Strict):
    likes: list[LikeInput] = Field(default_factory=list)
    life: dict[str, AnswerValue] = Field(default_factory=dict)


class AdaptiveChoices(Strict):
    A: str = Field(min_length=1)
    B: str = Field(min_length=1)
    C: str = Field(min_length=1)
    D: str = Field(min_length=1)
    E: str = Field(min_length=1)


class AdaptiveQuestion(Strict):
    id: str = Field(pattern=r'^AQ[1-9][0-9]*$')
    domain: str = Field(min_length=1)
    parent_question: str = Field(min_length=1)
    added_condition: str = Field(min_length=1, pattern=r'\S')
    purpose: str = Field(min_length=1)
    scene: str = Field(min_length=1)
    choices: AdaptiveChoices
    multi: bool = False


class AdaptiveOutput(Strict):
    llm_role: ClassVar[str] = 'steward'
    questions: list[AdaptiveQuestion] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def valid_questions(self):
        from cbs.bootstrap import validate_adaptive_questions
        validate_adaptive_questions(self.questions)
        return self


class ReaderConflict(Strict):
    topic: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    question_ref: str = Field(min_length=1)
    answer_keys: list[str] = Field(min_length=1)


class ConflictResolution(Strict):
    conflict: ReaderConflict
    choice: Literal['text', 'answer']


class BootstrapPersona(Strict):
    open_style: Literal["hinted", "free"] | None = None
    ai_question: Literal['immersive', 'playful', 'honest'] = 'immersive'


class BootstrapInput(Strict):
    id: str = Field(pattern=r'^[a-z][a-z0-9_\-]{0,63}$')
    basics: Basics
    world: WorldInput = Field(default_factory=WorldInput)
    # Missing on legacy inputs: use the existing partner's value, or immersive for a new one.
    persona: BootstrapPersona | None = None
    text: str | None = None
    import_batch_ids: list[str] = Field(default_factory=list, max_length=12)
    now: NowInput = Field(default_factory=NowInput)
    answers: dict[str, AnswerValue] = Field(default_factory=dict)
    life: dict[str, AnswerValue] = Field(default_factory=dict)
    adaptive_questions: list[AdaptiveQuestion] = Field(default_factory=list, max_length=4)
    adaptive_answers: dict[str, AnswerValue] = Field(default_factory=dict)
    elaborate: dict[str, Elaboration] = Field(default_factory=lambda: {'default': 'may_elaborate'})
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    review_notes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_answers(self):
        from cbs.bootstrap import decode_answers, load_questionnaire
        decode_answers(self, load_questionnaire())
        return self


class TextItem(Strict):
    id: str
    category: Literal['past', 'now', 'world', 'expression', 'relationship', 'goal']
    content: str
    quote: str
    covers: list[str] = Field(default_factory=list)


class Mention(Strict):
    domain: str
    kind: Literal['text', 'trait_only']
    quotes: list[str] = Field(min_length=1)


class QuotedValue(Strict):
    value: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class QuotedReality(QuotedValue):
    value: Literal['現実どおり', '少し不思議', 'ファンタジー', 'SF']


class QuotedContact(QuotedValue):
    value: Literal['A', 'B1', 'B2', 'C']


class ReaderWorld(Strict):
    era: QuotedValue | None = None
    region: QuotedValue | None = None
    reality: QuotedReality | None = None
    being: QuotedValue | None = None
    contact: QuotedContact | None = None


class ReaderProfile(Strict):
    name: QuotedValue | None = None
    world: ReaderWorld = Field(default_factory=ReaderWorld)


class ReaderVoice(Strict):
    first_person: QuotedValue | None = None
    second_person_default: QuotedValue | None = None
    second_person_when_needed: QuotedValue | None = None
    speech_register: QuotedValue | None = Field(default=None, alias='register')
    length_default: QuotedValue | None = None
    length_expand_when: QuotedValue | None = None
    sentence_endings: list[QuotedValue] = Field(default_factory=list)


class SettingConflict(Strict):
    setting: Literal['world', 'persona.ai_question', 'import.profile']
    choice_template: Literal['world', 'appearance', 'source'] = 'world'
    topic: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class ReaderOutput(Strict):
    llm_role: ClassVar[str] = 'steward'
    items: list[TextItem] = Field(default_factory=list)
    mentions: list[Mention] = Field(default_factory=list)
    conflicts: list[ReaderConflict] = Field(default_factory=list)
    profile: ReaderProfile | None = None
    voice: ReaderVoice = Field(default_factory=ReaderVoice)
    setting_conflicts: list[SettingConflict] = Field(default_factory=list)


class ReaderReview(Strict):
    llm_role: ClassVar[str] = 'judge'
    voice: ReaderVoice = Field(default_factory=ReaderVoice)
    missing_items: list[TextItem] = Field(default_factory=list)
    revised_items: list[TextItem] = Field(default_factory=list)
    mentions: list[Mention] | None = None
    conflicts: list[ReaderConflict] = Field(default_factory=list)
    setting_conflicts: list[SettingConflict] = Field(default_factory=list)


class SeedSignature(Strict):
    id: str
    when: str
    tends_to: str
    status: Literal['open', 'supported', 'well_supported']
    evidence: list[str]
    origin: Literal['longstanding', 'acquired', 'unknown']


class SeedDomain(Strict):
    id: str
    label: str
    description: str
    status: Literal['open', 'supported', 'well_supported']
    known: str | None
    evidence: list[str]
    related_signatures: list[str]
    soft_prior: str | None = None
    soft_prior_evidence: list[str] = Field(default_factory=list)


class LifeElement(Strict):
    id: str
    kind: str
    known: str
    evidence: list[str]
    open: list[str]
    major_unknowns: list[str]


class Phase(LifeElement):
    order: int | None = None


class Relationship(LifeElement):
    function: str | None = None
    tone_now: str | None = None


class Transition(LifeElement):
    from_ref: str | None = Field(default=None, alias='from')
    to: str | None = None
    meaning_then: str | None = None
    meaning_now: str | None = None


class Trajectory(Strict):
    tendency: str
    origin: Literal['longstanding', 'acquired', 'unknown']
    evidence: list[str]
    linked_to: str | None = None


class DetailPolicy(Strict):
    minor_details: str
    major_unknowns: str


class SeedLife(Strict):
    phases: list[Phase]
    relationships: list[Relationship]
    transitions: list[Transition]
    events: list[LifeElement]
    trajectories: list[Trajectory]
    past_detail_policy: DetailPolicy


class Tension(Strict):
    id: str
    a: str
    b: str
    status: Literal['unresolved'] = 'unresolved'


class SeedModel(Strict):
    version: Literal[1] = 1
    character_id: str
    display_name: str
    summary: list[str]
    signatures: list[SeedSignature]
    domains: list[SeedDomain]
    life_history: SeedLife
    tensions: list[Tension]


class SecondPerson(Strict):
    default: str
    when_needed: str


class Length(Strict):
    default: str
    expand_when: str


class SeedVoice(Strict):
    first_person: str
    second_person: SecondPerson
    speech_register: str = Field(alias='register')
    length: Length
    sentence_endings: list[str] = Field(default_factory=list)
    allow_disagreement_and_negative_emotion: bool = True


class ExpressionItem(Strict):
    ref: str
    content: str
    elaborate: Elaboration


class RelationshipContext(Strict):
    stance: str
    not_fixed: list[str]


class Existence(Strict):
    form: str
    daily_life: str
    open: list[str]


class SeedExpression(Strict):
    voice: SeedVoice
    demeanor: list[str]
    preferences: list[str]
    relationship_context: RelationshipContext
    not_adopted_as_goal: list[str]
    existence: Existence
    items: list[ExpressionItem]
    elaborate_default: Elaboration
    current_life: list[ExpressionItem]


class WorldNotes(Strict):
    naming: str
    daily_life: str
    money: str
    calendar: str


class SeedWorld(Strict):
    era: str = Field(min_length=1)
    region: str = Field(min_length=1)
    reality: Literal['現実どおり', '少し不思議', 'ファンタジー', 'SF']
    being: str = Field(min_length=1)
    contact: Literal['A', 'B1', 'B2', 'C']
    contact_note: str
    notes: WorldNotes


class Gate(Strict):
    days: int = Field(default=0, ge=0)
    deep_moments: int = Field(default=0, ge=0)
    user_disclosures: int = Field(default=0, ge=0)


class Gates(Strict):
    L1: Gate = Field(default_factory=Gate)
    L2: Gate = Field(default_factory=lambda: Gate(days=3, deep_moments=2, user_disclosures=1))
    L3: Gate = Field(default_factory=lambda: Gate(days=6, deep_moments=4, user_disclosures=2))


class SeedSecret(Strict):
    id: str
    life_ref: str
    about: str
    unknowns: list[str]
    constraints: list[str]


class SeedReveal(Strict):
    generation: Literal['auto', 'author_approval'] = 'auto'
    gates: Gates = Field(default_factory=Gates)
    secrets: list[SeedSecret]


class InitialInterest(Strict):
    id: str
    topic: str
    kind: Literal['like', 'dislike', 'curiosity', 'into', 'opinion']
    detail: str
    origin: Literal['author'] = 'author'
    status: Literal['established'] = 'established'
    elaborate: Elaboration
    evidence: list[str]


class SeedMemory(Strict):
    id: str
    kind: Literal['daily_life', 'past_detail']
    content: str
    event_date: date
    life_ref: str | None = None


class CharacterPackage(Strict):
    llm_role: ClassVar[str] = 'steward'
    model: SeedModel
    expression: SeedExpression
    world: SeedWorld
    reveal: SeedReveal
    interests: list[InitialInterest]
    seed_memories: list[SeedMemory] = Field(min_length=8, max_length=12)


class BootstrapJudgment(Strict):
    problems: list[str]


class AuthorReview(Strict):
    ref: str = Field(min_length=1)
    supplemented: str = Field(min_length=1)
    based_on: str = Field(min_length=1)


class BootstrapSummary(Strict):
    setting_conflicts: list[SettingConflict] = Field(default_factory=list)
    llm_role: ClassVar[str] = 'steward'
    introduction: str
    open_now: list[str]
    author_review: list[AuthorReview] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_review_refs(self):
        if len({item.ref for item in self.author_review}) != len(self.author_review):
            raise ValueError('author_review refs must be unique')
        return self


class AugmentOutput(Strict):
    llm_role: ClassVar[str] = 'steward'
    domains: list[SeedDomain]
