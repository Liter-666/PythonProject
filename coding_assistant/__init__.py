"""只读编程助手的公共入口，不在导入阶段初始化任何外部资源。"""

from coding_assistant.agent import (
    READONLY_CODING_SYSTEM_PROMPT,
    create_coding_agent,
)


__all__ = ["READONLY_CODING_SYSTEM_PROMPT", "create_coding_agent"]
