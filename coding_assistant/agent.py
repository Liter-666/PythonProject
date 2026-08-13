"""创建依赖可注入的只读编程 Agent，用于 M1 离线机制实验。"""

from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver


# 当前 Prompt 只声明阶段 1 已具备的能力，避免模型把后续计划误报为现有功能。
READONLY_CODING_SYSTEM_PROMPT = (
    "你是处于 M1 可行性实验阶段的只读后端编程助手。"
    "你只能使用调用方提供的工具，不得声称能够访问未由工具返回的仓库内容，"
    "也不得声称能够修改文件、执行命令或连接外部系统。"
)


def create_coding_agent(
    model: BaseChatModel,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]],
    *,
    response_format: ResponseFormat[Any] | type[Any] | dict[str, Any] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """使用调用方提供的依赖创建 Agent，不隐式初始化模型、数据库或网络客户端。

    返回类型由 LangChain 的 ``create_agent`` 根据 State、Context 和结构化输出泛型
    组合生成。此处不复制其内部复杂类型，避免项目代码与框架私有泛型细节耦合。
    """

    # 转为 tuple 固定本次 Agent 可见的工具集合，避免调用方随后修改可变列表。
    configured_tools = tuple(tools)
    return create_agent(
        model=model,
        tools=configured_tools,
        system_prompt=READONLY_CODING_SYSTEM_PROMPT,
        response_format=response_format,
        checkpointer=checkpointer,
        name="readonly-coding-assistant",
    )
