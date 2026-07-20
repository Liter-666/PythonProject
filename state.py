import sys

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

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# 定义自定义State结构
class CustomState(AgentState):
    """Agent的任务状态"""
    model_call_count: NotRequired[int]  # 模型调用次数
    session_start: NotRequired[str]  # 会话开始时间


@tool
def update_state(runtime: ToolRuntime[None, CustomState]) -> Command:
    """A tool that update agent state"""
    # 组织结果
    command = {
        "model_call_count": runtime.state.get("model_call_count", 0) + 1,
        "messages": [ToolMessage("Successfully updated agent state", tool_call_id=runtime.tool_call_id)]
    }
    # 判断是否是第一次
    if "session_start" not in runtime.state:
        command["session_start"] = datetime.now().isoformat()

    return Command(update=command)


model = ChatOpenAI(
    model="deepseek-v4-pro",
    base_url="https://api.deepseek.com",
)

agent = create_agent(
    model,
    tools=[update_state],
    state_schema=CustomState,
    checkpointer=InMemorySaver(),
    system_prompt="你是一个热心的助手，你需要在每次请求时调用update_state工具以更新任务状态。"
)

config = {"configurable": {"thread_id": "1"}}
response = agent.invoke(
    {"messages": [HumanMessage(content="Hi, my name is 虎哥")]},
    config
)

for message in response['messages']:
    message.pretty_print()
