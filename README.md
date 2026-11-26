# Git 回归定位台（Flask · 并行 Bisect）

发布工程师在**内置的合并历史**中选择一个「已知失败提交」和一个「已通过的祖先」，
启动一轮回归定位。页面以最多 **2 个并发实测槽位**调度，每个实测任务都用
`git archive <sha>` 把**该提交自己的文件**解到独立临时目录，再运行该提交内的
`qa/run_check.py`（0=通过，1=失败，77=跳过）。

- 通过 ⇒ 其所有**祖先**视为通过
- 失败 ⇒ 其所有**后代**视为失败
- 跳过 / 测试出错 ⇒ **未知**，不产生推导
- **全站共用 2 个实测槽位**（一个进程级 FIFO `SlotScheduler`），多个标签页 / 轮次一起排队
- 提交只有在**真正拿到全站槽位并通过开跑前复核后**才创建 Job、才显示“实测中”；
  在此之前是独立的 `queued`（排队等槽位）状态，不计入 `active_count`
- 有空槽就按 FIFO 自动放行下一个等待者并补测“**尚未尝试**且当前**推不出结果**”的提交；
  **自动补测在获准执行前，会按本轮最新事实再算一次推导闭包**：若已可推断则直接跳过、
  不启动测试（因此不会再用“排队期间被推及、却强行实测出的相反结果”制造冲突）
- 已获准 / 已开跑的测试即使后来可被推导，也**不取消**，一律等待实测并计入结论
- 已知 / 实测事实中出现「**祖先失败、后代通过**」时，标出全部冲突提交；
  立即取消本轮所有**尚未拿到槽位的排队请求**、停止补测，并排空在跑任务后，
  本轮以 `conflict` 结束（其他轮次的排队请求不受影响，空槽按 FIFO 让给它们）
- 在「失败端点 + 其全部祖先」范围内，列出**所有可能的最早失败提交**

## Docker Compose 启动（推荐）

```bash
docker compose up --build
```

然后打开（三个宿主端口都映射到同一服务，任选其一）：

- <http://127.0.0.1:5000>
- <http://127.0.0.1:8000>
- <http://127.0.0.1:8080>

容器内 gunicorn 单 worker / 8 线程，同时监听 5000 与 8000；轮次状态保存在进程内存中。
首次启动会在 `/data/history-repo` 自动构建一份确定性的内置合并历史
（命名卷 `history-data`，带版本标签，版本变化会自动重建）。

想让实测瞬间完成（例如自动化校验），把 compose 里的
`TEST_DELAY_MIN` / `TEST_DELAY_MAX` 都改成 `"0"` 即可。

## 本地直接运行

需要 Python 3.11+、git：

```bash
python3 -m pip install -r requirements.txt
python3 -m app.main          # 默认 http://127.0.0.1:5000
# 或：gunicorn -w1 --threads 8 -b 0.0.0.0:5000 wsgi:application
```

可用环境变量：`HISTORY_REPO`、`WORK_ROOT`、`TEST_DELAY_MIN`、`TEST_DELAY_MAX`、`PORT`。

## 内置历史与两个演示场景

`app/fixture.py` 用 git 确定性生成 19 个提交、含 3 个合并提交的 DAG：

```
r0 ─ r1 ─ r2 [baseline-good]
        ├─ A1 ✓ ─ A2 ✗ ─ A3 ✗ ─┐
        ├─ B1 ✓ ─ B2 ✗ ─ B3 ✗ ─┼─ merge ×3 ─ h1 [main / 已知失败]
        └─ D1 ✓ ─ D2 ⏭ ─ D3 ✗ ─┘
        └─ Q1 ✗ ─ Q2 ✓(修复) ─ Q3 ✗(再回归) [conflict-bad]
```

1. **合并列车**（`main` vs `baseline-good`）：三条并行特性线各有问题，其中 D2 被跳过。
   轮次正常 `complete`，最终候选恰好是 4 个：`A2`、`B2`、`D2(跳过)`、`D3`
   —— 跳过点与其失败后代都可能是最早失败提交。
2. **冲突演示**（`conflict-bad` vs `baseline-good`）：两个种子之间恰好只有
   `Q1`、`Q2` 两个未决提交，两个槽位会同时开跑；实测 `Q1 失败`、`Q2 通过`，
   稳定得到 `Q1(失败祖先) ↔ Q2(通过后代)` 冲突，本轮以 `conflict` 结束。

页面也支持两个下拉框任意选择（会校验 `good` 必须是 `bad` 的祖先），
或展开任一提交行，用「设为已知失败端点 / 设为已通过祖先」直接选择。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/history` | 全部提交、父边、引用、lane 布局、预设 |
| POST | `/api/sessions` | body `{"bad":"main","good":"baseline-good"}`，创建并启动一轮 |
| GET | `/api/sessions/<id>` | 轮次快照：节点状态/推导来源、`running_jobs`、`queued_jobs`、`slot_pool`、任务、计数、候选、冲突、事件 |
| POST | `/api/sessions/<id>/retry` | body `{"sha":"..."}`，手动重测跳过/出错提交（同样走全站 2 槽，槽满时排队而非报错） |

节点状态：`known-bad / known-good / test-pass / test-fail / skip / error /
running / queued / deduced-pass / deduced-fail / pending / outside`。
其中 `queued` 表示“本轮想要、但还没拿到全站槽位”，节点同时带 `queued:true`、
`queue_position`、`global_queue_position`；快照另有 `queued_count` / `waiting_count`
（及同义的 `waiting_jobs` / `queued_shas`），`active_count` 只数真正在跑的测试，
`slot_pool={slots,active,waiting,free}` 是全站槽位视图。

## 核心实现

- `app/graph.py`：提交 DAG、祖先/后代闭包、ref 解析、git-graph lane 布局。
- `app/history.py`：从内置 git 仓库读取拓扑与预设；校验祖先关系。
- `app/engine.py`：纯规则 + `BisectSession` 状态机。
  - `implied_sets`：通过→祖先闭包、失败→后代闭包；
  - `choose_next`：在范围内挑选未尝试、不在两个推导闭包中的提交，按 bisect
    信息量（两侧新增覆盖数）排序；
  - `earliest_failure_candidates`：范围内、非已知/推导通过、严格祖先中无失败事实；
  - `find_conflicts`：枚举「失败事实 f 是通过事实 p 的祖先」的事实对；
  - 规划（`_kick`/`_plan_locked`，维护 `inflight ∪ queued ≤ concurrency`）、
    槽位预留、开跑前复核准入（`_admit_locked`）、完成/冲突转移全部在锁内，
    测试在线程中执行；锁顺序单向（调度器锁不回调会话，回调时不持调度器锁）。
- `app/scheduler.py`：`SlotScheduler(2)` 进程级 FIFO 槽位池。`submit` 在有空槽时
  同步预留，否则进全局 FIFO；`release` 把空槽直接移交给最早的等待请求，由对应
  会话做最新事实复核，被拒则继续移交下一个；`cancel` 用于冲突时撤销本轮排队请求。
  网页后端 `app/server.py` 只创建一个实例并注入所有 `BisectSession`，因此全站最多
  两个真正执行的实测。
- `app/runner.py`：每任务独立临时目录 + `git archive` 取该 sha 的文件，
  `python -I qa/run_check.py` 执行；解析 `READ <file>` 作为“读取了哪些提交文件”的证据；
  `GlobalSlotGate(2)` 保证**整个进程**同时执行的实测不超过 2 个。
- `app/fixture.py`：确定性构建内置合并历史。
- `app/server.py` / `app/templates` / `app/static`：Flask API 与无外部依赖的原生 JS 页面
  （DAG 行图、实时轮询、槽位卡片、候选/冲突面板、事件日志、输出展开、localStorage 重连）。

## 自测结果（本次修复后实际执行）

在没有 Docker 的开发环境中，把 Flask/gunicorn/pytest/requests/PyYAML 装到隔离目录后执行：

```
PYTHONPATH=/tmp/pylibs python3 -m pytest
# 43 passed
```

原有 37 个测试全部保持通过；新增 `tests/test_slot_scheduling.py` 6 个回归测试并重复
运行 10 次无抖动，覆盖：

- 槽位被先开轮次占满时，后开轮次 `active_count=0`、`running_jobs=[]`、两个提交为
  `queued`（不是 `running`），且执行器确实没被调用；
- 排队提交在拿到槽位的准入瞬间按最新事实复核：构造“后代先通过 ⇒ 排队祖先已可推导通过，
  但该祖先配置为失败”的场景，断言它**从未执行**、不产生冲突，节点转为 `deduced-pass`；
- 空槽后轮次自动续跑直到完成（全 skip 时 4 个提交全部实测）；
- 冲突时撤销仍在排队的第三个请求且不再提交，另一个已开跑任务保留到完成并计入结论；
- 手动 retry 在全站槽满时进入队列、空槽后自动执行；
- Flask test client 双轮次共享一个 `SlotScheduler(2)`，全程 `active_count ≤ 2`、
  真实 runner `max_active ≤ 2`，两轮都到终态。

另做过 15 组 ×4 并发轮次的随机结果压测：真实并发始终 ≤2、无死锁、终态后
`scheduler.active_count()==waiting_count()==0`、各轮 `inflight/queued` 均清空。

用真实 gunicorn（`--workers 1 --threads 16`，`TEST_DELAY_MIN=1.2/MAX=2.2`，
真实 fixture 仓库）跑过：

- `scripts/live_smoke.py http://127.0.0.1:5055` → `LIVE_SMOKE_OK`：页面与静态资源 200、
  19 提交、非祖先 400、冲突对仍为 `b5be1bf ✗ ↔ 2c3b8b8 ✓` 且 `tested=2`、
  合并列车候选恰为 4、`max_active=2` 无超限；
- 两个重叠的合并列车轮次：后开轮次 POST 即刻 `active=0 queued=2`，时间线清楚看到
  FIFO 交接（如 `A 2跑0排 / B 0跑2排` → `A 1跑1排 / B 1跑1排` →
  `A 0跑2排 / B 2跑0排`），任意时刻两轮 `active_count` 之和恰 ≤2，最终都 `complete`、
  各 4 候选；
- 慢合并列车占用两槽时启动冲突轮：冲突轮先 `active=0 queued=2`，拿到槽后两个实测跑完，
  稳定 `conflict`、`tested=2`，后台合并列车轮仍正常 `complete`（4 候选）。

还用 jsdom 起两个独立 window 驱动**真实页面**模拟两个标签页（脚本在 `/tmp/jstest`，
不在仓库内）：

- 标签 1 出现 2 张“实测中”卡片后，标签 2 点启动，立即出现 2 张
  `data-testid="queued-job"` 排队卡片、0 张实测卡片，DAG 行徽标为“排队等槽位”，
  槽位面板显示“全站实测槽位 2 / 2 · 全站排队 2 个”，且无 JS 运行时错误；
- 两个标签最终都到“定位结束”；
- 对真实 fixture 服务另跑 jsdom：冲突预设渲染 1 个冲突对
  （`b5be1bf`/`2c3b8b8`），合并列车预设渲染恰好 4 个候选，无 JS 错误。

## 未完成 / 未在本环境验证的事项

- 本环境**没有 docker / docker compose 可执行文件，也没有 `/var/run/docker.sock`
  或任何容器运行时，进程 capabilities 为空、非 root 且无 sudo**，因此
  `docker compose up --build` 无法在此实际构建/启动，也无法在此给出真实浏览器标签页
  截图。已做的等价核对：compose YAML 解析通过；新增 `app/scheduler.py` 会被
  Dockerfile 的 `COPY app ./app` 带入镜像；容器所用的同一条多 `--bind` gunicorn
  命令（在本机用 5061/5062 两个端口）实际启动且两端口 `/api/health` 均 200；
  并用本机 gunicorn + jsdom 双标签完成了上述重叠轮次演示。首次镜像构建需要联网
  拉取 `python:3.11-slim`、apt 安装 git、pip 安装 requirements。
- 轮次状态仅保存在单进程内存中（刻意匹配单 worker），服务重启后旧轮次不可查询；
  浏览器会尝试用 localStorage 中的 id 重连，404 时自动清除。
- QA 时长用可配置的随机 sleep 模拟，便于观察并发；不是真实 CI 时长。
- 无鉴权 / 多用户隔离；定位为内网发布工具演示页。
