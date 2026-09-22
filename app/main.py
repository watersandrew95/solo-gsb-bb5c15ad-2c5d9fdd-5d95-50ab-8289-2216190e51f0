"""FastAPI application: templates, task creation, revision and result query."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import asynccontextmanager
from pydantic import ValidationError

from . import database
from .review import solve_review_plan
from .schemas import (
    OVERRIDABLE_FIELDS,
    AuditEventOut,
    ExamTaskIn,
    QuotaOut,
    QuotaReservationOut,
    QuotaSetIn,
    ReviewPlanIn,
    ReviewPlanOut,
    ReviseTaskIn,
    TaskAuditOut,
    TaskCreated,
    TaskQuotaOut,
    TaskResult,
    TaskStatus,
    TemplateConfig,
    TemplateIn,
    TemplateOut,
    TemplateTaskCreated,
    TemplateTaskIn,
)
from .worker import submit


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="智能组卷 API",
    description=(
        "面向教研组的题库快照约束组卷服务（FastAPI + SQLite + OR-Tools CP-SAT），"
        "支持 2~5 份平行卷联合组卷、1~5 个差异化候选卷组，"
        "可版本化、创建后不可变的组卷模板，"
        "按题号扣减的题目曝光配额，"
        "以及面向已完成且可行任务的盲审分配"
    ),
    version="1.7.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 题目曝光配额：按题号设置/查询可用次数
# --------------------------------------------------------------------------- #
def _quota_out(row: sqlite3.Row) -> QuotaOut:
    return QuotaOut(
        question_id=json.loads(row["question_id"]),
        quota=row["quota"],
        updated_at=row["updated_at"],
    )


@app.put("/api/quotas", response_model=List[QuotaOut])
def set_quotas(payload: QuotaSetIn) -> List[QuotaOut]:
    """按题号设置曝光配额的可用次数（绝对值，存在即覆盖）。

    题号类型（数字/字符串）需与题库快照中的 id 一致；未设置过配额的题
    在启用配额的任务中按余额 0 处理。
    """
    database.set_quotas([(item.question_id, item.quota) for item in payload.quotas])
    keys = {json.dumps(item.question_id, ensure_ascii=False) for item in payload.quotas}
    return [_quota_out(r) for r in database.list_quotas() if r["question_id"] in keys]


@app.get("/api/quotas", response_model=List[QuotaOut])
def list_quotas() -> List[QuotaOut]:
    """查询全部题号的当前剩余可用次数。"""
    return [_quota_out(r) for r in database.list_quotas()]


@app.post("/api/tasks", response_model=TaskCreated, status_code=201)
def create_task(payload: ExamTaskIn) -> TaskCreated:
    """提交组卷任务：题库快照 + 总分/题型/知识点/难度/互斥题组约束。"""
    req = payload.model_dump()
    task_id = database.create_task(
        payload.name,
        json.dumps(req, ensure_ascii=False),
        payload.seed,
    )
    submit(task_id)
    return TaskCreated(task_id=task_id, status="pending")


# --------------------------------------------------------------------------- #
# 组卷模板：可版本化、创建后不可变的题库无关约束配置
# --------------------------------------------------------------------------- #
def _template_out(row: sqlite3.Row) -> TemplateOut:
    return TemplateOut(
        template_id=row["id"],
        version=row["version"],
        name=row["name"],
        root_id=row["root_id"],
        base_template_id=row["base_id"],
        config=TemplateConfig(**json.loads(row["config"])),
        created_at=row["created_at"],
    )


@app.post("/api/templates", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateIn) -> TemplateOut:
    """创建组卷模板（题库无关的约束配置，创建后不可修改）。

    携带 base_template_id 时在该模板的版本谱系上创建新版本（version 递增），
    否则创建 version=1 的全新模板；模板不提供任何修改/删除接口。
    """
    base_row = None
    if payload.base_template_id is not None:
        base_row = database.get_template(payload.base_template_id)
        if base_row is None:
            raise HTTPException(status_code=404, detail="基准模板不存在")
    config_json = json.dumps(payload.config.model_dump(), ensure_ascii=False)
    template_id, _ = database.create_template(payload.name, config_json, base_row)
    return _template_out(database.get_template(template_id))


@app.get("/api/templates/{template_id}", response_model=TemplateOut)
def get_template(template_id: int) -> TemplateOut:
    """查询模板的冻结配置与版本信息。"""
    row = database.get_template(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return _template_out(row)


def _validation_detail(exc: ValidationError) -> List[Dict[str, Any]]:
    """将 Pydantic 校验错误转换为可 JSON 序列化的 422 明细。"""
    return [
        {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]


@app.post(
    "/api/templates/{template_id}/tasks",
    response_model=TemplateTaskCreated,
    status_code=201,
)
def create_task_from_template(
    template_id: int, payload: TemplateTaskIn
) -> TemplateTaskCreated:
    """以模板为约束蓝本实例化组卷任务（异步）。

    模板配置与 overrides 逐字段合并（overrides 优先，仅限白名单字段），
    合并结果连同新的题库快照一起按 ExamTaskIn 全量校验；实际生效配置与
    template_id/template_version 一并冻结进任务记录，求解流程与 /api/tasks 一致。
    """
    row = database.get_template(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")

    # 白名单之外的任何字段（未知字段、questions/seed/name 等）一律拒绝
    unknown = sorted(set(payload.overrides) - OVERRIDABLE_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"overrides 包含不允许覆盖的字段: {unknown}；"
                f"可覆盖字段（白名单）: {sorted(OVERRIDABLE_FIELDS)}"
            ),
        )

    config = json.loads(row["config"])
    merged = {**config, **payload.overrides}
    name = payload.name if payload.name is not None else row["name"]
    try:
        effective = ExamTaskIn(
            name=name,
            questions=payload.questions,
            seed=payload.seed,
            use_quota=payload.use_quota,
            **merged,
        )
    except ValidationError as exc:
        # 非法合并：覆盖值自身非法，或与模板其余约束组合后非法
        raise HTTPException(
            status_code=422, detail=_validation_detail(exc)
        ) from exc

    # 冻结实际生效配置与模板溯源信息；kind=template 仅供识别，求解同普通任务
    frozen = effective.model_dump()
    frozen["kind"] = "template"
    frozen["template_id"] = row["id"]
    frozen["template_version"] = row["version"]
    task_id = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        payload.seed,
    )
    submit(task_id)
    return TemplateTaskCreated(
        task_id=task_id,
        status="pending",
        template_id=row["id"],
        template_version=row["version"],
    )


def _source_paper_selection(
    source_req: Dict[str, Any], source_result: Dict[str, Any]
) -> List[List[Any]]:
    """从源任务结果中提取逐卷选中题号（单卷 -> 单元素列表）。

    多候选源任务以排名第 1（择优最优）的候选为改卷基准。
    """
    paper_count = int(source_req.get("paper_count") or 1)
    alts = source_result.get("alternatives")
    if alts:
        top = min(alts, key=lambda a: a.get("rank", 1))
        if paper_count == 1:
            return [list(top.get("selected_ids") or [])]
        return [list(p.get("selected_ids") or []) for p in top.get("papers") or []]
    if paper_count == 1:
        return [list(source_result.get("selected_ids") or [])]
    return [
        list(p.get("selected_ids") or [])
        for p in source_result.get("papers") or []
    ]


@app.post("/api/tasks/{task_id}/revise", response_model=TaskCreated, status_code=201)
def revise_task(task_id: int, payload: ReviseTaskIn) -> TaskCreated:
    """基于已完成且可行的源任务创建定向改卷任务（异步）：按卷指定必须保留/移除的题号。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="源任务尚未完成，无法改卷")

    source_req = json.loads(row["request"])
    # 改卷以最近一次组卷/改卷的原始请求与源卷选择为准（支持对改卷结果再次改卷）
    if source_req.get("kind") == "revise":
        source_req = source_req["source_request"]
    if not row["result"]:
        raise HTTPException(status_code=422, detail="源任务尚无结果，无法改卷")
    source_result = json.loads(row["result"])
    if source_result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="源任务结果不可行，无法改卷")

    paper_count = int(source_req.get("paper_count") or 1)
    id_set = {q["id"] for q in source_req["questions"]}

    # 卷序号不可重复；不在 [0, paper_count) 内或题号不存在均返回 422
    seen_papers = set()
    for e in payload.edits:
        if e.paper in seen_papers:
            raise HTTPException(status_code=422, detail=f"第 {e.paper} 卷的编辑条件重复指定")
        seen_papers.add(e.paper)
        if e.paper >= paper_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"卷序号 {e.paper} 不存在：源任务共 {paper_count} 份卷"
                    f"（卷序号 0~{paper_count - 1}）"
                ),
            )
        missing = [qid for qid in (e.retain_ids + e.remove_ids) if qid not in id_set]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"第 {e.paper} 卷引用了题库中不存在的题号: {missing}",
            )

    # 冻结源请求与逐卷人工编辑条件；源任务本身保持不变
    frozen = {
        "kind": "revise",
        "source_task_id": task_id,
        "source_request": source_req,
        "source_selection": _source_paper_selection(source_req, source_result),
        "edits": [e.model_dump() for e in payload.edits],
    }
    seed = int(source_req.get("seed") or 42)
    name = payload.name if payload.name is not None else row["name"]
    new_task_id = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        seed,
    )
    submit(new_task_id)
    return TaskCreated(task_id=new_task_id, status="pending")


# --------------------------------------------------------------------------- #
# 盲审分配：为已完成且可行的组卷任务联合分配审阅人（同步求解，不写库）
# --------------------------------------------------------------------------- #
@app.post(
    "/api/tasks/{task_id}/review-plan",
    response_model=ReviewPlanOut,
    response_model_exclude_none=True,
)
def create_review_plan(task_id: int, payload: ReviewPlanIn) -> ReviewPlanOut:
    """为已完成且结果可行的组卷任务生成盲审分配方案（同步返回，不产生新任务）。

    在**同一个 CP-SAT 联合模型**中把审阅人分配到源任务的全部卷：每卷恰好
    `reviewers_per_paper` 名审阅人（同卷不重复）、审阅人回避其不可审卷号、
    每人跨卷合计不超过其容量。依次最小化：最大个人负载 → 各人负载相对
    平均值的绝对偏差总和 → 按卷号展平的审阅人编号序列（结果可复现）。
    无解时返回 `infeasible` 及各卷可用人力与总容量缺口，不返回部分方案。
    """
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="任务尚未完成，无法生成盲审分配方案")
    if not row["result"]:
        raise HTTPException(status_code=422, detail="任务尚无结果，无法生成盲审分配方案")
    result = json.loads(row["result"])
    if result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="任务结果不可行，无法生成盲审分配方案")

    # 卷数取自冻结的源请求（改卷任务溯源到原始组卷请求）
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        req = req["source_request"]
    paper_count = int(req.get("paper_count") or 1)

    # 不可审卷号越界校验（卷序号从 0 起，上界为源任务的卷数）
    for r in payload.reviewers:
        bad = [p for p in r.excluded_papers if p >= paper_count]
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"审阅人 {r.reviewer_id!r} 的不可审卷号 {bad} 越界："
                    f"共 {paper_count} 份卷（卷序号 0~{paper_count - 1}）"
                ),
            )

    plan = solve_review_plan(
        [r.model_dump() for r in payload.reviewers],
        payload.reviewers_per_paper,
        paper_count,
        seed=int(row["seed"]),
    )
    return ReviewPlanOut(task_id=task_id, **plan)


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskStatus,
    response_model_exclude_none=True,
)
def get_task(task_id: int) -> TaskStatus:
    """查询任务状态与组卷结果（可行卷或最小冲突约束集）。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    result: Optional[TaskResult] = None
    if row["result"]:
        result = TaskResult(**json.loads(row["result"]))

    source_task_id: Optional[int] = None
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        source_task_id = req.get("source_task_id")

    # 模板实例化任务：回显冻结的模板溯源信息（普通任务不返回这两个字段）
    template_id: Optional[int] = None
    template_version: Optional[int] = None
    if req.get("kind") == "template":
        template_id = req.get("template_id")
        template_version = req.get("template_version")

    # 启用配额的任务：回显本次预留及剩余次数（未声明 use_quota 的任务不返回该字段）
    quota_info: Optional[TaskQuotaOut] = None
    if req.get("use_quota"):
        quota_status: Optional[str] = None
        if row["status"] == "done" and result is not None:
            if result.status == "quota_exceeded":
                quota_status = "exceeded"
            elif result.status == "feasible":
                quota_status = "reserved"
        quota_info = TaskQuotaOut(
            status=quota_status,
            reservations=[
                QuotaReservationOut(
                    question_id=json.loads(r["question_id"]),
                    reserved=r["reserved"],
                    remaining=r["remaining"],
                )
                for r in database.get_quota_reservations(task_id)
            ],
        )

    return TaskStatus(
        task_id=row["id"],
        status=row["status"],
        name=row["name"],
        seed=row["seed"],
        created_at=row["created_at"],
        source_task_id=source_task_id,
        template_id=template_id,
        template_version=template_version,
        quota=quota_info,
        result=result,
    )


# --------------------------------------------------------------------------- #
# 防篡改审计链：重放哈希链并与业务表交叉校验，返回事件链与首个断点
# --------------------------------------------------------------------------- #
@app.get("/api/tasks/{task_id}/audit", response_model=TaskAuditOut)
def get_task_audit(task_id: int) -> TaskAuditOut:
    """查询任务的防篡改审计事件链及校验结论。

    返回按序号排列的全部事件（created / started / finished / quota_settled）
    与逐条重放 SHA-256 哈希链、交叉比对任务/配额业务表后的校验结论：

    - ``valid=true``：序号从 1 连续、前驱哈希逐环咬合、事件内容摘要全部重算
      一致，且请求/结果摘要、配额预留与业务表实际内容相符；
    - ``valid=false``：``break_point`` 给出**首个**断点的序号、类别与
      期望/实际值（不伪报通过），``events`` 仍返回链上全部记录以便定位。

    事件哈希口径：对 ``{task_id, seq, type, created_at, payload, prev_hash}``
    的规范化 JSON（UTF-8、键升序、无空白、不转义非 ASCII）计算 SHA-256；
    首事件前驱为 64 个 0。
    """
    # 任务行、事件链、预留明细在单一只读事务快照中读取并校验，避免跨连接
    # 瞬时不一致造成误报；快照内查无任务即 404
    snapshot = database.get_audit_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _, event_rows, break_point = snapshot
    events = [
        AuditEventOut(
            seq=r["seq"],
            type=r["event_type"],
            created_at=r["created_at"],
            payload=json.loads(r["payload"]),
            prev_hash=r["prev_hash"],
            hash=r["hash"],
        )
        for r in event_rows
    ]
    return TaskAuditOut(
        task_id=task_id,
        valid=break_point is None,
        verified_at=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        events=events,
        break_point=break_point,
        last_hash=event_rows[-1]["hash"] if event_rows else None,
    )
