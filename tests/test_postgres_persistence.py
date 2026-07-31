"""验证 PostgreSQL Checkpointer、Store 与会话归属的持久化边界。"""

from __future__ import annotations

import os
from typing import Annotated, NotRequired, TypedDict
from uuid import uuid4

import pytest
from langchain.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from persistence import (
    ThreadOwnershipError,
    delete_thread_record,
    ensure_thread_owner,
    list_user_threads,
    open_postgres_checkpointer,
    open_postgres_store,
    setup_database,
    touch_thread,
    verify_thread_owner,
)


pytestmark = pytest.mark.integration


class CheckpointTestState(TypedDict):
    """最小图状态：消息使用标准 reducer，计数用于验证普通字段恢复。"""

    messages: Annotated[list[AnyMessage], add_messages]
    counter: NotRequired[int]


def _postgres_uri() -> str:
    """获取测试连接；未配置时明确跳过，而不是偷偷退回内存。"""

    uri = os.getenv("POSTGRES_URI")
    if not uri:
        pytest.skip("未配置 POSTGRES_URI，跳过 PostgreSQL 集成测试")
    return uri


def _build_checkpoint_graph(checkpointer):
    """构建不调用在线模型的最小图，隔离验证 Checkpointer 本身。"""

    def update_state(state: CheckpointTestState) -> dict:
        return {
            "messages": [AIMessage(content="已写入 PostgreSQL")],
            "counter": state.get("counter", 0) + 1,
        }

    builder = StateGraph(CheckpointTestState)
    builder.add_node("update_state", update_state)
    builder.add_edge(START, "update_state")
    builder.add_edge("update_state", END)
    return builder.compile(checkpointer=checkpointer)


def test_checkpointer_restores_state_after_reopening_connection() -> None:
    """关闭并重新打开连接后，thread 状态和消息类型仍能恢复。"""

    uri = _postgres_uri()
    setup_database(uri)
    thread_id = f"test-checkpoint-{uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        with open_postgres_checkpointer(uri) as checkpointer:
            graph = _build_checkpoint_graph(checkpointer)
            result = graph.invoke(
                {"messages": [HumanMessage(content="请保存这条消息")]},
                config,
            )
            assert result["counter"] == 1

        with open_postgres_checkpointer(uri) as checkpointer:
            graph = _build_checkpoint_graph(checkpointer)
            snapshot = graph.get_state(config)
            assert snapshot.values["counter"] == 1
            assert isinstance(snapshot.values["messages"][0], HumanMessage)
            assert isinstance(snapshot.values["messages"][1], AIMessage)
            assert snapshot.values["messages"][1].content == "已写入 PostgreSQL"
    finally:
        with open_postgres_checkpointer(uri) as checkpointer:
            checkpointer.delete_thread(thread_id)


def test_store_restores_value_after_reopening_connection() -> None:
    """Store 的 namespace、key 和 JSON value 在新连接中保持不变。"""

    uri = _postgres_uri()
    setup_database(uri)
    namespace = ("tests", "profiles", str(uuid4()))
    key = "response_style"
    expected_value = {"text": "回答尽量简洁"}

    try:
        with open_postgres_store(uri) as store:
            store.put(namespace, key, expected_value)

        with open_postgres_store(uri) as store:
            item = store.get(namespace, key)
            assert item is not None
            assert item.value == expected_value
    finally:
        with open_postgres_store(uri) as store:
            store.delete(namespace, key)


def test_thread_registry_rejects_cross_user_access() -> None:
    """相同 thread_id 只能属于一个 user_id，并能在用户会话列表中查询。"""

    uri = _postgres_uri()
    setup_database(uri)
    thread_id = f"test-owner-{uuid4()}"
    owner_id = f"owner-{uuid4()}"
    other_user_id = f"other-{uuid4()}"

    try:
        ensure_thread_owner(thread_id, owner_id, title="归属隔离测试", uri=uri)
        assert verify_thread_owner(thread_id, owner_id, uri=uri)

        with pytest.raises(ThreadOwnershipError):
            ensure_thread_owner(thread_id, other_user_id, uri=uri)

        touch_thread(thread_id, owner_id, uri=uri)
        thread_ids = {
            thread["thread_id"] for thread in list_user_threads(owner_id, uri=uri)
        }
        assert thread_id in thread_ids
    finally:
        delete_thread_record(thread_id, owner_id, uri=uri)
