# 智能体团队实验台

两套并列的拓扑 + 一个本地工作台 + 多模式对比工具。
目的是**看清机制、摸清协作模式**，不是为了产出能用的剧本。

- **多模式流水线**：岗位是**无状态的一次调用**，上下文由代码按需切片装配（6 个模式）。
- **角色剧场**：每个角色是**一个有状态的扮演循环**，它的上下文就是它的全部记忆，
  存档 = 上下文检查点，读档 = 全局回退而读档者豁免。见 [角色剧场设计.md](./角色剧场设计.md)。

背景设定见 [全员存取档Meta系统设定_v7_整合版.md](./全员存取档Meta系统设定_v7_整合版.md)，
架构设计见 [全员存取档_智能体团队架构图.md](./全员存取档_智能体团队架构图.md)，
流水线实验结果见 [实验记录_协作模式对比.md](./实验记录_协作模式对比.md)。

## 六种协作模式

| 模式 | 说明 | 调用次数 |
| --- | --- | --- |
| `workshop` | `team` 的流程 **+ 审查采纳闭环**（状态提案 → 机制审查 → 有限修订） | 8 |
| `team`（默认） | 总编 → 时间线主管 → 角色编剧（**切片事实包·隔离**）→ 主笔 → 审读 | 6 |
| `team_fullctx` | 同上，但角色编剧看到**全量账本**（故意关掉隔离，做对照） | 6 |
| `single` | 一个智能体拿到全部资料，直接写稿（**单 agent 基线**） | 1 |
| `groupchat` | 五个岗位在**同一讨论串**里依次发言，再由主笔成稿（群聊拓扑） | 6 |
| `groupchat_guarded` | 群聊讨论 → **规则守门（有否决权）** → 主笔只据批准清单写稿 | 7 |

`team` 和 `single` 的对比回答"多智能体到底值不值"；`team` 和 `team_fullctx` 的对比回答
"信息隔离有没有用"；`groupchat` 和 `groupchat_guarded` 的对比回答
"群聊跑偏能不能靠一道否决闸修好"。

> 后两组对比同时改变了不止一个变量（`groupchat_guarded` 比 `groupchat` 多了守门这一次调用），
> 所以"守门更优"目前是**待验证假说**，不是结论。详见实验报告。

## 跑起来

```bash
uv sync

# 1) 本地创作工作台（推荐：草稿 → 审查 → 采纳闭环）
uv run uvicorn agentteam.server:app --port 8765
#    浏览器打开 http://127.0.0.1:8765

# 2) 命令行版
uv run python -m agentteam.main --mock                  # 假模型，不联网不花钱
uv run python -m agentteam.main                         # 默认 team 模式 + stepfun

# 3) 多模式对比实验（会出一份对照报告）
uv run python -m agentteam.compare --modes team single --mock
uv run python -m agentteam.compare --modes team single team_fullctx groupchat groupchat_guarded \
    --judge --judge-provider longcat

# 4) 换评审模型重评已有稿子（隔离"评审"这个变量）
uv run python -m agentteam.rejudge runs/<时间戳>_compare --provider stepfun

# 5) 角色剧场（另一种拓扑：上下文即记忆）
uv run python -m agentteam.theater.run --mock --turns 4              # 走完存读档，不花钱
uv run python -m agentteam.theater.run --turns 6                     # 真模型
uv run python -m agentteam.theater.run --beats data/beats_双线夜戏.json   # 双线场景
```

> **评审务必跨族。** 实验发现：用写作模型自己评审自己的产出，会放过机制硬伤
> （stepfun 给 5/5 的地方，longcat 抓出"读档后不该继续复盘"）。详见实验报告。

## 创作工作台：草稿 → 审查 → 采纳

工作台把"一段戏"当成**候选草稿**，而不是直接写进账本：

```
本场要求 ──▶ 生成草稿（含正文 + 状态提案）
                     │
                     ▼
              结构化机制审查 ──pass──▶ 待采纳
                     │                    │
                   revise              （人工）采纳
                     │                    │
                     ▼                    ▼
              定向修改（≤2 轮）      写回账本，推进版本
                     └──▶ 重新审查
```

- 正文和**状态提案**（本场发生了什么事、谁的知识变了、伏笔、玩家已见）分开落盘，提案只是候选。
- 审查按**权威机制原文**判定，输出 `pass / revise / needs_input`。规则原文由
  `agentteam/rules.py` 从设定文档**逐字抽取**（R01–R09，带原文行号），不是改写过的第二套规则。
- 采纳是**幂等**的，且有多道闸：正文或提案在审查后被改过要重查；账本变了要重查；
  规则或人物卡变了要重查；mock 稿不能采纳。
- 采纳后账本追加一个新版本（`scene_<run_id>`）并 `revision += 1`，下一场从这里接着写。
- **存/读/删档与版本跳跃目前不可采纳**：那需要完整的目标快照，原型还没有这个模型，
  所以审查会把这类剧情标成 `needs_input` 而不是伪造过去的状态。

状态存在 `data/workshop.sqlite3`（本地运行时状态，已 gitignore；每次启动自动建表）。

## 模型接入

`.env` 里已配好从 ZCode 取的两个 provider：

| provider | 接口 | 模型 | 备注 |
| --- | --- | --- | --- |
| `stepfun`（默认） | `api.stepfun.com/step_plan/v1` | `step-5-preview` | 额度多 |
| `longcat` | `api.longcat.chat/openai/v1` | `LongCat-2.5-Preview` | 额度少 |

换 provider：改 `.env` 的 `LLM_PROVIDER`，或在网页上选，或加 `--provider longcat`。

### 推理档位

step-5-preview 支持 `disabled / low / medium / high`，通过 `reasoning_effort` 传。

在一个"需要推理、输出很短"的问题上实测：

| 档位 | 耗时 | 输出 token |
| --- | --- | --- |
| disabled | 36.0s | 2782 |
| **low** | **12.7s** | 1035 |
| medium | 41.1s | 3247 |
| high | 43.1s | 3305 |

**但别外推**：长文创作里**输出长度主导耗时**，档位影响很小。实测 `low` 跑 team 用了 250 秒，
比 `medium` 的 194 秒还慢，稿子还变短了。**所以默认 `medium`**；只在短推理任务上考虑 `low`。

### 并发与重试

请求层只有一个出口（`agentteam/llm.py`），排队上限取三者最小值：全局 / 端点 / 账户配额组。
同一进程有效，所以**不要用多个 uvicorn worker 来规避配额**。

```bash
LLM_GLOBAL_CONCURRENCY=2        # 全进程同时进行中的请求
LLM_PROVIDER_CONCURRENCY=2      # 同一 base_url
LLM_QUOTA_CONCURRENCY=2         # 同一账户配额组（LLM_QUOTA_GROUP）
LLM_REQUEST_TIMEOUT=90          # 单次尝试
LLM_REQUEST_DEADLINE=180        # 整个 chat（含排队与退避）
LLM_MAX_RETRIES=2               # SDK 内置重试已关闭，重试由本层控制
```

- 只重试**连接建立失败、429、500/502/503/504**；额度/账单/鉴权错误直接停。
- **读取超时结果不确定**（服务端可能还在生成），默认不重发；`Retry-After` 是最小等待，
  超过总时限就停，不会用 `RETRY_CAP` 提前截短。
- 429 会冷却**整个账户组**；排队和退避不占用请求许可。
- 错误信息已脱敏，不会把 key 或上游响应体带出来。
- 按岗位换模型：`ROLE_WRITER_PROVIDER=longcat`、`ROLE_CONTINUITY_MODEL=...` 等，见 `.env.example`。

> 密钥只在本机 `.env` 里，`.env` 已 gitignore。网页点「mock」可以不花钱先看流程。

## 产物

每次运行落盘到 `runs/<时间戳>/`；对比实验落到 `runs/<时间戳>_compare/`：

| 文件 | 是什么 |
| --- | --- |
| `inputs.json` | 本次运行开始时冻结的规则、账本、人物卡 |
| `result.json` | 正文、提案、审查结果、阶段状态 |
| `NN_角色_主角.json` | **最重要**：发给该 agent 的完整 messages + 它的完整回复 |
| `compare.md` | 对比实验报告（数据表 + 各稿全文 + 评审意见） |
| `judge_input.json` | 评审收到的**完整冻结输入**（规则/账本/人物卡/稿件与顺序），可复现 |
| `_summary.md` | 单次运行总览 |

想看 agent team 到底怎么运作，**就读那些 `NN_*.json`**。

## 想学什么，就改什么

**1. 验证信息隔离是真的**：跑 `--modes team team_fullctx`，对比两组角色分析，
看 `team_fullctx` 里角色是不是开始"未卜先知"。

**2. 对比单 agent 和团队**：跑 `--modes team single --judge`，看团队多花的 5 倍调用值不值。

**3. 观察群聊跑偏与守门**：跑 `--modes groupchat groupchat_guarded`，读
`群聊_*.json` 和 `规则守门.json`，看守门人否决了什么、成稿有没有变干净。

**4. 验证"评审必须跨族"**：对同一批稿子跑
`rejudge --provider stepfun` 和 `--provider longcat`，对比两份评审的排序差异。
带 `judge_input.json` 的新目录会**复用冻结输入**，所以差异只可能来自评审模型。

**5. 改岗位卡**：`prompts/*.md` 是岗位说明，改它不用碰代码。试试把"角色编剧"的铁律去掉。

**6. 给不同岗位用不同模型**：在 `.env` 里写 `ROLE_WRITER_PROVIDER=longcat` 之类即可，
不用改代码。

## 目录结构

```
agentteam/
  config.py     配置（provider 预设 + 推理档位 + 并发预算 + 按岗位路由 + .env）
  llm.py        唯一的网络出口：排队、有限重试、退避、取消、脱敏（mock 也走限流）
  recorder.py   每次调用完整落盘 + usage 累计
  rules.py      从设定原文逐字抽取权威机制摘录（R01–R09，带行号）
  ledger.py     账本：切片隔离、状态提案校验与写回
  workspace.py  工作台：SQLite 存草稿/账本/已采纳，采纳闸门
  agents.py     各岗位的调用 + 装配上下文 + 单agent/群聊/守门/审查/提案/修订
  pipeline.py   多模式流水线（含 closed_loop 审查采纳闭环）
  main.py       命令行单次运行
  compare.py    多模式对比实验（落 judge_input.json）
  rejudge.py    换评审模型重评（优先复用冻结输入）
  server.py     本地创作工作台 + 观察台（FastAPI）
  web/index.html 观察台前端（单文件，无构建）
  theater/      角色剧场：每个角色一个只追加的扮演循环（见 角色剧场设计.md）
    world.py      事件日志 + 分支指针 + 三槽（唯一非派生状态）
    context.py    静态系统提示 + 只追加历史 + 每轮结尾状态注入
    actor.py      演员循环、界面操作协议、信念矩阵
    director.py   派轮次、执行读档全局栅栏、写手渲染
    observer.py   结构层泄漏检测 + 语义层分叉与暂停判据
    run.py        命令行跑一场
prompts/        岗位卡（可直接改）
data/
  ledger.json   账本（唯一事实来源，含版本图）
  bible/*.md    人物圣经
  beats_双线夜戏.json  双线场景节拍示例（stage 写成 {角色:文字}）
tests/          请求层 / 工作台 / 剧场验收（不联网）
runs/           产物（自动生成，已 gitignore）
```

## 已知的"没做完"

- **存读档在流水线里没有建模**：账本有 `transition` 字段能画跳跃，但流水线的账本模型
  表达不了"读档 = 世界回溯 + 所有人记忆替换 + 存档库不回退"，所以那类剧情一律 `needs_input`。
  **这件事在角色剧场里已经解决了**——剧场就是为它做的。
- **流水线只跑一段戏**：一场戏采纳后能接着写下一场，但还不能自动连跑多场。
- **剧场没有实测缓存命中率**：前缀稳定性有断言保证（`PrefixCacheTests`），但 stepfun/longcat
  实际命中率还没量。这决定这套架构的经济性，是下一步。
- **剧场的导演不会自己决定收场**：轮次目前是固定给的。

## 观察台看什么

启动 `uv run uvicorn agentteam.server:app --port 8765`，打开 http://127.0.0.1:8765 ，两个页签：

**角色剧场（默认）**
| 区块 | 内容 |
| --- | --- |
| **快照图 · 分支与指针** | 分支线、紫色虚线=读档分叉、绿框=当前指针、彩色方块=各角色的槽位 |
| **各角色的上下文（并排）** | 每个角色一份只追加的上下文，颜色区分"世界事件/他的行动/只他知道/记忆覆盖层/状态注入" |
| **信念 vs 事实** | 谁看得见哪些事件、盲区是什么；两个角色的盲区不同，隔离才是真的 |
| **客观事件日志** | 事实，带在场者 |
| **观察者 / 结构层** | 分叉与暂停判据；泄漏硬证据；界面操作；被覆盖而"从未发生"的意图 |
| **世界动作** | 存/读/删/暂停/回退的时间线 |

**协作模式 / 工作台**：原来的 6 个模式 + 草稿 → 审查 → 采纳闭环。

## 待验证的下一步：角色剧场

按"上下文即记忆"的思路另起了一套拓扑：每个角色一个只追加的上下文，
存档 = 上下文检查点，读档 = 全局回退而读档者豁免。设定原文（R01/R02/R03）本身就
要求"记忆可回退、存档库不回退"，所以这套映射和设定是同构的。

```bash
uv run python -m agentteam.theater.run --mock --turns 4          # 走完存读档，不花钱
uv run python -m agentteam.theater.run --turns 6                 # 真模型
uv run python -m agentteam.theater.run --beats data/beats_双线夜戏.json   # 双线
```

设计与取舍见 [角色剧场设计.md](./角色剧场设计.md)。

