"""使用官方 fake model 验证只读编程 Agent 的离线调用边界。"""

from __future__ import annotations

from collections.abc import Iterator

from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from coding_assistant import create_coding_agent


@tool
def probe_repository(path: str) -> str:
    """返回固定的仓库探测结果，仅用于验证离线 Tool Calling 消息循环。"""

    # 测试工具不访问文件系统，确保阶段 1 只验证 Agent 机制而不提前实现阶段 4。
    return f"found:{path}"


class ProbeResult(BaseModel):
    """阶段 1 用于验证 ToolStrategy 可行性的最小结构化结果。"""

    summary: str


def scripted_model(messages: list[AIMessage]) -> GenericFakeChatModel:
    """用固定迭代器创建可重复、无网络调用的聊天模型。"""

    scripted_messages: Iterator[AIMessage | str] = iter(messages)
    return GenericFakeChatModel(messages=scripted_messages)


def test_fake_agent_runs_tool_call_loop_offline() -> None:
    """fake model 应产生 Human → AI Tool Call → Tool → AI 的完整消息链。"""

    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "probe_repository",
                        "args": {"path": "app.py"},
                        "id": "probe-call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已完成离线探测。"),
        ]
    )
    agent = create_coding_agent(model, [probe_repository])

    result = agent.invoke(
        {"messages": [HumanMessage(content="查找 app.py")]},
    )

    messages = result["messages"]
    assert isinstance(messages[0], HumanMessage)
    assert isinstance(messages[1], AIMessage)
    assert messages[1].tool_calls[0]["name"] == "probe_repository"
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].content == "found:app.py"
    assert isinstance(messages[3], AIMessage)
    assert messages[3].content == "已完成离线探测。"


def test_fake_agent_returns_tool_strategy_structured_response() -> None:
    """ToolStrategy 应把 fake model 的结构化 Tool Call 转成 Pydantic 对象。"""

    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ProbeResult",
                        "args": {"summary": "结构化输出可离线验证"},
                        "id": "structured-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    agent = create_coding_agent(
        model,
        [],
        response_format=ToolStrategy(ProbeResult, handle_errors=False),
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="返回探测摘要")]},
    )

    assert result["structured_response"] == ProbeResult(
        summary="结构化输出可离线验证"
    )


def test_in_memory_saver_does_not_require_external_configuration(
    monkeypatch,
) -> None:
    """内存 Checkpointer 应在没有模型和 PostgreSQL 配置时保存 thread 状态。"""

    # 主动移除外部配置，证明该实验的依赖边界，而不是依靠开发机现有环境碰巧成功。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    model = scripted_model([AIMessage(content="状态已写入内存。")])
    checkpointer = InMemorySaver()
    agent = create_coding_agent(model, [], checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "m1-fake-thread"}}

    agent.invoke(
        {"messages": [HumanMessage(content="保存这次离线实验")]},
        config,
    )
    snapshot = agent.get_state(config)

    assert snapshot.values["messages"][0].content == "保存这次离线实验"
    assert snapshot.values["messages"][1].content == "状态已写入内存。"
