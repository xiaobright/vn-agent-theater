# VN Agent Theater · 角色剧场

一个在本机运行的多智能体叙事实验台，用同一套网页观察台探索两种协作方式：

- **编剧流水线**：岗位分工构思、写稿、检查，由人决定是否采纳。
- **角色剧场**：角色拥有独立记忆，声明行动和下次行动时间；世界裁决，导演调度，写手整理正文。

*A local multi-agent narrative lab: writing workflows, isolated character memories, event-driven scheduling and save/load experiments. Python + FastAPI + SQLite + vanilla HTML/JS. No agent framework required.*

这是个人学习项目，用来观察协作、信息分叉和回溯。目前没有游戏引擎导出、长期自治运行或成熟的剧本质量保证。

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。以下命令适用于 PowerShell 及常见 Linux/macOS shell。

~~~sh
git clone https://github.com/xiaobright/vn-agent-theater.git
cd vn-agent-theater
uv sync --locked
uv run uvicorn agentteam.server:app --host 127.0.0.1 --port 8765
~~~

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。网页默认 **mock**，无需密钥，不调用模型。安装依赖需要网络；mock 验证运行流程，不评价创作质量。

~~~sh
# 固定轮次：存读档和记忆分叉
uv run python -m agentteam.theater.run --mock --turns 4
# 事件驱动：位置、物品、动作与自主唤醒
uv run python -m agentteam.theater.run --mock --scene data/scene_大乱斗.json --max-turns 6
~~~

## 模型与预算

将 [.env.example](.env.example) 复制为 .env，填写自己的 OpenAI 兼容接口。仓库不包含可用密钥或模型额度。

~~~ini
LLM_PROVIDER=custom
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_MODEL=your-model
LLM_API_KEY=your-key
LLM_GLOBAL_CONCURRENCY=2
LLM_PROVIDER_CONCURRENCY=2
LLM_QUOTA_CONCURRENCY=2
LLM_QUOTA_GROUP=my-plan
~~~

提供 StepFun、LongCat 的可修改预设；模型与推理档位是否支持，以提供方当前文档为准。取消 mock 后才产生真实模型请求。

- 请求统一受全局、端点、账户配额组上限约束。不同 URL 共用 coding plan 时使用相同组名。
- ROLE_WRITER_PROVIDER、ROLE_CONTINUITY_PROVIDER、ROLE_PROTAGONIST_MODEL 等可按岗位分配模型，详见配置示例。
- 连接失败、限流及部分服务端错误有有限重试、退避、Retry-After 和总时限；读取超时不自动重发。
- 取消停止本地排队和等待，但不保证提供方取消生成或计费。
- 限流只对**一个进程**生效；多个 worker 或同时启动的 CLI 不共享预算。

## 角色剧场

~~~text
私有记忆 + 当前可见状态
          ↓
角色给出行动意图和下次唤醒条件
          ↓
世界裁决 → 追加事件 → 更新可见记忆
          ↓
存 / 读 / 删档 / 改备注（如果角色选择）
          ↓
调度下一人 → 观察者检查 → 写手渲染
~~~

存档冻结**具体事件边界**、时钟、每个人的记忆和待执行计划。读档恢复这些内容，唯独主动读档者保留当前记忆；三槽及备注不回溯。轮次只是展示标签。

全量作者设定用于检查；演员系统提示只放自身操作协议和表达风格，初始信念进入可回退记忆。系统提示不会提前揭示场外玩家。

网页显示分支图、位置、物品、唤醒记录、上下文和观察报告。实现与限制见 [角色剧场设计](角色剧场设计.md)。

## 编剧工作台

保留六个模式：workshop、team、team_fullctx、single、groupchat、groupchat_guarded。

支持生成正文与状态提案、结构化检查、有限次修改、人工采纳及下一场。手工编辑、账本或规则更新后必须重查；mock 稿不能采纳到正式账本。

- data/ledger.json 是初始种子；采纳后的事实在 data/workshop.sqlite3。
- 流水线只采纳连续场景，不采纳世界内存读档变更；这类模拟由剧场承担。
- 剧场不自动写入工作台账本，两条路径保持独立便于对比。

~~~sh
uv run python -m agentteam.main --mock
uv run python -m agentteam.compare --modes team single --mock
~~~

agentteam.rejudge 可复用新实验目录冻结的评审输入。agentteam.theater.compare_injection 比较状态注入方式，**会调用真实模型**，先查看 --help 和预算配置。

## 记录与验证

输入、模型回复和产物写在 runs/，可能包含私人剧情，默认不进入 Git。这些记录用于审查，不是完整进程恢复格式。

~~~sh
uv run python -m unittest discover -s tests -v
# 页面状态回归检查（需要 Node.js 22+）
node --test tests/web.test.cjs
~~~

测试用临时目录及模型替身，无需密钥。CI 在 Windows 和 Linux 上运行，覆盖请求预算、取消、快照、记忆、物品、时间线和工作台持久化。

限制：导演按轮数和时间上限结束，不自主判断文学上的收场；字符串匹配只能发现部分泄漏；模型仍可能编造或违背格式；文学效果和缓存收益需单独评估，历史小样本不能证明某种架构普遍更好。

## 文档与许可

- [当前设计](角色剧场设计.md) / [变更记录](CHANGELOG.md)
- [贡献与验证](CONTRIBUTING.md) / [本地运行与敏感数据](SECURITY.md)
- [历史设计记录](docs/history/角色剧场设计_原型记录.md)
- [协作模式实验](实验记录_协作模式对比.md) / [状态注入实验](实验记录_状态注入对比.md)

**代码及技术文档采用 [MIT](LICENSE)。原创设定、人物、剧情和随附素材保留版权，适用 [素材许可](CONTENT_LICENSE.md)，不属于 MIT。** 公开阅读不代表开放改编或商业使用。
