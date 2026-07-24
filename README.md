# Agent Memory Console

一个本地 LangChain 聊天应用，演示以下能力：

- `CustomState` 自定义会话状态
- `ToolRuntime` 读取状态、上下文和 Store
- `Command` 更新计数与会话开始时间
- MySQL Checkpointer 按 `thread_id` 持久化短期记忆
- `InMemoryStore` 按 `user_id` 保存跨会话偏好
- 用户 ID 精确查询与 1024 维语义搜索
- DeepSeek OpenAI 兼容接口

## 项目结构

- `app.py`：FastAPI 接口、Agent 调用入口和消息序列化。
- `state.py`：自定义 Agent State、`Command` 状态更新和最小状态示例。
- `store.py`：模型、工具、长期 Store、Embedding 和 Agent 工厂。
- `persistence.py`：MySQL Checkpointer 连接、数据库初始化和会话登记。
- `static/`：用于观察对话、工具调用和状态变化的原生前端。
- `tests/`：工具、持久化、thread 隔离和 API 失败路径测试。
- `test.py`：早期模型直连实验，不作为正式自动化测试入口。

## 配置

复制 `.env.example` 为 `.env`，填写 DeepSeek Key 和 MySQL 连接：

```env
OPENAI_API_KEY=your_deepseek_api_key
EMBEDDING_PROVIDER=local
MYSQL_CHECKPOINT_URI=mysql://agent_user:agent_password@127.0.0.1:3306/agent_memory
```

默认语义索引完全在本地运行。如果希望使用 DashScope：

```env
EMBEDDING_PROVIDER=dashscope
DASHSCOPE_API_KEY=your_valid_dashscope_api_key
```

MySQL Checkpointer 使用第三方 `langgraph-checkpoint-mysql` 包，要求 MySQL `>= 8.0.19`
或 MariaDB `>= 10.7.1`。首次使用或依赖升级后，先显式执行数据库初始化：

```powershell
python persistence.py --setup
```

该命令负责：

- 由 Checkpointer 创建或升级 `checkpoint_migrations`、`checkpoints`、
  `checkpoint_blobs` 和 `checkpoint_writes`。
- 创建应用管理的 `chat_threads` 会话登记表。

业务代码不得直接读写 Checkpointer 的四张内部表；读取会话状态时通过
`agent.get_state(config)`，删除 thread 时通过 `checkpointer.delete_thread(thread_id)`。

## 启动

```powershell
python -m pip install -r requirements.txt
python persistence.py --setup
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

MySQL Checkpointer 会在进程重启后保留 thread 的消息和 `CustomState`。
`InMemoryStore` 仍只存在于当前 Python 进程中，长期偏好会在服务重启后清空；本次持久化
实验不把 Store 误判为已经持久化。
