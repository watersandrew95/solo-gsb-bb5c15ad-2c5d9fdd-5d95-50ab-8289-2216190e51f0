"""Pydantic models for the intelligent exam-paper composition API."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QuestionId = Union[int, str]


class QuestionIn(BaseModel):
    """题库快照中的一道题。"""

    id: QuestionId
    score: int = Field(..., description="题目分值（正整数）")
    type: str = Field(..., description="题型，如 single_choice / fill_blank / essay")
    difficulty: float = Field(..., ge=0.0, le=1.0, description="难度系数，区间 [0,1]")
    knowledge_points: List[str] = Field(default_factory=list, description="该题命中的知识点")

    @field_validator("score")
    @classmethod
    def _score_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("score 必须为正整数")
        return v

    @field_validator("type")
    @classmethod
    def _type_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("type 不能为空")
        return v.strip()


class ExamTaskIn(BaseModel):
    """组卷请求：题库快照 + 全部约束。"""

    name: Optional[str] = Field(None, description="任务名称，便于教研组分发管理")
    questions: List[QuestionIn] = Field(..., min_length=1, description="题库快照（提交后冻结）")
    total_score: int = Field(..., description="试卷总分（精确等于）")
    type_counts: Dict[str, int] = Field(
        default_factory=dict, description="各题型的精确数量，如 {'single_choice': 10, 'essay': 3}"
    )
    knowledge_coverage: Dict[str, int] = Field(
        default_factory=dict, description="知识点覆盖要求：知识点 -> 试卷中至少出现的题目数"
    )
    difficulty_range: List[float] = Field(
        ..., min_length=2, max_length=2, description="试卷平均难度的闭区间 [min, max]，取值 [0,1]"
    )
    target_difficulty: Optional[float] = Field(
        None, description="期望平均难度，用于计算难度偏差；缺省时取难度区间中点"
    )
    mutex_groups: List[List[QuestionId]] = Field(
        default_factory=list, description="互斥题组：每组至多选取 1 题"
    )
    paper_count: Optional[int] = Field(
        None,
        ge=1,
        le=5,
        description="平行卷份数：2~5 一次生成多份平行卷；缺省（或 1）为单卷，响应结构保持兼容",
    )
    max_shared_questions: Optional[int] = Field(
        None,
        ge=0,
        description="任意两卷共同题数上限，仅 paper_count>=2 时生效；缺省为 0（平行卷互不重复）",
    )
    alternative_count: int = Field(
        1,
        ge=1,
        le=5,
        description=(
            "候选卷组数量：同一冻结题库与原约束下按既有择优规则返回多个候选；"
            "缺省 1，响应结构与旧版完全兼容"
        ),
    )
    min_question_changes: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "后续候选与此前每个候选的每张对应卷至少不同的题目数；"
            "alternative_count>=2 时缺省 1，且不允许仅交换卷序制造新候选"
        ),
    )
    use_quota: bool = Field(
        False,
        description=(
            "是否启用题目曝光配额：启用后任务完成可行组卷时，按排名第 1 候选各卷"
            "选中题号的出现次数在 SQLite 事务中原子扣减配额；任一题余额不足则整组"
            "不扣减，任务结果返回 quota_exceeded 及缺额明细"
        ),
    )
    seed: int = Field(42, description="随机种子，保证同请求结果可复现")

    @field_validator("total_score")
    @classmethod
    def _total_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("total_score 必须为正整数")
        return v

    @model_validator(mode="after")
    def _validate(self) -> "ExamTaskIn":
        # 难度区间合法性
        lo, hi = self.difficulty_range
        if not (0.0 <= lo <= hi <= 1.0):
            raise ValueError("difficulty_range 需满足 0 <= min <= max <= 1")
        if self.target_difficulty is not None and not (0.0 <= self.target_difficulty <= 1.0):
            raise ValueError("target_difficulty 必须在 [0,1] 内")

        # 题目编号唯一
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("questions 中存在重复的题目编号 id")
        id_set = set(ids)

        # 题型数量必须为正整数
        for t, c in self.type_counts.items():
            if c <= 0:
                raise ValueError(f"题型 {t} 的数量必须为正整数")

        # 覆盖要求非负
        for kp, c in self.knowledge_coverage.items():
            if c <= 0:
                raise ValueError(f"知识点 {kp} 的覆盖数量必须为正整数")

        # 互斥题组：成员必须存在，至少 2 题才有约束意义
        for gi, group in enumerate(self.mutex_groups):
            if len(group) != len(set(group)):
                raise ValueError(f"第 {gi} 个互斥题组内存在重复题目编号")
            missing = [qid for qid in group if qid not in id_set]
            if missing:
                raise ValueError(f"第 {gi} 个互斥题组引用了题库中不存在的题目: {missing}")

        # max_shared_questions 仅在多卷请求下有意义
        if self.max_shared_questions is not None and (self.paper_count or 1) < 2:
            raise ValueError("max_shared_questions 仅在 paper_count 为 2~5 时可设置")

        # 多候选时 min_question_changes 缺省 1；单候选下该参数不参与求解
        if self.alternative_count >= 2 and self.min_question_changes is None:
            self.min_question_changes = 1
        return self


class PaperEdit(BaseModel):
    """单份卷的定向改卷条件：必须保留 / 必须移除的题号。"""

    paper: int = Field(..., ge=0, description="卷序号（从 0 起，与组卷结果中的 paper 下标一致）")
    retain_ids: List[QuestionId] = Field(
        default_factory=list, description="新卷中必须选入的题号（强制约束）"
    )
    remove_ids: List[QuestionId] = Field(
        default_factory=list, description="新卷中不得选入的题号（强制约束）"
    )

    @model_validator(mode="after")
    def _validate(self) -> "PaperEdit":
        retain = set(self.retain_ids)
        remove = set(self.remove_ids)
        overlap = retain & remove
        if overlap:
            raise ValueError(
                f"第 {self.paper} 卷中以下题号同时出现在保留与移除列表中: "
                f"{sorted(overlap, key=lambda v: str(v))}"
            )
        # 去重（保持首次出现顺序），重复指定同一条编辑没有附加语义
        self.retain_ids = list(dict.fromkeys(self.retain_ids))
        self.remove_ids = list(dict.fromkeys(self.remove_ids))
        return self


class ReviseTaskIn(BaseModel):
    """定向改卷请求：在已完成且可行的源任务上按卷指定保留/移除条件。"""

    name: Optional[str] = Field(None, description="新任务名称；缺省沿用源任务名称")
    edits: List[PaperEdit] = Field(
        ..., min_length=1, description="逐卷人工编辑条件（每份卷至多一条，卷序号不可越界）"
    )


# --------------------------------------------------------------------------- #
# 组卷模板（可版本化、创建后不可变）
# --------------------------------------------------------------------------- #
#: 实例化模板任务时允许通过 overrides 覆盖的约束字段（白名单）。
#: questions / seed / name 是实例化请求的独立字段，不属于可覆盖约束；
#: 除此之外的任何字段（含拼写错误）一律拒绝。
OVERRIDABLE_FIELDS = frozenset(
    {
        "total_score",
        "type_counts",
        "knowledge_coverage",
        "difficulty_range",
        "target_difficulty",
        "mutex_groups",
        "paper_count",
        "max_shared_questions",
        "alternative_count",
        "min_question_changes",
    }
)


class TemplateConfig(BaseModel):
    """模板冻结的题库无关约束配置（创建后不可修改）。

    字段与 ExamTaskIn 的约束字段一一对应，但不包含 questions/seed/name：
    题库快照与种子在实例化（POST /api/templates/{id}/tasks）时提供，
    互斥题组的题号存在性也在实例化时按题库快照校验。
    """

    model_config = ConfigDict(extra="forbid")

    total_score: int = Field(..., description="试卷总分（精确等于）")
    type_counts: Dict[str, int] = Field(
        default_factory=dict, description="各题型的精确数量"
    )
    knowledge_coverage: Dict[str, int] = Field(
        default_factory=dict, description="知识点 -> 试卷中至少出现的题目数"
    )
    difficulty_range: List[float] = Field(
        ..., min_length=2, max_length=2, description="试卷平均难度闭区间 [min, max]，取值 [0,1]"
    )
    target_difficulty: Optional[float] = Field(
        None, description="期望平均难度；缺省取难度区间中点"
    )
    mutex_groups: List[List[QuestionId]] = Field(
        default_factory=list,
        description="互斥题组：每组至多选取 1 题（题号存在性在实例化时校验）",
    )
    paper_count: Optional[int] = Field(
        None, ge=1, le=5, description="平行卷份数：2~5 为多卷；缺省（或 1）为单卷"
    )
    max_shared_questions: Optional[int] = Field(
        None, ge=0, description="任意两卷共同题数上限，仅 paper_count>=2 时生效"
    )
    alternative_count: int = Field(1, ge=1, le=5, description="候选卷组数量，缺省 1")
    min_question_changes: Optional[int] = Field(
        None, ge=0, description="后续候选与此前每个候选的每张对应卷至少不同的题目数"
    )

    @field_validator("total_score")
    @classmethod
    def _total_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("total_score 必须为正整数")
        return v

    @model_validator(mode="after")
    def _validate(self) -> "TemplateConfig":
        lo, hi = self.difficulty_range
        if not (0.0 <= lo <= hi <= 1.0):
            raise ValueError("difficulty_range 需满足 0 <= min <= max <= 1")
        if self.target_difficulty is not None and not (0.0 <= self.target_difficulty <= 1.0):
            raise ValueError("target_difficulty 必须在 [0,1] 内")

        for t, c in self.type_counts.items():
            if c <= 0:
                raise ValueError(f"题型 {t} 的数量必须为正整数")
        for kp, c in self.knowledge_coverage.items():
            if c <= 0:
                raise ValueError(f"知识点 {kp} 的覆盖数量必须为正整数")

        # 互斥题组：组内不可重复；题号是否存在于题库在实例化时校验
        for gi, group in enumerate(self.mutex_groups):
            if len(group) != len(set(group)):
                raise ValueError(f"第 {gi} 个互斥题组内存在重复题目编号")

        if self.max_shared_questions is not None and (self.paper_count or 1) < 2:
            raise ValueError("max_shared_questions 仅在 paper_count 为 2~5 时可设置")
        return self


class TemplateIn(BaseModel):
    """创建模板请求：完整给出约束配置；基于既有模板时创建其谱系的新版本。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, description="模板名称，便于教研组管理")
    base_template_id: Optional[int] = Field(
        None,
        description="既有模板 ID：在其版本谱系上创建新版本（version 递增）；缺省创建全新模板（version=1）",
    )
    config: TemplateConfig


class TemplateOut(BaseModel):
    """模板详情：配置为创建时冻结的内容，任何字段都不会随时间变化。"""

    template_id: int
    version: int = Field(..., description="谱系内版本号，从 1 起单调递增")
    name: Optional[str] = None
    root_id: int = Field(..., description="版本谱系根模板 ID（首版本的 template_id）")
    base_template_id: Optional[int] = Field(
        None, description="派生来源模板 ID（首版本为 null）"
    )
    config: TemplateConfig
    created_at: str


class TemplateTaskIn(BaseModel):
    """模板实例化请求：新的题库快照 + seed + 白名单约束覆盖。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, description="任务名称；缺省沿用模板名称")
    questions: List[QuestionIn] = Field(..., min_length=1, description="题库快照（提交后冻结）")
    seed: int = Field(42, description="随机种子，保证同请求结果可复现")
    use_quota: bool = Field(
        False,
        description=(
            "是否启用题目曝光配额（实例化请求的独立字段，不属于模板配置，"
            "也不可经 overrides 覆盖）；语义与 /api/tasks 的 use_quota 一致"
        ),
    )
    overrides: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "约束覆盖项：仅允许白名单字段（total_score / type_counts / knowledge_coverage / "
            "difficulty_range / target_difficulty / mutex_groups / paper_count / "
            "max_shared_questions / alternative_count / min_question_changes），"
            "逐字段覆盖模板配置；显式置 null 表示清除模板值回到缺省行为"
        ),
    )


class TemplateTaskCreated(BaseModel):
    task_id: int
    status: str
    template_id: int
    template_version: int


# --------------------------------------------------------------------------- #
# 题目曝光配额（按题号设置可用次数；启用配额的任务完成可行组卷时原子扣减）
# --------------------------------------------------------------------------- #
class QuotaSetItem(BaseModel):
    """单个题号的配额设置项。"""

    question_id: QuestionId = Field(..., description="题号（数字或字符串，与题库快照中的 id 一致）")
    quota: int = Field(..., ge=0, description="可用次数（绝对值，覆盖旧值；0 表示该题不可用）")


class QuotaSetIn(BaseModel):
    """批量设置题目曝光配额。"""

    model_config = ConfigDict(extra="forbid")

    quotas: List[QuotaSetItem] = Field(..., min_length=1, description="按题号设置可用次数")

    @model_validator(mode="after")
    def _validate(self) -> "QuotaSetIn":
        seen = set()
        for item in self.quotas:
            key = json.dumps(item.question_id, ensure_ascii=False)
            if key in seen:
                raise ValueError(f"题号 {item.question_id!r} 在请求中重复设置")
            seen.add(key)
        return self


class QuotaOut(BaseModel):
    question_id: QuestionId
    quota: int = Field(..., description="当前剩余可用次数")
    updated_at: str


class QuotaShortage(BaseModel):
    """单题缺额明细（quota_exceeded 结果中返回）。"""

    question_id: QuestionId
    required: int = Field(..., description="本次组卷需要扣减的次数")
    available: int = Field(..., description="扣减前的可用余额（无配额记录按 0 计）")
    shortage: int = Field(..., description="缺额：required - available")


class QuotaReservationOut(BaseModel):
    """本次任务对单题的预留记录（扣减后落库的快照）。"""

    question_id: QuestionId
    reserved: int = Field(..., description="本次预留（实际扣减）次数")
    remaining: int = Field(..., description="本次扣减后的剩余次数快照")


class TaskQuotaOut(BaseModel):
    """任务查询返回的配额信息（仅创建时声明 use_quota 的任务返回）。"""

    status: Optional[str] = Field(
        None,
        description="reserved（已扣减）/ exceeded（余额不足未扣减）；任务未完成时不返回该字段",
    )
    reservations: List[QuotaReservationOut] = Field(
        default_factory=list, description="本次预留明细：题号、扣减次数与扣后剩余快照"
    )


class QuestionOut(BaseModel):
    id: QuestionId
    score: int
    type: str
    difficulty: float
    knowledge_points: List[str]


class ConflictConstraint(BaseModel):
    kind: str = Field(..., description="约束类别")
    scope: Optional[str] = Field(
        None, description="per_paper（单卷约束，detail.paper 为卷序号）/ cross_paper（跨卷重叠上限）"
    )
    detail: dict = Field(..., description="约束内容（原始请求中的表达形式）")


class PaperResult(BaseModel):
    """单份卷的组卷结果（多卷响应中的 papers 元素）。"""

    paper: List[QuestionOut]
    selected_ids: List[QuestionId]
    total_score: int
    average_difficulty: float
    difficulty_deviation: float
    type_counts: Dict[str, int]
    knowledge_point_counts: Dict[str, int]
    repeated_knowledge_points: int

    # —— 定向改卷任务：相对源卷的逐题差异（仅 revise 任务返回）——
    retained_ids: Optional[List[QuestionId]] = None
    added_ids: Optional[List[QuestionId]] = None
    removed_ids: Optional[List[QuestionId]] = None


class SharedQuestionPair(BaseModel):
    papers: List[int] = Field(..., description="两份卷的序号（从 0 起）")
    count: int = Field(..., description="两卷共同题数")
    question_ids: List[QuestionId] = Field(..., description="共同题编号（按题库顺序）")


class AlternativeResult(BaseModel):
    """单个候选卷组的组卷结果（多候选响应 alternatives 数组的元素）。

    单卷任务该元素为单卷原有统计字段（selected_ids 等），多卷任务为
    papers + 跨卷统计字段；并附 rank（从 1 起的择优排名）。
    """

    rank: int = Field(..., ge=1, description="按既有择优规则的候选排名（从 1 起）")

    # —— 单卷任务：单卷原有统计 ——
    paper: Optional[List[QuestionOut]] = None
    selected_ids: Optional[List[QuestionId]] = None
    total_score: Optional[int] = None
    average_difficulty: Optional[float] = None
    difficulty_deviation: Optional[float] = None
    type_counts: Optional[Dict[str, int]] = None
    knowledge_point_counts: Optional[Dict[str, int]] = None
    repeated_knowledge_points: Optional[int] = None

    # —— 多卷任务：papers + 该卷组原有跨卷统计 ——
    papers: Optional[List[PaperResult]] = None
    total_difficulty_deviation: Optional[float] = None
    total_shared_questions: Optional[int] = None
    shared_question_pairs: Optional[List[SharedQuestionPair]] = None


class TaskResult(BaseModel):
    status: str = Field(..., description="feasible / infeasible / error / quota_exceeded")

    # —— 单卷响应（未传 paper_count 时结构保持兼容）——
    paper: Optional[List[QuestionOut]] = None
    selected_ids: Optional[List[QuestionId]] = None
    total_score: Optional[int] = None
    average_difficulty: Optional[float] = None
    difficulty_deviation: Optional[float] = None
    type_counts: Optional[Dict[str, int]] = None
    knowledge_point_counts: Optional[Dict[str, int]] = None
    repeated_knowledge_points: Optional[int] = None

    # —— 定向改卷任务：单卷响应的逐题差异（仅 revise 任务返回）——
    retained_ids: Optional[List[QuestionId]] = None
    added_ids: Optional[List[QuestionId]] = None
    removed_ids: Optional[List[QuestionId]] = None

    # —— 多卷响应（paper_count 为 2~5）——
    paper_count: Optional[int] = None
    max_shared_questions: Optional[int] = None
    papers: Optional[List[PaperResult]] = None
    total_difficulty_deviation: Optional[float] = None
    total_shared_questions: Optional[int] = None
    shared_question_pairs: Optional[List[SharedQuestionPair]] = None

    # —— 多候选响应（alternative_count 为 2~5）——
    alternative_count: Optional[int] = Field(None, description="请求的候选卷组数量")
    returned_count: Optional[int] = Field(None, description="实际返回的候选卷组数量")
    min_question_changes: Optional[int] = Field(
        None, description="后续候选与此前每个候选的每张对应卷至少不同的题目数（对称差）"
    )
    exhausted: Optional[bool] = Field(
        None,
        description=(
            "候选是否已穷尽：true 表示在不放宽任何约束的前提下无法再给出更多候选"
            "（requested_count > returned_count 时恒为 true）"
        ),
    )
    alternatives: Optional[List[AlternativeResult]] = None

    seed: Optional[int] = None
    source_task_id: Optional[int] = Field(
        None, description="定向改卷任务的源任务 ID（仅 revise 任务返回）"
    )
    conflict_constraints: Optional[List[ConflictConstraint]] = None
    shortages: Optional[List[QuotaShortage]] = Field(
        None, description="配额不足时的逐题缺额明细（仅 quota_exceeded 结果返回）"
    )
    message: Optional[str] = None
    error: Optional[str] = None


class TaskCreated(BaseModel):
    task_id: int
    status: str


# --------------------------------------------------------------------------- #
# 防篡改审计链（GET /api/tasks/{task_id}/audit）
# --------------------------------------------------------------------------- #
class AuditEventOut(BaseModel):
    """审计链上的单个事件（哈希计算口径见 README“防篡改审计链”）。"""

    seq: int = Field(..., description="同任务内从 1 起连续递增的事件序号")
    type: str = Field(
        ..., description="事件类型：created / started / finished / quota_settled"
    )
    created_at: str = Field(..., description="事件时间（UTC ISO 8601，秒级，Z 结尾）")
    payload: Dict[str, Any] = Field(
        ..., description="最小业务载荷（规范化 JSON 对象，内容摘要进入事件哈希）"
    )
    prev_hash: str = Field(
        ..., description="前一事件哈希；首事件为 64 个 0（创世哈希）"
    )
    hash: str = Field(
        ...,
        description=(
            "本事件哈希：对 {task_id, seq, type, created_at, payload, prev_hash}"
            " 的规范化 JSON（UTF-8、键升序、无空白）计算的 SHA-256"
        ),
    )


class AuditBreakOut(BaseModel):
    """首个断点定位（valid=false 时必返；不伪报通过）。"""

    at_seq: Optional[int] = Field(
        None, description="断点所在事件序号；空链或链尾缺失类问题时为 null"
    )
    kind: str = Field(
        ...,
        description=(
            "异常类别：empty_chain / seq_gap / unexpected_first_event / "
            "prev_hash_mismatch / hash_mismatch / payload_not_json / "
            "payload_not_canonical / request_digest_mismatch / "
            "result_digest_mismatch / quota_settled_event_missing 等"
        ),
    )
    expected: Optional[Any] = None
    actual: Optional[Any] = None


class TaskAuditOut(BaseModel):
    """任务审计链及重放校验结论。"""

    task_id: int
    valid: bool = Field(
        ...,
        description="链是否完整：序号连续、前驱哈希与逐事件哈希全部重算通过，"
        "且与任务/配额业务表交叉比对一致；发现任何异常即为 false",
    )
    verified_at: str = Field(..., description="校验执行时间（UTC ISO 8601，Z 结尾）")
    events: List[AuditEventOut] = Field(
        ..., description="全部事件（按 seq 升序；校验失败时同样返回，便于定位）"
    )
    break_point: Optional[AuditBreakOut] = Field(
        None, description="首个断点（valid=true 时为 null）"
    )
    last_hash: Optional[str] = Field(
        None, description="链尾事件哈希（空链时为 null），可作为外部留存的链顶锚点"
    )


class TaskStatus(BaseModel):
    task_id: int
    status: str
    name: Optional[str] = None
    seed: int
    created_at: str
    source_task_id: Optional[int] = Field(
        None, description="定向改卷任务的源任务 ID（仅 revise 任务返回）"
    )
    template_id: Optional[int] = Field(
        None, description="实例化来源模板 ID（仅模板实例化任务返回）"
    )
    template_version: Optional[int] = Field(
        None, description="实例化来源模板的版本号（仅模板实例化任务返回）"
    )
    quota: Optional[TaskQuotaOut] = Field(
        None, description="本次配额预留及剩余次数（仅创建时声明 use_quota 的任务返回）"
    )
    result: Optional[TaskResult] = None


# --------------------------------------------------------------------------- #
# 盲审分配（POST /api/tasks/{task_id}/review-plan）
# --------------------------------------------------------------------------- #
ReviewerId = Union[int, str]


class ReviewerIn(BaseModel):
    """一名盲审审阅人：编号、总容量与不可审卷号（回避关系）。"""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: ReviewerId = Field(
        ..., description="审阅人编号（数字或字符串，同一请求内唯一）"
    )
    capacity: int = Field(..., description="可承接的最大卷数（正整数，跨卷合计）")
    excluded_papers: List[int] = Field(
        default_factory=list, description="不可审的卷序号（回避关系，从 0 起）"
    )

    @field_validator("capacity")
    @classmethod
    def _capacity_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("capacity 必须为正整数")
        return v

    @field_validator("excluded_papers")
    @classmethod
    def _excluded_non_negative(cls, v: List[int]) -> List[int]:
        for p in v:
            if p < 0:
                raise ValueError("excluded_papers 中的卷序号不能为负数")
        return v

    @model_validator(mode="after")
    def _dedupe_excluded(self) -> "ReviewerIn":
        # 去重（保持首次出现顺序），重复指定同一卷号没有附加语义
        self.excluded_papers = list(dict.fromkeys(self.excluded_papers))
        return self


class ReviewPlanIn(BaseModel):
    """盲审分配请求：审阅人名单 + 每卷所需审阅人数。"""

    model_config = ConfigDict(extra="forbid")

    reviewers: List[ReviewerIn] = Field(
        ..., min_length=1, description="审阅人名单（编号不可重复）"
    )
    reviewers_per_paper: int = Field(..., description="每卷所需审阅人数（正整数）")

    @field_validator("reviewers_per_paper")
    @classmethod
    def _per_paper_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("reviewers_per_paper 必须为正整数")
        return v

    @model_validator(mode="after")
    def _unique_reviewers(self) -> "ReviewPlanIn":
        # 题号口径与配额一致：整数 1 与字符串 "1" 是两个不同的审阅人
        seen = set()
        for r in self.reviewers:
            key = json.dumps(r.reviewer_id, ensure_ascii=False)
            if key in seen:
                raise ValueError(f"审阅人编号 {r.reviewer_id!r} 在请求中重复出现")
            seen.add(key)
        return self


class ReviewAssignmentOut(BaseModel):
    """单份卷的盲审分配结果。"""

    paper: int = Field(..., description="卷序号（从 0 起，与组卷结果中的卷下标一致）")
    reviewer_ids: List[ReviewerId] = Field(
        ..., description="该卷的审阅人编号（按编号规范排序，同卷不重复）"
    )


class ReviewerLoadOut(BaseModel):
    """单名审阅人的负载（按编号规范排序返回）。"""

    reviewer_id: ReviewerId
    capacity: int = Field(..., description="声明的容量上限")
    load: int = Field(..., description="实际分到的卷数（不超过 capacity）")


class ReviewPaperManpowerOut(BaseModel):
    """单份卷的可用人力诊断（无解时返回）。"""

    paper: int = Field(..., description="卷序号（从 0 起）")
    required: int = Field(..., description="该卷所需审阅人数")
    available_count: int = Field(..., description="未回避该卷的审阅人数")
    available_reviewers: List[ReviewerId] = Field(
        ..., description="未回避该卷的审阅人编号（规范排序）"
    )
    shortage: int = Field(
        ..., description="人数缺口：max(0, required - available_count)"
    )


class ReviewPlanOut(BaseModel):
    """盲审分配结果：可行方案或（无解时的）人力诊断，绝不返回部分方案。"""

    task_id: int
    status: str = Field(..., description="feasible / infeasible / error")
    paper_count: int = Field(..., description="源任务的卷数")
    reviewers_per_paper: int = Field(..., description="每卷所需审阅人数（回显请求）")
    required_total: int = Field(
        ..., description="总分配数：paper_count × reviewers_per_paper"
    )

    # —— 可行方案 ——
    max_load: Optional[int] = Field(
        None, description="最大个人负载（第 1 择优目标）"
    )
    assignments: Optional[List[ReviewAssignmentOut]] = Field(
        None, description="逐卷分配结果（按卷号升序）"
    )
    loads: Optional[List[ReviewerLoadOut]] = Field(
        None, description="各人负载（按审阅人编号规范排序）"
    )

    # —— 无解诊断（不返回任何部分方案）——
    message: Optional[str] = None
    capacity_total: Optional[int] = Field(
        None, description="审阅人合计可承接卷数（每人按未回避卷数截断后求和）"
    )
    capacity_gap: Optional[int] = Field(
        None, description="总容量缺口：max(0, required_total - capacity_total)"
    )
    papers: Optional[List[ReviewPaperManpowerOut]] = Field(
        None, description="各卷可用人力（无解时返回）"
    )
    error: Optional[str] = None
