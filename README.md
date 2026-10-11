# 智能体团队实验台

一个最小可跑的 agent team 脚手架 + 网页观察台 + 多模式对比工具。
目的是**看清机制、摸清协作模式**，不是为了产出能用的剧本。

背景设定见 [全员存取档Meta系统设定_v7_整合版.md](./全员存取档Meta系统设定_v7_整合版.md)，
架构设计见 [全员存取档_智能体团队架构图.md](./全员存取档_智能体团队架构图.md)，
实验结果见 [实验记录_协作模式对比.md](./实验记录_协作模式对比.md)。

## 四种协作模式

| 模式 | 说明 | 调用次数 |
| --- | --- | --- |
| `team`（默认） | 总编 → 时间线主管 → 角色编剧（**切片事实包·隔离**）→ 主笔 → 审读 | 6 |
| `team_fullctx` | 同上，但角色编剧看到**全量账本**（故意关掉隔离，做对照） | 6 |
| `single` | 一个智能体拿到全部资料，直接写稿（**单 agent 基线**） | 1 |
| `groupchat` | 五个岗位在**同一讨论串**里依次发言，再由主笔成稿（群聊拓扑） | 6 |
| `groupchat_guarded` | 群聊讨论 → **规则守门（有否决权）** → 主笔 | 7 |

`team` 和 `single` 的对比回答"多智能体到底值不值"；`team` 和 `team_fullctx` 的对比回答
"信息隔离有没有用"；`groupchat` 和 `groupchat_guarded` 的对比回答
"群聊跑偏能不能靠一道否决闸修好"（实验结论：**能**）。

## 跑起来

```bash
uv sync

# 1) 网页观察台（推荐：时间线跳跃 + 实时进度 + 切模式）
uv run uvicorn agentteam.server:app --port 8765
#    浏览器打开 http://127.0.0.1:8765 ，选模式，点「开跑」

# 2) 命令行版
uv run python -m agentteam.main --mock                  # 假模型，不联网不花钱
uv run python -m agentteam.main                         # 默认 team 模式 + stepfun

# 3) 多模式对比实验（会出一份对照报告）
uv run python -m agentteam.compare --modes team single --mock
uv run python -m agentteam.compare --modes team single team_fullctx groupchat groupchat_guarded \
    --judge --judge-provider longcat

# 4) 换评审模型重评已有稿子（隔离"评审"这个变量）
uv run python -m agentteam.rejudge runs/<时间戳>_compare --provider stepfun
```

> **评审务必跨族。** 实验发现：用写作模型自己评审自己的产出，会放过机制硬伤
> （stepfun 给 5/5 的地方，longcat 抓出"读档后不该继续复盘"）。详见实验报告。

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

> 密钥只在本机 `.env` 里，`.env` 已 gitignore。网页点「mock」可以不花钱先看流程。

## 网页观察台看什么

| 区块 | 内容 |
| --- | --- |
| **讨论进行到哪一步** | 各岗位实时状态：等待 / 进行中 / 已完成 |
| **时间线 · 版本与跳跃** | 版本节点图；实线=正常推进，橙色虚线=角色读档，紫色虚线=外层加载；绿框=当前版本 |
| **角色分析** | 每个角色编剧的实时状态与产出（并发，谁在跑一眼可见） |
| **账本 · 当前状态** | 各角色知道/相信/关系/目标 + 三槽 + 伏笔 |
| **场前快照 / 本场目标 / 场景稿 / 审读意见** | 各岗位的产出 |

## 产物

每次运行落盘到 `runs/<时间戳>/`；对比实验落到 `runs/<时间戳>_compare/`：

| 文件 | 是什么 |
| --- | --- |
| `compare.md` | 对比实验报告（数据表 + 各稿全文 + 评审意见） |
| `_summary.md` | 单次运行总览 |
| `scene.md` | 场景稿 |
| `NN_角色_主角.json` | **最重要**：发给该 agent 的完整 messages + 它的完整回复 |

想看 agent team 到底怎么运作，**就读那些 `NN_*.json`**。

## 想学什么，就改什么

**1. 验证信息隔离是真的**：跑 `--modes team team_fullctx`，对比两组角色分析，
看 `team_fullctx` 里角色是不是开始"未卜先知"。

**2. 对比单 agent 和团队**：跑 `--modes team single --judge`，看团队多花的 5 倍调用值不值。

**3. 观察群聊跑偏与守门**：跑 `--modes groupchat groupchat_guarded`，读
`群聊_*.json` 和 `规则守门.json`，看守门人否决了什么、成稿有没有变干净。

**4. 验证"评审必须跨族"**：对同一批稿子跑
`rejudge --provider stepfun` 和 `--provider longcat`，对比两份评审的排序差异。

**5. 改岗位卡**：`prompts/*.md` 是岗位说明，改它不用碰代码。试试把"角色编剧"的铁律去掉。

**6. 给不同岗位用不同模型**：`config.py` 的 `load_config()` 现在返回统一配置，
可以改成按岗位取配置的映射。

## 目录结构

```
agentteam/
  config.py     配置（provider 预设 + reasoning 档位 + .env）
  llm.py        模型调用（唯一的网络出口，含 mock）
  recorder.py   每次调用落盘 + usage 累计
  ledger.py     账本 + 切片（隔离点在这里）
  agents.py     各岗位的调用 + 装配上下文 + 单agent/群聊/评审
  pipeline.py   多模式流水线
  main.py       命令行单次运行
  compare.py    多模式对比实验
  rejudge.py    换评审模型重评已有稿子
  server.py     网页观察台（FastAPI）
  web/index.html 前端（单文件，无构建）
prompts/        岗位卡（可直接改）
data/
  ledger.json   账本（唯一事实来源，含版本图）
  bible/*.md    人物圣经
runs/           产物（自动生成，已 gitignore）
```

## 已知的"没做完"

- **采纳回写**：现在只读账本，不写回。真实流程里，稿子采纳后要更新角色状态、三槽、伏笔。
- **回档事件**：账本里有 `transition` 字段能画跳跃，但读档的完整事件记录还没建模。
- **定向修改**：审读意见出来后，还不能只改某一段再跑。
- **多轮**：一次运行只跑一段戏，还不能连着跑下一段。
