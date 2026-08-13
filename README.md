# Agent Memory Console

一个本地 LangChain 聊天应用，演示以下能力：

- `CustomState` 自定义会话状态
- `ToolRuntime` 读取状态、上下文和 Store
- `Command` 更新计数与会话开始时间
- PostgreSQL Checkpointer 按 `thread_id` 持久化短期记忆
- PostgreSQL Store 按 `user_id` 持久化跨会话偏好
- 用户 ID 精确查询与 1024 维语义搜索
- DeepSeek OpenAI 兼容接口

## 项目结构

- `app.py`：FastAPI 接口、Agent 调用入口和消息序列化。
- `state.py`：自定义 Agent State、`Command` 状态更新和最小状态示例。
- `store.py`：模型、工具、长期 Store、Embedding 和 Agent 工厂。
- `persistence.py`：PostgreSQL Checkpointer、Store、数据库初始化和会话登记。
- `coding_assistant/`：M1 只读编程助手领域包；模型、工具和持久化依赖均由调用方注入，导入包时不得连接外部资源。
- `M1_READONLY_CODING_ASSISTANT_PLAN.md`：M1 的架构、安全边界、测试评测方案、实施阶段和进度台账。
- `static/`：用于观察对话、工具调用和状态变化的原生前端。
- `tests/`：现有持久化/API 测试，以及 `tests/coding_assistant/` 中不依赖网络和 PostgreSQL 的编程助手测试。
- `test.py`：早期模型直连实验，不作为正式自动化测试入口。

## 编程助手迁移状态

当前可运行入口仍是下文介绍的企业信息与记忆演示；新的编程助手处于 M1 headless 实验阶段，尚未接入 FastAPI 和前端。旧演示不构成兼容性约束，后续按目标架构逐步重写或移除。

首个开发切片对现有代码的处置如下：

| 范围 | 当前处置 | 判断依据 |
|---|---|---|
| `app.py`、`static/` | 暂缓 | M1 先验证无 UI 的模型注入和离线工具循环，后续再替换运行入口。 |
| `state.py` | 暂缓 | 当前状态字段服务于旧演示，待正式编程任务 State 确定后再决定重写或移除。 |
| `store.py` | 暂缓且禁止新代码依赖 | 文件包含旧业务工具、模块级真实模型和 Embedding 初始化，不适合作为新编程助手基础。 |
| `persistence.py` | 保留候选 | PostgreSQL 生命周期可能复用，但首个 fake 实验使用 `InMemorySaver`，不连接数据库。 |
| 现有测试 | 暂时保留 | 旧入口尚未退出；后续删除旧业务时再按准确文件范围处理。 |

M1 新目录和职责已经在本节确定。实施代码不得为旧业务维持双轨兼容，也不得在模块导入期间创建真实模型、读取密钥或探测外部服务。

## 配置

复制 `.env.example` 为 `.env`，填写 DeepSeek Key 和 PostgreSQL 连接：

```env
OPENAI_API_KEY=your_deepseek_api_key
EMBEDDING_PROVIDER=dashscope
DASHSCOPE_API_KEY=your_valid_dashscope_api_key
POSTGRES_URI=postgresql://root:your_password@127.0.0.1:5432/agent_memory?sslmode=disable
LANGGRAPH_STRICT_MSGPACK=true
```

默认优先调用 DashScope `text-embedding-v4`。启动探测或运行期间调用失败后，当前进程会
自动切换到本地哈希 Embedding；健康接口始终返回实际使用的后端。如需完全禁止外部调用：

```env
EMBEDDING_PROVIDER=local
```

PostgreSQL 持久化使用官方 `langgraph-checkpoint-postgres` 包。首次使用或依赖升级后，
先显式执行数据库初始化：

```powershell
python persistence.py --setup
```

该命令负责：

- 由 Checkpointer 创建或升级 `checkpoint_migrations`、`checkpoints`、
  `checkpoint_blobs` 和 `checkpoint_writes`。
- 由 Store 创建或升级 `store_migrations` 和 `store`。
- 创建应用管理的 `chat_threads` 会话登记表。

业务代码不得直接读写 Checkpointer 和 Store 的框架内部表；读取会话状态时通过
`agent.get_state(config)`，长期数据通过 `runtime.store`，删除 thread 时通过
`checkpointer.delete_thread(thread_id)`。

当前项目尚未安装 `pgvector`。为了保留可观察的最小语义搜索实验，用户目录先从
PostgreSQL Store 读取，再由应用层 Embedding 计算相似度；这适合少量演示数据，不代表
生产级向量检索。进入 M2 RAG 后再安装 `pgvector`，由 `PostgresStore` 创建
`store_vectors` 并在数据库内执行向量召回。

## 启动

```powershell
python -m pip install -r requirements.txt
python persistence.py --setup
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

不调用在线模型的回归测试：

```powershell
python -m pytest -q
```

PostgreSQL Checkpointer 会在进程重启后保留 thread 的消息和 `CustomState`；
PostgreSQL Store 会保留跨 thread 的用户资料与长期偏好。两者使用同一个数据库实例，
但仍是职责、接口和表结构彼此独立的两套持久化机制。

## 从 MySQL 方案迁移

原 MySQL 方案使用第三方 `langgraph-checkpoint-mysql`，只持久化 Checkpointer；
长期 Store 仍是 `InMemoryStore`。当前 PostgreSQL 方案使用官方包，同时提供
`PostgresSaver` 和 `PostgresStore`。本地 MySQL 服务仍可保留用于对照实验，但应用运行
时只读取 `POSTGRES_URI`，不会同时写入两种数据库。
