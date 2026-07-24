"""演示 LangChain Agent 自定义状态、工具更新和短期记忆。"""

import sys

# LangChain/LangGraph：工具、状态、消息、模型与检查点相关类型。
from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from langchain.messages import ToolMessage
from datetime import datetime
from langchain.agents import AgentState
from typing import NotRequired
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langchain.messages import HumanMessage
from dotenv import load_dotenv

# 从项目目录的 .env 文件加载 API Key 等环境变量。
load_dotenv()

# Windows 控制台可能默认使用 GBK；改成 UTF-8 后可以打印 emoji 和各种语言字符。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# 在 AgentState 自带的 messages 字段上增加当前项目需要的状态字段。
class CustomState(AgentState):
    """Agent 的会话状态结构。"""

    # NotRequired 表示首次创建状态时可以暂时没有这些字段。
    model_call_count: NotRequired[int]  # 当前示例中实际统计 update_state 的调用次数。
    session_start: NotRequired[str]  # 当前 thread 第一次更新时间，保存为 ISO 格式字符串。


@tool
def update_state(runtime: ToolRuntime[None, CustomState]) -> Command:
    """读取当前运行状态，并通过 Command 提交本轮状态更新。"""

    # runtime 由 LangChain 自动注入；模型看不到也不需要填写这个参数。
    # ToolMessage 必须带本次 tool_call_id，以便和 AIMessage 中的工具调用正确配对。
    command = {
        "model_call_count": runtime.state.get("model_call_count", 0) + 1,
        "messages": [ToolMessage("Successfully updated agent state", tool_call_id=runtime.tool_call_id)]
    }

    # 只有字段不存在时才记录开始时间，避免后续请求覆盖原始时间。
    if "session_start" not in runtime.state:
        command["session_start"] = datetime.now().isoformat()

    # Command 只是更新指令；函数返回后由 LangGraph 合并状态并写入 checkpointer。
    return Command(update=command)


def create_demo_agent():
    """创建一个只演示 update_state 的 Agent。"""

    # DeepSeek 提供 OpenAI 兼容接口，因此可以通过 ChatOpenAI 连接。
    model = ChatOpenAI(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
    )
    # create_agent 会在内部构建包含模型节点和工具节点的 LangGraph。
    return create_agent(
        model,
        tools=[update_state],
        state_schema=CustomState,
        checkpointer=InMemorySaver(),
        system_prompt="你是一个热心的助手，你需要在每次请求时调用update_state工具以更新任务状态。",
    )

# 只有直接运行 state.py 时才执行演示；被 store.py 导入时不会发送模型请求。
if __name__ == "__main__":
    agent = create_demo_agent()

    # thread_id 是 InMemorySaver 保存和读取该段对话状态的主键。
    config = {"configurable": {"thread_id": "1"}}

    # 第一个参数是 Agent 输入，第二个参数是本次运行配置。
    response = agent.invoke(
        {"messages": [HumanMessage(content="Hi, my name is 虎哥")]},
        config,
    )

    # 按顺序打印 HumanMessage、AIMessage 和 ToolMessage。
    for message in response["messages"]:
        message.pretty_print()
