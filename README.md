# Git 回归定位台（Flask · 并行 Bisect）

发布工程师在**内置的合并历史**中选择一个「已知失败提交」和一个「已通过的祖先」，
启动一轮回归定位。页面以最多 **2 个并发实测槽位**调度，每个实测任务都用
`git archive <sha>` 把**该提交自己的文件**解到独立临时目录，再运行该提交内的
`qa/run_check.py`（0=通过，1=失败，77=跳过）。

- 通过 ⇒ 其所有**祖先**视为通过
- 失败 ⇒ 其所有**后代**视为失败
- 跳过 / 测试出错 ⇒ **未知**，不产生推导
- 有空槽就补测「**尚未尝试**且当前**推不出结果**」的提交
- 已开跑的测试即使后来可被推导，也**不取消**，一律等待实测并计入结论
- 已知 / 实测事实中出现「**祖先失败、后代通过**」时，标出全部冲突提交；
  停止补测并排空在跑任务后，本轮以 `conflict` 结束
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
| GET | `/api/sessions/<id>` | 轮次快照：节点状态/推导来源、任务、计数、候选、冲突、事件 |
| POST | `/api/sessions/<id>/retry` | body `{"sha":"..."}`，手动重测跳过/出错提交（仍受 2 槽限制） |

节点状态：`known-bad / known-good / test-pass / test-fail / skip / error /
running / deduced-pass / deduced-fail / pending / outside`。

## 核心实现

- `app/graph.py`：提交 DAG、祖先/后代闭包、ref 解析、git-graph lane 布局。
- `app/history.py`：从内置 git 仓库读取拓扑与预设；校验祖先关系。
- `app/engine.py`：纯规则 + `BisectSession` 状态机。
  - `implied_sets`：通过→祖先闭包、失败→后代闭包；
  - `choose_next`：在范围内挑选未尝试、不在两个推导闭包中的提交，按 bisect
    信息量（两侧新增覆盖数）排序；
  - `earliest_failure_candidates`：范围内、非已知/推导通过、严格祖先中无失败事实；
  - `find_conflicts`：枚举「失败事实 f 是通过事实 p 的祖先」的事实对；
  - 槽位 / 排空 / 终态全部在锁内转移，测试在线程中执行。
- `app/runner.py`：每任务独立临时目录 + `git archive` 取该 sha 的文件，
  `python -I qa/run_check.py` 执行；解析 `READ <file>` 作为“读取了哪些提交文件”的证据；
  `GlobalSlotGate(2)` 保证**整个进程**同时执行的实测不超过 2 个。
- `app/fixture.py`：确定性构建内置合并历史。
- `app/server.py` / `app/templates` / `app/static`：Flask API 与无外部依赖的原生 JS 页面
  （DAG 行图、实时轮询、槽位卡片、候选/冲突面板、事件日志、输出展开、localStorage 重连）。

## 自测结果（交付前实际执行）

在没有 Docker 的开发环境中，把 Flask/gunicorn/pytest 装到隔离目录后执行：

```
PYTHONPATH=/tmp/pylibs python3 -m pytest
# 37 passed
```

覆盖：祖先/后代与菱形合并、两类推导、跳过无推导、冲突枚举、单/多候选、
调度只选未尝试且不可推导的提交、槽位上限与补位、排空语义、
“在跑任务即使可推导也保留并计入”、真实线程下最大并发恰为 2 且确实重叠、
全局信号量、真实 fixture 上两个预设的端到端结论、Flask API 校验/轮询/双轮次全局不超 2。

另用真实 gunicorn + 人为延迟跑过 `scripts/live_smoke.py`：

- 页面与 `/static/*` 200；历史 19 提交；非祖先选择返回 400；
- 冲突轮：全程 `active_count ≤ 2`、首个采样即见 2 个并行任务，
  得到 `b5be1bf ✗(祖先) ↔ 2c3b8b8 ✓(后代)`，`tested=2`；
- 合并列车轮：约 2–4 秒完成，`max_active=2`、无超限采样，候选恰为 4 个。

还用 jsdom 驱动真实页面验证：19 行 DAG 渲染、默认场景启动/轮询/4 候选、
提交展开输出、行内“设为祖先”、冲突预设渲染 1 个冲突对，且无 JS 运行时错误。

## 未完成 / 未在本环境验证的事项

- 本环境**没有 docker / docker compose 可执行文件**，因此 `docker compose up --build`
  未被实际构建/启动验证；已做的核对：compose YAML 解析通过、Dockerfile 关键步骤齐全、
  容器所用的同一条 gunicorn 命令在本机实际启动并通过 HTTP 自测。首次镜像构建需要联网
  拉取 `python:3.11-slim`、apt 安装 git、pip 安装 requirements。
- 轮次状态仅保存在单进程内存中（刻意匹配单 worker），服务重启后旧轮次不可查询；
  浏览器会尝试用 localStorage 中的 id 重连，404 时自动清除。
- QA 时长用可配置的随机 sleep 模拟，便于观察并发；不是真实 CI 时长。
- 无鉴权 / 多用户隔离；定位为内网发布工具演示页。
