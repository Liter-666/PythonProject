"""FastAPI 服务：提供聊天 API、状态 API，并托管原生前端静态文件。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from persistence import (
    ThreadOwnershipError,
    delete_thread_record,
    ensure_thread_owner,
    list_user_threads,
    open_postgres_resources,
    touch_thread,
    verify_thread_owner,
)
from store import (
    AppContext,
    create_app_agent,
    get_embedding_backend,
    seed_store,
)


# 配置后端日志，异常时可以在终端中看到完整调用栈。
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 通过当前文件位置计算 static 目录，避免受 PyCharm 工作目录影响。
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    """让 PostgreSQL 资源与 FastAPI 应用使用相同的生命周期。"""

    # 两条数据库连接在整个服务运行期间保持可用，退出时由上下文管理器可靠关闭。
    with open_postgres_resources() as (checkpointer, postgres_store):
        # 演示用户使用相同 namespace + key 幂等写入，不会覆盖其他用户偏好。
        seed_store(postgres_store)
        fastapi_app.state.checkpointer = checkpointer
        fastapi_app.state.store = postgres_store
        fastapi_app.state.agent = create_app_agent(checkpointer, postgres_store)
        yield


# 创建 Web 应用，并把 /static 路径映射到本地静态资源目录。
app = FastAPI(
    title="Agent Memory Console",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    """POST /api/chat 接收的 JSON 请求结构。"""

    # Field 同时负责生成接口文档和校验长度，阻止空消息或异常大的输入。
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str = Field(min_length=1, max_length=128)  # 短期会话标识。
    user_id: str = Field(min_length=1, max_length=128)  # 长期 Store 的用户标识。


class ResetRequest(BaseModel):
    """清空某个会话 checkpoint 时使用的请求结构。"""

    thread_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


def content_to_text(content: Any) -> str:
    """把模型可能返回的字符串或内容块列表统一转换成纯文本。"""

    # 大多数聊天模型直接返回字符串，原样返回即可。
    if isinstance(content, str):
        return content

    # 多模态或新式消息可能由多个内容块组成，需要逐块提取文本。
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            # 内容块本身就是字符串。
            if isinstance(block, str):
                parts.append(block)
            # 标准内容块通常使用 {"type": "text", "text": "..."} 结构。
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        # 使用换行连接多个文本块，保留内容之间的边界。
        return "\n".join(parts)

    # 遇到其他类型时转成字符串，保证响应能够被 JSON 序列化。
    return str(content)


def serialize_new_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """把 LangChain 消息对象转换成前端可消费的 JSON 事件列表。"""

    # events 只包含普通字典，FastAPI 可以自动把它转换成 JSON。
    events: list[dict[str, Any]] = []

    # 保持原始顺序遍历本轮产生的全部消息。
    for message in messages:
        # 用户消息在发送请求前已由浏览器显示；再次返回会造成重复气泡。
        if isinstance(message, HumanMessage):
            continue

        # AIMessage 既可能包含普通文本，也可能包含一个或多个工具调用。
        if isinstance(message, AIMessage):
            # 统一内容格式并清除首尾空白，防止生成空的助手气泡。
            text = content_to_text(message.content).strip()
            if text:
                events.append({"kind": "assistant", "content": text})

            # 一个 AIMessage 可以并行请求多个工具，所以需要逐个转换。
            for call in message.tool_calls:
                events.append(
                    {
                        "kind": "tool_call",
                        # get 的第二个参数是字段缺失时使用的安全默认值。
                        "name": call.get("name", "tool"),
                        "args": call.get("args", {}),
                        # call_id 用于将工具请求和之后的 ToolMessage 对应起来。
                        "call_id": call.get("id", ""),
                    }
                )

        # ToolMessage 表示某次工具调用已经执行完毕并返回结果。
        elif isinstance(message, ToolMessage):
            events.append(
                {
                    "kind": "tool_result",
                    # 某些 ToolMessage 没有 name，因此提供 tool 作为后备显示名。
                    "name": message.name or "tool",
                    "content": content_to_text(message.content),
                    "call_id": message.tool_call_id,
                }
            )

    # 其他未识别消息类型会被忽略；返回所有已转换事件。
    return events


def state_payload(values: dict[str, Any]) -> dict[str, Any]:
    """从完整 Agent State 中提取前端状态面板真正需要的字段。"""

    return {
        # 字段可能尚未创建，因此提供 0 或 None 作为默认值。
        "model_call_count": values.get("model_call_count", 0),
        "session_start": values.get("session_start"),
        # 前端只需要消息数量，不需要再次接收全部历史消息。
        "message_count": len(values.get("messages", [])),
    }


@app.get("/")
def index() -> FileResponse:
    """返回聊天应用首页。"""

    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    """供前端检查服务是否在线，并展示当前模型和向量后端。"""

    return {
        "status": "ok",
        "model": "deepseek-v4-pro",
        "embedding": get_embedding_backend(),
        "persistence": "postgresql",
    }


@app.get("/api/state")
def get_state(
    http_request: Request,
    thread_id: str = Query(min_length=1, max_length=128),
    user_id: str = Query(min_length=1, max_length=128),
) -> dict[str, Any]:
    """根据 thread_id 读取该会话的最新 checkpoint 状态。"""

    try:
        # 只读查询不创建 chat_threads 记录；新会话还不存在时直接返回空状态。
        exists = verify_thread_owner(thread_id, user_id, allow_missing=True)
        if not exists:
            return state_payload({})
    except ThreadOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # checkpointer 使用 configurable.thread_id 定位对应的状态历史。
    config = {"configurable": {"thread_id": thread_id}}
    agent = http_request.app.state.agent
    snapshot = agent.get_state(config)
    return state_payload(snapshot.values if snapshot else {})


@app.post("/api/chat")
def chat(request: ChatRequest, http_request: Request) -> dict[str, Any]:
    """执行一轮 Agent 对话，并返回本轮事件及更新后的状态摘要。"""

    config = {"configurable": {"thread_id": request.thread_id}}
    agent = http_request.app.state.agent

    try:
        # 首次消息登记 thread 归属；已有 thread 必须由同一个 user_id 继续访问。
        ensure_thread_owner(
            request.thread_id,
            request.user_id,
            title=request.message[:80],
        )

        # 调用前先读取历史消息数量，之后只把本轮新增消息发给前端。
        before = agent.get_state(config)
        previous_count = len(before.values.get("messages", [])) if before else 0

        # context 中的 user_id 由应用提供，工具通过 runtime.context 读取它。
        result = agent.invoke(
            {"messages": [HumanMessage(content=request.message)]},
            config,
            context=AppContext(user_id=request.user_id),
        )
        # 使用切片排除 checkpoint 中之前已经展示过的历史消息。
        new_messages = result["messages"][previous_count:]
        touch_thread(request.thread_id, request.user_id)

        # FastAPI 会把下面的普通字典转换为 application/json 响应。
        return {
            "events": serialize_new_messages(new_messages),
            "state": state_payload(result),
            "thread_id": request.thread_id,
            "user_id": request.user_id,
        }
    except ThreadOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        # 服务端记录完整堆栈；同时向浏览器返回可识别的 HTTP 500。
        logger.exception("Agent request failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/reset")
def reset_thread(request: ResetRequest, http_request: Request) -> dict[str, str]:
    """删除指定 thread 的全部 PostgreSQL checkpoint 和应用会话记录。"""

    try:
        verify_thread_owner(request.thread_id, request.user_id)
        http_request.app.state.checkpointer.delete_thread(request.thread_id)
        delete_thread_record(request.thread_id, request.user_id)
        return {"status": "reset", "thread_id": request.thread_id}
    except ThreadOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/users")
def list_users(http_request: Request) -> dict[str, list[dict[str, Any]]]:
    """列出演示 Store 中的用户资料，主要用于调试。"""

    # 不传 query 时按 namespace 获取数据，而不是执行语义相似度查询。
    items = http_request.app.state.store.search(("users",), limit=20)
    return {"users": [item.value for item in items]}


@app.get("/api/threads")
def list_threads(
    user_id: str = Query(min_length=1, max_length=128),
) -> dict[str, list[dict[str, Any]]]:
    """列出指定用户拥有的有效会话。"""

    return {"threads": list_user_threads(user_id)}


# 直接调试 app.py 时启动 Uvicorn；通过 uvicorn app:app 启动时不会重复执行。
if __name__ == "__main__":
    import uvicorn

    # reload=False 避免开发重载同时创建两组数据库连接和 Agent。
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
