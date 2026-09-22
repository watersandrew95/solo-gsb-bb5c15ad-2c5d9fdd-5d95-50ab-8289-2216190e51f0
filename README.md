# 智能组卷 API

面向教研组的约束组卷服务：提交**题库快照**与**总分、题型数量、知识点覆盖、难度区间、互斥题组**等约束，自动生成分值精确、约束满足、结果可复现的试卷；多组可行解按 **难度偏差 → 重复知识点数 → 题目编号序列** 依次择优；无解时返回经逐项删减验证的**最小冲突约束集**。

支持一次请求生成 **2～5 份平行卷**（`paper_count`）：所有卷在同一个 CP-SAT 联合模型中分别满足全部单卷约束，并限制任意两卷共同题数不超过 `max_shared_questions`；多卷可行解按 **各卷难度偏差总和 → 跨卷重复题总数 → 各卷题号序列** 依次择优，无解时冲突集区分**单卷约束**（`per_paper`）与**跨卷重叠上限**（`cross_paper`）。不传 `paper_count` 时为单卷，请求与响应结构完全保持兼容。

还支持一次请求返回 **1～5 个候选卷组**（`alternative_count`，缺省 `1`）：在同一冻结题库与原约束下，按上述既有择优规则依次给出排名候选；后续候选与此前每个候选的**每张对应卷**至少有 `min_question_changes`（多候选缺省 `1`）道题不同，且多卷时**不能仅靠交换卷序**制造新候选。差异约束只增不删、绝不放宽原约束；候选不足时返回实际数量与 `exhausted: true`。`alternative_count` 缺省（或为 `1`）时响应结构与旧版完全一致。

此外支持对已完成且可行的任务**定向改卷**（`POST /api/tasks/{task_id}/revise`）：按卷指定必须保留/移除的题号，联合重求解全部卷并在满足原约束的前提下**最大化各卷保留原题总数**，详见下文[定向改卷](#定向改卷post-apitaskstask_idrevise)。

还提供**盲审分配**（`POST /api/tasks/{task_id}/review-plan`）：为已完成且结果可行的组卷任务把审阅人**联合分配**到各卷——满足同卷不重复、回避（不可审卷号）与总容量约束，并依次最小化**最大个人负载 → 负载相对平均值的绝对偏差总和 → 按卷号展平的审阅人编号序列**，结果可复现；无解时返回各卷可用人力与总容量缺口，绝不返回部分方案，详见下文[盲审分配](#盲审分配post-apitaskstask_idreview-plan)。

还提供**可版本化的组卷模板**（`POST /api/templates`）：把题库无关的约束配置（总分、题型数量、知识点覆盖、难度区间、互斥题组、多卷/候选参数）保存为**创建后不可修改**的模板；用 `POST /api/templates/{template_id}/tasks` 以新的题库快照实例化组卷任务时，可通过白名单 `overrides` 覆盖部分约束，**实际生效配置与 `template_id`/`version` 一并冻结进任务记录**，详见下文[组卷模板](#组卷模板可版本化)。

另提供**题目曝光配额**模块（`PUT /api/quotas`）：按题号设置可用次数；创建任务时声明 `use_quota: true` 后，任务完成可行组卷即在 **SQLite 事务中按题号原子扣减**——并发任务串行化校验，任一题余额不足则整组不扣减、任务返回 `quota_exceeded` 及缺额明细；失败或无解任务不消耗配额，详见下文[题目曝光配额](#题目曝光配额use_quota)。

每个异步任务还附带一条**防篡改审计链**（`GET /api/tasks/{task_id}/audit`）：任务创建、开始、完成或失败、配额结算均在**对应业务写入的同一 SQLite 事务内**追加哈希链接事件（规范化 JSON + SHA-256，含前驱哈希）；接口重放整条链并与任务/配额业务表交叉校验，发现序号、前驱哈希或内容摘要异常时返回**首个断点**，绝不伪报通过，详见下文[防篡改审计链](#防篡改审计链)。

技术栈：Python · FastAPI · SQLite · OR-Tools CP-SAT · Docker / Compose。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build        # 前台运行；加 -d 后台运行
```

容器启动时自动安装依赖并创建 SQLite 数据库（含 tasks、templates、quotas、quota_reservations 与 task_audit_events 表），无需任何手动初始化。

- API 地址：http://localhost:8000
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 修改）
- 必要配置（环境变量，均有默认值，一般无需修改）：
  - `EXAM_DB_PATH`：SQLite 数据库文件路径，默认 `/data/exam.db`（已挂载为持久卷 `exam_data`，删除卷 `docker compose down -v` 会清空全部任务、模板与配额数据）
- 交互式文档（Swagger）：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/health`（Compose 已配置同路径 healthcheck）
- 查看日志：`docker compose logs -f exam-api`；停止：`docker compose down`

### 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
EXAM_DB_PATH=./exam.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建组卷任务（异步），返回 `task_id`；`use_quota: true` 时启用题目曝光配额 |
| `POST` | `/api/tasks/{task_id}/revise` | 在已完成且可行的任务上创建**定向改卷**任务（异步） |
| `POST` | `/api/tasks/{task_id}/review-plan` | 为已完成且可行的任务生成**盲审分配**方案（同步返回，不产生新任务） |
| `GET`  | `/api/tasks/{task_id}/audit` | 查询任务的**防篡改审计事件链**及重放校验结论（首个断点） |
| `GET`  | `/api/tasks/{task_id}` | 查询任务状态与结果（启用配额的任务附本次预留及剩余次数） |
| `POST` | `/api/templates` | 创建组卷模板（不可变）；带 `base_template_id` 时创建该谱系的新版本 |
| `GET`  | `/api/templates/{template_id}` | 查询模板的冻结配置与版本信息 |
| `POST` | `/api/templates/{template_id}/tasks` | 用模板 + 新题库快照实例化组卷任务（异步，支持白名单 `overrides` 与 `use_quota`） |
| `PUT`  | `/api/quotas` | 按题号**设置**曝光配额可用次数（绝对值，存在即覆盖） |
| `GET`  | `/api/quotas` | 查询全部题号的当前剩余可用次数 |
| `GET`  | `/health` | 健康检查 |

任务状态：`pending` → `running` → `done`；结果在 `result` 字段中返回（`feasible` / `infeasible` / `error` / `quota_exceeded`）。

## 请求示例

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [
      {"id": 1, "score": 5,  "type": "single_choice", "difficulty": 0.2, "knowledge_points": ["集合"]},
      {"id": 2, "score": 5,  "type": "single_choice", "difficulty": 0.5, "knowledge_points": ["函数"]},
      {"id": 3, "score": 5,  "type": "single_choice", "difficulty": 0.8, "knowledge_points": ["导数"]},
      {"id": 4, "score": 10, "type": "essay",         "difficulty": 0.6, "knowledge_points": ["函数", "导数"]},
      {"id": 5, "score": 10, "type": "essay",         "difficulty": 0.7, "knowledge_points": ["数列"]}
    ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "seed": 42
  }'
```

字段说明：

- `questions`：题库快照，提交后冻结。每题含 `id`（数字或字符串，唯一）、`score`（正整数）、`type`、`difficulty`（0~1）、`knowledge_points`。
- `total_score`：试卷总分，**精确等于**。
- `type_counts`：各题型的精确数量。
- `knowledge_coverage`：知识点 → 试卷中至少出现的题目数。
- `difficulty_range`：试卷平均难度（按分值加权）闭区间 `[min, max]`。
- `target_difficulty`：期望平均难度，缺省取难度区间中点；难度偏差为 `|平均难度 - 目标难度|`。
- `mutex_groups`：互斥题组，每组至多选取 1 题。
- `paper_count`：平行卷份数，取 `2`～`5` 时一次生成多份卷；缺省（或 `1`）为单卷，响应结构与旧版一致。
- `max_shared_questions`：任意两卷**共同题数上限**，仅多卷时可设置；缺省 `0`（平行卷之间不重复）。
- `alternative_count`：候选卷组数量，取 `1`～`5`，缺省 `1`（单候选，响应结构与旧版完全一致）。
- `min_question_changes`：后续候选与此前每个候选的**每张对应卷**至少不同的题目数（按对称差计），多候选时缺省 `1`；多卷任务下仅交换卷序不算新候选。
- `use_quota`：是否启用**题目曝光配额**（缺省 `false`）；启用后任务完成可行组卷时按题号原子扣减配额，详见下文[题目曝光配额](#题目曝光配额use_quota)。
- `seed`：随机种子（默认 42），相同请求 + 相同种子结果可复现。

## 多卷请求示例（2～5 份平行卷）

在单卷请求体上增加 `paper_count` 与 `max_shared_questions` 即可。各卷在**同一个联合 CP-SAT 模型**中分别满足总分、题型、知识点覆盖、难度区间、互斥题组约束，且任意两卷共同题数不超过上限：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·A/B/C 平行卷",
    "questions": [
      {"id": 1,  "score": 5,  "type": "single_choice", "difficulty": 0.3, "knowledge_points": ["集合"]}
      // ……题库快照（题量需足够分给各卷，允许部分题目各卷都不选）
    ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "mutex_groups": [[12, 13]],
    "paper_count": 3,
    "max_shared_questions": 1,
    "seed": 42
  }'
```

择优顺序（逐层最优后再优化下一层，保证确定性、与卷的排列无关）：

1. **各卷难度偏差总和**最小；
2. **跨卷重复题总数**（所有卷对共同题数求和）最少；
3. **各卷题号序列**按卷序依次最小（同时唯一确定卷的排列）。

### 多卷可行结果

结果在 `papers` 数组中逐卷返回（每卷字段与单卷结果一致），并附跨卷统计：

```json
{
  "status": "feasible",
  "paper_count": 3,
  "max_shared_questions": 1,
  "papers": [
    {
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2}
    }
  ],
  "total_difficulty_deviation": 0.04,
  "total_shared_questions": 1,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 1, "question_ids": [9]},
    {"papers": [0, 2], "count": 0, "question_ids": []},
    {"papers": [1, 2], "count": 0, "question_ids": []}
  ],
  "seed": 42
}
```

- `papers[i].paper`：第 `i` 卷的完整题目对象（卷序号从 0 起）。
- `total_difficulty_deviation`：各卷 `difficulty_deviation` 之和（第 1 择优目标）。
- `total_shared_questions`：所有卷对共同题数之和（第 2 择优目标），`shared_question_pairs` 给出每对卷的共同题号，每对的 `count` 均不超过 `max_shared_questions`。

### 多卷无解结果（单卷约束 vs 跨卷上限）

多卷无解时，`conflict_constraints` 用 `scope` 区分两类来源：

- `per_paper`：某一份卷自身的约束，`detail.paper` 标明卷序号（从 0 起）；
- `cross_paper`：跨卷共同题上限（`kind` 为 `shared_questions`，`detail.papers` 标明卷对）。

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 1, "type": "essay", "count": 2}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [1, 2], "max_shared_questions": 0}}
  ]
}
```

上例表示：第 2 卷（`paper: 1`）的题型数量约束与卷 2/卷 3 的零重叠上限相互冲突（题库中大题不足）；放宽其中任一条（例如允许两卷共用 1 题，或调大题量）后即可组卷。

## 多候选卷组（`alternative_count` 1～5）

在任意单卷/多卷请求上增加 `alternative_count`（及可选 `min_question_changes`），即可在**同一冻结题库、同一组原始约束**下一次取回多个候选卷组。求解过程：

1. 第 1 个候选按既有择优规则（单卷：难度偏差 → 重复知识点 → 题号序列；多卷：难度偏差总和 → 跨卷重复题 → 题号序列）求出；
2. 此后每轮**只追加、从不放宽**两类差异约束后重新分层择优：
   - **逐卷差异**：新候选的第 `p` 卷与此前**每个**候选的第 `p` 卷至少有 `min_question_changes` 道题不同（对单卷任务即整张卷的差异）；
   - **禁止卷序重排**（多卷）：不存在任何卷排列能让新候选与旧候选逐卷重合——不能只把 A/B 卷换个顺序充当新候选；
3. 全部候选共用同一 `seed`、单线程确定性求解，候选顺序可复现；
4. 某一轮在全部原约束 + 累计差异约束下无解（或限时内无法判定）时立即停止，返回已找到的实际数量并置 `exhausted: true`，**不会为凑数放宽任何约束**。

### 请求示例（单卷任务取 3 个候选）

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·3 个候选卷",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "alternative_count": 3,
    "min_question_changes": 2,
    "seed": 42
  }'
```

多卷任务同理，与 `paper_count` / `max_shared_questions` 同时给出即可，例如
`"paper_count": 3, "max_shared_questions": 0, "alternative_count": 4, "min_question_changes": 3`：
返回的每个候选都是一个完整的 3 卷平行卷组，候选间 3 张对应卷两两至少差 3 道题。

### 多候选响应

单候选（缺省 `alternative_count: 1`）响应与旧版**完全一致**；多候选时结果放在 `alternatives` 数组中，按既有择优规则从 `rank: 1` 起排序，**每组都带原有全部统计字段**（单卷为 `selected_ids` / `average_difficulty` / `difficulty_deviation` / `type_counts` / `knowledge_point_counts` / `repeated_knowledge_points`；多卷另含 `papers` 与 `total_difficulty_deviation` / `total_shared_questions` / `shared_question_pairs`）：

```json
{
  "status": "feasible",
  "alternative_count": 3,
  "returned_count": 3,
  "min_question_changes": 2,
  "exhausted": false,
  "alternatives": [
    {
      "rank": 1,
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    },
    {
      "rank": 2,
      "selected_ids": ["/* 与 rank 1 至少 2 题不同…… */"],
      "total_score": 150,
      "average_difficulty": 0.51,
      "difficulty_deviation": 0.01,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"/* …… */": 0},
      "repeated_knowledge_points": 1,
      "paper": [ /* …… */ ]
    },
    { "rank": 3 }
  ],
  "seed": 42
}
```

多卷任务的多候选响应顶层另有 `paper_count` 与 `max_shared_questions`，每个候选元素形如：

```json
{
  "rank": 2,
  "papers": [
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]},
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]}
  ],
  "total_difficulty_deviation": 0.05,
  "total_shared_questions": 0,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 0, "question_ids": []}
  ]
}
```

字段说明：

- `alternative_count`：请求的候选数量；`returned_count`：实际返回数量；
- `exhausted`：候选是否已穷尽——`true` 表示在不放宽任何原约束的前提下无法再给出满足差异要求的候选（`returned_count < alternative_count` 时恒为 `true`）；
- `min_question_changes`：本次实际采用的逐卷最少差异题数（回显缺省值）；
- `alternatives[i].rank`：从 1 起的择优排名；排名仅依据既有择优规则，候选间的差异约束不参与排序。

> 多候选任务同样可以发起[定向改卷](#定向改卷post-apitaskstask_idrevise)：以排名第 1（择优最优）的候选为基准卷冻结；改卷任务始终只返回一个新卷组（差异最大化择优），不再产生多候选。

## 定向改卷（`POST /api/tasks/{task_id}/revise`）

在一份**已完成且可行**的组卷结果上按卷施加人工编辑：指定每份卷**必须保留**与**必须移除**的题号，服务在**联合模型中对全部卷重新求解**，得到满足编辑条件且尽量少改的新平行卷组。

- 源任务仅允许状态为 `done` 且结果为 `feasible`：任务不存在返回 `404`；未完成或不可行返回 `422`。源任务为多候选结果时，以 `rank: 1`（择优最优）候选的各卷作为改卷基准。
- 请求体逐卷给出编辑（卷序号 `paper` 从 0 起）：`retain_ids`（必须选入）、`remove_ids`（不得选入）。
  - 两者题号重叠、引用题库中不存在的题号、卷序号越界、同一卷重复指定，均返回 `422`。
- 创建后返回一个**新的异步任务**：源任务请求、题库快照与编辑条件一并冻结，**源任务保持不变**；新任务沿用源任务的 `seed`，也可再次作为源任务继续改卷。
- 求解顺序在原有三层择优之前新增第 0 层：

  1. **各卷保留源卷原题的总数最大化**（先满足全部原始约束与人工编辑，再尽量少改）；
  2. 各卷难度偏差总和（单卷为自身偏差）最小；
  3. 跨卷重复题总数（单卷为重复知识点数）最少；
  4. 各卷题号序列最小。

```bash
curl -X POST http://localhost:8000/api/tasks/42/revise \
  -H "Content-Type: application/json" \
  -d '{
    "name": "A/B 平行卷·人工微调",
    "edits": [
      {"paper": 0, "retain_ids": [9],            "remove_ids": [12, 13]},
      {"paper": 1, "retain_ids": [4, 7],         "remove_ids": []}
    ]
  }'
# -> {"task_id": 43, "status": "pending"}
```

字段说明：

- `name`：新任务名称，缺省沿用源任务名称。
- `edits`：非空数组，每份卷至多一条；未出现的卷序表示该卷无人工编辑条件（仍参与联合重求解与保留最大化）。

新任务通过 `GET /api/tasks/43` 查询，顶层与 `result` 均带 `source_task_id`（上例为 `42`）；可行时逐卷给出与源卷的差异：

- `retained_ids`：新卷选入、且源卷也有的题号（保留的原题）；
- `added_ids`：源卷没有、新卷新增的题号；
- `removed_ids`：源卷有、新卷被移走的题号（按题库规范顺序输出）。

多卷时差异位于 `papers[i]` 内：

```json
{
  "status": "feasible",
  "source_task_id": 42,
  "paper_count": 2,
  "papers": [
    {
      "selected_ids": [1, 4, 8, 9, "/* … */"],
      "retained_ids": [1, 4, 9],
      "added_ids": [8],
      "removed_ids": [12, 13],
      "total_score": 150
    },
    {
      "selected_ids": [4, 7, "/* … */"],
      "retained_ids": [7],
      "added_ids": [4],
      "removed_ids": ["/* … */"],
      "total_score": 150
    }
  ],
  "seed": 42
}
```

单卷改卷时差异字段与 `source_task_id` 直接位于结果顶层（不返回 `papers` 数组）：

```json
{
  "status": "feasible",
  "source_task_id": 7,
  "selected_ids": [1, 2, 5],
  "retained_ids": [1, 5],
  "added_ids": [2],
  "removed_ids": [3]
}
```

### 定向改卷无解

编辑条件与原有约束冲突时，`conflict_constraints` 同样返回逐项删减验证的删除极小冲突集，其中人工编辑条件为：

- `kind: "question_retain"`（必须保留）/ `"question_remove"`（必须移除）；
- `scope: "per_paper"`，`detail.paper` 为卷序号、`detail.question_id` 为题号、`detail.edit` 为“保留”/“移除”，可直接定位到“哪份卷的哪道题”。

```json
{
  "status": "infeasible",
  "source_task_id": 42,
  "message": "不存在满足全部约束及人工编辑条件的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 0, "type": "essay", "count": 4}},
    {"kind": "question_retain", "scope": "per_paper",
     "detail": {"paper": 0, "question_id": 21, "edit": "保留"}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [0, 1], "max_shared_questions": 0}}
  ]
}
```

## 盲审分配（`POST /api/tasks/{task_id}/review-plan`）

为一份**已完成且结果可行**的组卷任务生成盲审分配方案：把审阅人**联合分配**到该任务的全部卷（单卷任务即 1 份卷，多卷/多候选/模板/改卷任务按其冻结的卷数）。分配在**同一个 CP-SAT 联合模型**中求解并**同步返回**——不产生新任务、不写库；同一任务同一请求重复调用结果完全一致（可复现）。

- 源任务仅允许状态为 `done` 且结果为 `feasible`：任务不存在返回 `404`；未完成或结果不可行返回 `422`。
- 硬约束（全部必须满足，不满足即整体无解）：
  - 每卷恰好 `reviewers_per_paper` 名审阅人，**同卷审阅人不重复**；
  - **回避**：审阅人不得分配到其 `excluded_papers` 声明的卷；
  - **总容量**：每名审阅人跨卷合计分到的卷数不超过其 `capacity`。
- 择优顺序（字典序，逐层最优后固定再优化下一层）：

  1. **最大个人负载**（单人分到的卷数）最小；
  2. **各人负载相对平均值的绝对偏差总和**最小（平均负载 = 总分配数 ÷ 审阅人数）；
  3. **按卷号展平的审阅人编号序列**最小（逐卷把选中审阅人的规范排序编号列表按字典序最小化，唯一破缺对称性，保证可复现）。

### 请求示例

```bash
curl -X POST http://localhost:8000/api/tasks/42/review-plan \
  -H "Content-Type: application/json" \
  -d '{
    "reviewers": [
      {"reviewer_id": "R1", "capacity": 3, "excluded_papers": [0]},
      {"reviewer_id": "R2", "capacity": 2, "excluded_papers": []},
      {"reviewer_id": "R3", "capacity": 2, "excluded_papers": [2]},
      {"reviewer_id": "R4", "capacity": 3, "excluded_papers": []}
    ],
    "reviewers_per_paper": 2
  }'
```

字段说明：

- `reviewers`：非空数组，每个元素为一名审阅人：
  - `reviewer_id`：审阅人编号（数字或字符串），**同一请求内不可重复**（整数 `1` 与字符串 `"1"` 是两个不同的审阅人）；
  - `capacity`：可承接的最大卷数，**正整数**（非正容量返回 `422`）；
  - `excluded_papers`：不可审的卷序号（回避关系，从 `0` 起），缺省 `[]`；**越界卷号**（≥ 源任务卷数或为负）返回 `422`。
- `reviewers_per_paper`：每卷所需审阅人数，**正整数**。

### 可行结果

```json
{
  "task_id": 42,
  "status": "feasible",
  "paper_count": 3,
  "reviewers_per_paper": 2,
  "required_total": 6,
  "max_load": 2,
  "assignments": [
    {"paper": 0, "reviewer_ids": ["R2", "R3"]},
    {"paper": 1, "reviewer_ids": ["R1", "R2"]},
    {"paper": 2, "reviewer_ids": ["R1", "R4"]}
  ],
  "loads": [
    {"reviewer_id": "R1", "capacity": 3, "load": 2},
    {"reviewer_id": "R2", "capacity": 2, "load": 2},
    {"reviewer_id": "R3", "capacity": 2, "load": 1},
    {"reviewer_id": "R4", "capacity": 3, "load": 1}
  ]
}
```

- `assignments`：逐卷分配结果（按卷号升序，卷内审阅人按编号规范排序）；
- `loads`：各人实际负载（不超过其 `capacity`）；`max_load` 为第 1 择优目标的最优值；
- `required_total`：总分配数（`paper_count × reviewers_per_paper`）。

### 无解结果（不返回部分方案）

无解时**不返回任何分配**，仅给出诊断：各卷可用人力（未回避该卷的审阅人）与总容量缺口：

```json
{
  "task_id": 42,
  "status": "infeasible",
  "message": "不存在满足全部回避与容量约束的盲审分配方案",
  "paper_count": 3,
  "reviewers_per_paper": 2,
  "required_total": 6,
  "capacity_total": 2,
  "capacity_gap": 4,
  "papers": [
    {"paper": 0, "required": 2, "available_count": 2,
     "available_reviewers": ["A", "B"], "shortage": 0},
    {"paper": 1, "required": 2, "available_count": 2,
     "available_reviewers": ["A", "B"], "shortage": 0},
    {"paper": 2, "required": 2, "available_count": 1,
     "available_reviewers": ["A"], "shortage": 1}
  ]
}
```

- `capacity_total`：审阅人合计可承接卷数（每人按其未回避卷数截断后求和）；`capacity_gap = max(0, required_total - capacity_total)` 为总容量缺口；
- `papers[i]`：第 `i` 卷的所需人数、可用人数与可用审阅人名单，`shortage` 为该卷人数缺口——总容量充足但分布不均（回避过于集中）时，可据此定位问题卷。

## 组卷模板（可版本化）

模板把**题库无关的约束配置**固化为可复用的组卷蓝本：`total_score`、`type_counts`、`knowledge_coverage`、`difficulty_range`、`target_difficulty`、`mutex_groups`、`paper_count`、`max_shared_questions`、`alternative_count`、`min_question_changes`（字段语义与 `/api/tasks` 完全一致）。模板**创建后不可修改**——服务不提供任何修改/删除接口；要调整约束，只能基于既有模板**创建新版本**。

- 模板不含 `questions` / `seed` / `name`：题库快照与种子在实例化时提供；互斥题组引用的题号是否存在，也在实例化时按当次题库快照校验。
- 每个模板属于一个版本谱系（`root_id` 标识）：首版本 `version=1`，携带 `base_template_id` 创建时在该谱系内递增版本号，`base_template_id` 记录直接派生来源。谱系内每个版本都是完整、独立、不可变的配置快照。
- 实例化时**实际生效配置**（模板配置 ⊕ 白名单 overrides，含缺省值落定）与 `template_id` / `template_version` 一并冻结进任务记录；任务查询会回显这两个字段，求解流程、结果结构与 `/api/tasks` 完全一致，模板任务同样可以继续[定向改卷](#定向改卷post-apitaskstask_idrevise)。

### 创建模板（`POST /api/templates`）

```bash
curl -X POST http://localhost:8000/api/templates \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考模板",
    "config": {
      "total_score": 150,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_coverage": {"函数": 2, "导数": 1},
      "difficulty_range": [0.45, 0.6],
      "target_difficulty": 0.52,
      "mutex_groups": [[12, 13]],
      "paper_count": 2,
      "max_shared_questions": 0
    }
  }'
```

响应（`201`）回显冻结后的完整配置（缺省字段一并落定）：

```json
{
  "template_id": 1,
  "version": 1,
  "name": "高三数学月考模板",
  "root_id": 1,
  "base_template_id": null,
  "config": {
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "mutex_groups": [[12, 13]],
    "paper_count": 2,
    "max_shared_questions": 0,
    "alternative_count": 1,
    "min_question_changes": null
  },
  "created_at": "2026-09-21 12:00:00"
}
```

- `config` 校验规则与 `/api/tasks` 的约束字段一致（难度区间有序、数量为正整数、`max_shared_questions` 仅多卷可设等）；**未知字段一律 422**。
- 创建新版本：同一请求体加 `"base_template_id": 1`，新模板 `version=2`、`root_id=1`、`base_template_id=1`；`base_template_id` 不存在返回 `404`。`config` 必须完整给出（版本间不做字段级合并）。
- `GET /api/templates/{template_id}` 返回同一结构；不存在返回 `404`。

### 实例化组卷任务（`POST /api/templates/{template_id}/tasks`）

```bash
curl -X POST http://localhost:8000/api/templates/1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·9 月",
    "questions": [ /* ……本次题库快照…… */ ],
    "seed": 42,
    "overrides": {
      "difficulty_range": [0.4, 0.65],
      "knowledge_coverage": {"函数": 3, "导数": 1}
    }
  }'
# -> {"task_id": 57, "status": "pending", "template_id": 1, "template_version": 1}
```

- `questions`（必填）、`seed`、`name`、`use_quota` 为实例化请求的独立字段；`name` 缺省沿用模板名称，`use_quota` 语义与 `/api/tasks` 一致（见[题目曝光配额](#题目曝光配额use_quota)）。
- `overrides` 逐字段覆盖模板配置，**仅允许白名单字段**：`total_score`、`type_counts`、`knowledge_coverage`、`difficulty_range`、`target_difficulty`、`mutex_groups`、`paper_count`、`max_shared_questions`、`alternative_count`、`min_question_changes`；显式置 `null` 表示清除模板值、回到缺省行为（如 `"target_difficulty": null` 恢复为区间中点）。
- 白名单之外的任何字段（未知字段、拼写错误、`questions`/`seed`/`name` 等）一律 `422`，错误明细列出全部可覆盖字段。
- 合并后的生效配置连同题库快照按 `/api/tasks` 的同一套规则全量校验，**非法合并**（如把 `paper_count` 覆盖为 `1` 而模板带有 `max_shared_questions`、难度区间倒置、互斥组引用了快照中不存在的题号等）返回 `422` 与逐字段错误明细。
- 校验通过但约束本身无解时，任务正常创建，结果按既有规则返回 `infeasible` 与最小冲突约束集。

实例化任务的查询与 `/api/tasks` 完全同构，`GET /api/tasks/57` 额外回显模板溯源信息：

```json
{
  "task_id": 57,
  "status": "done",
  "name": "高三数学月考·9 月",
  "seed": 42,
  "template_id": 1,
  "template_version": 1,
  "result": {"status": "feasible", "paper_count": 2, "papers": [ /* …… */ ]}
}
```

> 直接通过 `POST /api/tasks` 创建的任务不返回 `template_id` / `template_version` 字段，原有行为完全不变。

## 题目曝光配额（`use_quota`）

按题号控制题目可被组卷使用的次数：先通过 `PUT /api/quotas` 为题号设置可用次数，创建任务时声明 `use_quota: true`，任务完成**可行组卷**时即在**单个 SQLite 事务**中按题号**原子扣减**配额。

- **扣减口径**：以排名第 1 的候选卷组为准（单候选即结果本身），按各卷选中题号的**出现次数**扣减——多卷任务中同一题被多份卷选用时按次数累计（每份卷都是一次实际曝光）。
- **串行化校验**：校验-扣减在进程内互斥锁 + `BEGIN IMMEDIATE` 事务中串行执行，并发任务不会超扣；任一题余额不足（**未设置过配额的题按余额 0 计**）则**整组不扣减**，任务结果返回 `quota_exceeded` 及逐题缺额明细。
- **失败/无解不消耗**：结果为 `infeasible` 或 `error` 的任务不扣减任何配额；定向改卷（revise）任务不参与配额扣减。
- **查询回显**：`GET /api/tasks/{task_id}` 对启用配额的任务返回 `quota` 块，含本次预留（`reserved`）及扣减后的剩余次数快照（`remaining`）；未声明 `use_quota` 的任务不返回该字段。

### 设置 / 查询配额（`PUT` / `GET /api/quotas`）

```bash
curl -X PUT http://localhost:8000/api/quotas \
  -H "Content-Type: application/json" \
  -d '{
    "quotas": [
      {"question_id": 1,    "quota": 2},
      {"question_id": 2,    "quota": 2},
      {"question_id": 5,    "quota": 2},
      {"question_id": "A-1", "quota": 10}
    ]
  }'
# -> [{"question_id": 1, "quota": 2, "updated_at": "2026-09-22 12:00:00"}, ...]

curl http://localhost:8000/api/quotas
```

- `quota` 为**绝对值**（覆盖旧值），`0` 表示该题不可用；同一请求内题号不可重复，负值返回 `422`。
- 题号类型（数字/字符串）需与题库快照中的 `id` 一致，整数 `1` 与字符串 `"1"` 是两个不同的题号。

### 创建启用配额的任务

在普通请求体上加 `"use_quota": true`（模板实例化任务同样支持该字段，为实例化请求的独立字段、不属于模板配置，也不可经 `overrides` 覆盖）：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "use_quota": true,
    "seed": 42
  }'
# -> {"task_id": 21, "status": "pending"}
```

配额充足时任务正常完成，`GET /api/tasks/21` 返回本次预留及剩余次数：

```json
{
  "task_id": 21,
  "status": "done",
  "quota": {
    "status": "reserved",
    "reservations": [
      {"question_id": 1, "reserved": 1, "remaining": 1},
      {"question_id": 2, "reserved": 1, "remaining": 1},
      {"question_id": 5, "reserved": 1, "remaining": 1}
    ]
  },
  "result": {"status": "feasible", "selected_ids": [1, 2, 5], "total_score": 20}
}
```

### 配额不足（`quota_exceeded`）

任一题余额不足时**整组不扣减**，任务仍以 `done` 完成，但结果为 `quota_exceeded` 并附缺额明细（`required` 本次需扣次数、`available` 扣前余额、`shortage` 缺额），可行卷不返回：

```json
{
  "task_id": 22,
  "status": "done",
  "quota": {"status": "exceeded", "reservations": []},
  "result": {
    "status": "quota_exceeded",
    "message": "题目曝光配额不足，本次组卷未扣减任何配额",
    "shortages": [
      {"question_id": 1, "required": 1, "available": 0, "shortage": 1}
    ]
  }
}
```

补足配额（重新 `PUT /api/quotas`）后重新提交任务即可。

## 防篡改审计链

每个异步组卷任务都附带一条**只追加、不可变**的哈希链接审计链，落库于 `task_audit_events` 表。任务生命周期与配额结算的每个关键动作都在**对应业务写入所在的现有 SQLite 事务内**追加事件——业务改动与审计事件要么同时提交、要么同时回滚：

| 事件 `type` | 追加时机（同事务的业务写入） | 最小业务载荷 `payload` |
| --- | --- | --- |
| `created` | 插入 `tasks` 行（任务创建，恒为第 1 个事件） | `name`、`seed`、`request_sha256`（冻结请求文本的 SHA-256） |
| `started` | 状态 `pending → running` | `{"status": "running"}` |
| `finished` | 结果落库（`feasible` / `infeasible` / `error` / `quota_exceeded`） | `result_status`、`result_sha256`（结果文本的 SHA-256） |
| `quota_settled` | **配额扣减/判定不扣减**（与扣减结果同一事务原子提交） | `outcome=reserved` 时为逐题 `items`（题号、`reserved` 扣减次数、`remaining` 扣后剩余快照）；`outcome=exceeded` 时为 `shortages` 缺额明细 |

普通任务的链为 `created → started → finished`（3 个事件）；声明 `use_quota` 的任务在 `finished` 后同事务追加 `quota_settled`（4 个事件）；任务尚未开始时只有 `created`。

### 哈希与规范化口径

- 事件序号 `seq`：同一任务内从 **1** 起连续递增；并发下事件追加与业务写入同受进程内互斥锁保护，同任务写入严格连续。
- 前驱哈希 `prev_hash`：等于链上前一事件的 `hash`；**首事件固定为 64 个 `0`**（创世哈希）。
- 事件哈希 `hash`：对如下事件体的**规范化 JSON**（UTF-8、对象键按字典序升序、无任何空白字符、非 ASCII 不转义）计算 SHA-256：

  ```
  {"task_id":…,"seq":…,"type":…,"created_at":…,"payload":{…},"prev_hash":"…"}
  ```

  事件体不含 `hash` 自身；`created_at` 为事件落库时刻的 UTC ISO 8601 时间（秒级，`Z` 结尾）。载荷中的 `request_sha256` / `result_sha256` 另把完整业务文本钉在链上，使链与 `tasks` 表形成交叉锚定。

### 查询审计链（`GET /api/tasks/{task_id}/audit`）

```bash
curl http://localhost:8000/api/tasks/21/audit
```

任务不存在返回 `404`。链完整时 `valid: true`，按序号返回全部事件与链尾锚点 `last_hash`：

```json
{
  "task_id": 21,
  "valid": true,
  "verified_at": "2026-09-22T12:00:05Z",
  "events": [
    {
      "seq": 1,
      "type": "created",
      "created_at": "2026-09-22T12:00:00Z",
      "payload": {
        "name": "高三数学月考",
        "seed": 42,
        "request_sha256": "57ac592d34ceb4d61403a8a3277b618335d35e71ad691d9d31aec568a…"
      },
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "hash": "cf867f54fbf0f295068395de27422de2d6929c97c3057d71e8bd8661302f9c84"
    },
    {"seq": 2, "type": "started", "created_at": "2026-09-22T12:00:00Z",
     "payload": {"status": "running"}, "prev_hash": "cf86…f9c84", "hash": "ed9b…0e802"},
    {"seq": 3, "type": "finished", "created_at": "2026-09-22T12:00:05Z",
     "payload": {"result_status": "feasible", "result_sha256": "9a79…b7dd"},
     "prev_hash": "ed9b…0e802", "hash": "458b…ac846"},
    {
      "seq": 4,
      "type": "quota_settled",
      "created_at": "2026-09-22T12:00:05Z",
      "payload": {
        "outcome": "reserved",
        "items": [
          {"question_id": 1, "reserved": 1, "remaining": 1},
          {"question_id": 2, "reserved": 1, "remaining": 0}
        ]
      },
      "prev_hash": "458b…ac846",
      "hash": "a301…7f21"
    }
  ],
  "break_point": null,
  "last_hash": "a301…7f21"
}
```

`outcome=exceeded`（配额不足、整组未扣减）时第 4 个事件形如：

```json
{
  "seq": 4,
  "type": "quota_settled",
  "payload": {
    "outcome": "exceeded",
    "shortages": [{"question_id": 2, "required": 1, "available": 0, "shortage": 1}]
  },
  "prev_hash": "…",
  "hash": "…"
}
```

### 校验结论与首个断点

接口在每次请求时**重放整条链**，按下列顺序检查，命中第一个异常即停止并在 `break_point` 返回断点（`at_seq` 定位事件序号、`kind` 异常类别、`expected`/`actual` 期望与实际值），同时 `events` 仍返回链上全部记录便于排查：

1. **序号连续性**：空链为 `empty_chain`；事件必须从 `seq=1` 起逐条 +1，断号/跳号返回 `seq_gap`；首事件类型不是 `created` 返回 `unexpected_first_event`；
2. **前驱哈希**：`prev_hash` 不等于上一事件 `hash`（首事件不等于创世哈希）返回 `prev_hash_mismatch`；
3. **内容摘要**：按同一规范化口径重算事件体哈希，与存储 `hash` 不符返回 `hash_mismatch`；载荷不是合法 JSON / 非规范化文本返回 `payload_not_json` / `payload_not_canonical`；未知事件类型返回 `unknown_event_type`；
4. **生命周期**：`created` / `started` / `finished` 各至多一次（重复为 `duplicate_event`），且必须按 `created → started → finished → quota_settled` 的顺序出现（越序或缺前驱为 `event_order`）；
5. **与业务表交叉比对**：`created` 载荷的 `request_sha256` / `name` / `seed` 与 `tasks` 行不符（`request_digest_mismatch` / `name_mismatch` / `seed_mismatch`）；`finished` 的 `result_sha256` / `result_status` 与 `tasks.result` 不符（`result_digest_mismatch` / `result_status_mismatch`）；`quota_settled` 的逐题预留/缺额与 `quota_reservations`、任务结果不符（`reservations_mismatch` / `shortages_mismatch` / `settlement_outcome_mismatch`）；
6. **状态完备性**：`running`/`done` 任务缺 `started` 事件、`done` 任务缺 `finished` 事件、声明 `use_quota` 的任务缺 `quota_settled` 事件（分别为 `started_event_missing` / `finished_event_missing` / `quota_settled_event_missing`，此时 `at_seq` 为 `null`，断点位于链尾之后）。

校验在**单一只读事务快照**中读取任务行、事件链与预留明细后重放（不存在跨连接的瞬时不一致），任何一项失败 `valid` 即为 `false`，**不存在“链被改动但仍报告通过”的情况**——即使攻击者重新计算全部事件哈希使链自洽，业务文本摘要的交叉比对也会暴露篡改。例如下例中第 2 个事件的前驱哈希被改动：

```json
{
  "task_id": 21,
  "valid": false,
  "verified_at": "2026-09-22T12:03:00Z",
  "events": [ /* …链上全部事件仍原样返回… */ ],
  "break_point": {
    "at_seq": 2,
    "kind": "prev_hash_mismatch",
    "expected": "cf867f54fbf0f295068395de27422de2d6929c97c3057d71e8bd8661302f9c84",
    "actual": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "last_hash": "a301…7f21"
}
```

> 审计事件表只追加，服务不提供任何修改/删除接口；`last_hash` 可作为外部留存的链顶锚点，与历史响应中的值逐次比对即可发现库被离线改写。

## 可行结果

```json
{
  "status": "feasible",
  "selected_ids": [1, 2, 5],
  "total_score": 20,
  "average_difficulty": 0.525,
  "difficulty_deviation": 0.025,
  "type_counts": {"single_choice": 2, "essay": 1},
  "knowledge_point_counts": {"集合": 1, "函数": 1, "数列": 1},
  "repeated_knowledge_points": 0,
  "paper": [ /* 完整题目对象 */ ]
}
```

## 无解结果（最小冲突约束集）

当约束无解时，`status` 为 `infeasible`，`conflict_constraints` 给出经**逐项删减验证**的删除极小冲突集——即其中任何一条被放宽/删除后，其余约束即可满足：

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的试卷",
  "conflict_constraints": [
    {"kind": "total_score",      "detail": {"total_score": 20}},
    {"kind": "difficulty_range", "detail": {"range": [0.9, 1.0]}}
  ]
}
```

单卷冲突项的 `kind` 取值：`total_score` / `type` / `coverage` / `difficulty_range` / `mutex`；多卷时另有跨卷项 `shared_questions`（`scope: "cross_paper"`），单卷项带 `scope: "per_paper"`。定向改卷任务的人工编辑条件为 `question_retain` / `question_remove`（均带 `scope: "per_paper"`，可定位到 `detail.paper` 与 `detail.question_id`）。
