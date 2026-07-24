"""组装企业信息 Agent：语义 Store、业务工具、短期 checkpoint 与长期偏好。"""

# Python 标准库：环境变量、哈希、数学计算、文本切分和控制台输出。
import os
import hashlib
import math
import re
import sys
from dataclasses import dataclass

# 第三方库：配置加载、LangChain Agent/工具/模型，以及 LangGraph 记忆组件。
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.embeddings import Embeddings
from langchain.messages import HumanMessage
from langchain.tools import ToolRuntime, tool
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from state import CustomState, update_state


# 将 .env 中的配置加入当前进程环境变量。
load_dotenv()

# 保证 Windows 控制台能够显示模型回复中的中文、emoji 等 Unicode 字符。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class AppContext:
    """由应用传入、不会交给模型决定的用户上下文。"""

    # user_id 用于隔离不同用户的长期偏好；它不属于某一个 thread 的短期状态。
    user_id: str


# 应用启动时写入 Store 的演示用户目录。
# 外层 Key 同时作为 Store 中的 item key，内层字典是真正保存的 JSON 数据。
SEED_USERS = {
    "user_001": {
        "id": "user_001",
        "name": "张三",
        "department": "技术部",
        "clearance_level": 3,
    },
    "user_002": {
        "id": "user_002",
        "name": "李四",
        "department": "市场部",
        "clearance_level": 1,
    },
}


class LocalHashEmbeddings(Embeddings):
    """无需外部服务的中文字符/词组哈希向量，用作开发环境后备。"""

    def __init__(self, dimensions: int = 1024):
        """记录每条向量的固定维度。"""

        self.dimensions = dimensions

    @staticmethod
    def _tokens(text: str) -> list[str]:
        """把文本拆成英文单词/数字、单个中文字符以及相邻二元词组。"""

        # 统一大小写并去掉首尾空白，减少同义输入在形式上的差异。
        normalized = text.lower().strip()

        # 英文和数字连续保留为词；中文按单个汉字提取。
        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)

        # 把 token 拼接后生成相邻二元组，让“技术部”和“技术部门”产生重叠特征。
        compact = "".join(words)
        bigrams = [compact[index : index + 2] for index in range(len(compact) - 1)]

        # 单词/单字负责基础匹配，二元组负责保留部分局部顺序信息。
        return words + bigrams

    def _embed(self, text: str) -> list[float]:
        """把一段文本转换成归一化的固定维度哈希向量。"""

        # 先创建一个全部为 0 的 1024 维向量。
        vector = [0.0] * self.dimensions

        # 每个 token 通过稳定哈希映射到向量中的某个位置。
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()

            # 前 4 字节决定向量下标，取模确保不会超出维度范围。
            index = int.from_bytes(digest[:4], "big") % self.dimensions

            # 第 5 个字节决定加 1 还是减 1，减少不同 token 哈希碰撞带来的偏差。
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        # 计算向量的 L2 范数，并把向量单位化，便于进行相似度比较。
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """实现 Embeddings 接口：批量转换需要写入 Store 的文档。"""

        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """实现 Embeddings 接口：转换用户的单条搜索语句。"""

        return self._embed(text)


def _seed_store(store: InMemoryStore) -> None:
    """把 SEED_USERS 中的演示资料写入 users namespace。"""

    for user_id, user_info in SEED_USERS.items():
        # namespace=("users",)，key=user_id，value=user_info。
        # Store 配置了 index 时，put 还会同步生成对应的搜索向量。
        store.put(("users",), user_id, user_info)


def create_memory_store() -> tuple[InMemoryStore, str]:
    """创建语义 Store，并返回 Store 对象和实际使用的向量后端名称。"""

    # 没有环境变量时返回 None；存在时返回 Key 字符串。
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")

    # 当前逻辑是：只要存在 DashScope Key，就优先尝试 text-embedding-v4。
    if  dashscope_key:
        try:
            # 创建阿里云 DashScope 的 Embedding 客户端。
            dashscope_embeddings = DashScopeEmbeddings(
                model="text-embedding-v4",
                dashscope_api_key=dashscope_key,
            )
            # $ 表示把每条 Store value 的整个 JSON 文档参与向量化。
            store = InMemoryStore(
                index={
                    "embed": dashscope_embeddings,
                    "dims": 1024,
                    "fields": ["$"],
                }
            )
            # put 数据时会真正请求 DashScope，因此也顺便验证 Key 和网络是否有效。
            _seed_store(store)

            # 第二个返回值供健康接口和前端显示当前实际使用的后端。
            return store, "text-embedding-v4"
        except Exception as exc:
            # 云端向量不可用时记录原因，但不终止整个聊天应用。
            print(
                f"DashScope embeddings unavailable; using local fallback: {exc}",
                file=sys.stderr,
            )

    # 没有 Key 或云端调用失败时，创建无需联网的本地哈希向量后备。
    local_embeddings = LocalHashEmbeddings(dimensions=1024)

    # 两个分支使用完全相同的 Store 接口，后续工具不需要关心具体向量提供方。
    store = InMemoryStore(
        index={
            "embed": local_embeddings,
            "dims": 1024,
            "fields": ["$"],
        }
    )
    # 使用本地算法为演示数据建立索引，不会产生外部 API 费用。
    _seed_store(store)
    return store, "local-hash-1024"


# 模块加载时创建一份共享 Store；Agent 和 FastAPI 都引用同一个实例。
memory_store, embedding_backend = create_memory_store()


@tool
def get_user_info(user_id: str, runtime: ToolRuntime[AppContext, CustomState]) -> str:
    """根据准确的用户 ID 查询用户资料。"""

    # 只有 create_agent(..., store=memory_store) 后 runtime.store 才不为 None。
    if runtime.store is None:
        return "Store not available"

    # get 是 namespace + key 精确查询，不调用语义搜索。
    item = runtime.store.get(("users",), user_id)
    if item is None:
        return f"没有找到用户 {user_id}"

    # get 返回 Item 包装对象，真正保存的字典位于 item.value。
    return f"用户信息：{item.value}"


@tool
def search_users(query: str, runtime: ToolRuntime[AppContext, CustomState]) -> str:
    """根据姓名、部门或自然语言描述对用户目录进行语义搜索。"""

    if runtime.store is None:
        return "Store not available"

    # search 会先把 query 转成向量，再返回 users namespace 中最相似的 3 条数据。
    results = runtime.store.search(("users",), query=query, limit=3)
    if not results:
        return "没有找到匹配的用户"

    # search 返回 Item 列表，因此需要遍历并读取每个 item.value。
    return "\n".join(f"用户信息：{item.value}" for item in results)


@tool
def save_preference(
    preference: str,
    runtime: ToolRuntime[AppContext, CustomState],
) -> str:
    """保存当前登录用户的长期回答偏好。"""

    if runtime.store is None:
        return "Store not available"

    # context.user_id 来自应用请求，不由模型填写，可避免模型写错用户空间。
    # namespace 将不同用户的 preference 数据彼此隔离。
    namespace = ("profiles", runtime.context.user_id, "preferences")

    # 相同 namespace + key 再次 put 时会更新原来的回答风格。
    runtime.store.put(
        namespace,
        "response_style",
        {"text": preference},
    )
    return f"已保存长期偏好：{preference}"


@tool
def get_preference(runtime: ToolRuntime[AppContext, CustomState]) -> str:
    """读取当前登录用户之前保存的长期回答偏好。"""

    if runtime.store is None:
        return "Store not available"

    # 必须使用和 save_preference 完全相同的 namespace 与 key 才能读到数据。
    namespace = ("profiles", runtime.context.user_id, "preferences")
    item = runtime.store.get(namespace, "response_style")
    if item is None:
        return "当前用户还没有保存回答偏好"

    # value 是保存时写入的 {"text": preference} 字典。
    return f"长期偏好：{item.value['text']}"


# 聊天模型和 Embedding 模型职责不同：这里的 DeepSeek 负责推理、回答和工具选择。
model = ChatOpenAI(
    model="deepseek-v4-pro",
    base_url="https://api.deepseek.com",
)

# Checkpointer 保存每个 thread 的短期消息和 CustomState；程序退出后内存数据消失。
checkpointer = InMemorySaver()

# 把模型、全部工具、状态结构、上下文、短期记忆和长期 Store 组装成一个 Agent。
agent = create_agent(
    model=model,
    tools=[
        # 每轮更新 CustomState。
        update_state,
        # 精确用户查询与语义用户搜索。
        get_user_info,
        search_users,
        # 跨 thread 保存和读取当前用户的长期偏好。
        save_preference,
        get_preference,
    ],
    state_schema=CustomState,  # 定义 messages、计数和会话开始时间。
    context_schema=AppContext,  # 定义 runtime.context 的结构。
    checkpointer=checkpointer,  # 保存 thread 级短期状态。
    store=memory_store,  # 提供跨 thread 的长期数据和语义索引。
    system_prompt=(
        "你是企业信息助手。每轮请求必须先调用 update_state 更新会话状态。"
        "遇到明确用户 ID 时使用 get_user_info；按姓名、部门或描述查找时使用 "
        "search_users；用户要求记住回答偏好时使用 save_preference；询问长期偏好时使用 "
        "get_preference。不要编造工具没有返回的数据。"
    ),
)


# 直接运行 store.py 时执行命令行演示；被 app.py 导入时只创建并导出 Agent。
if __name__ == "__main__":
    # 同一个 thread_id 会继续使用该会话之前的 checkpoint。
    demo_config = {"configurable": {"thread_id": "store-demo"}}

    # AppContext 提供可信 user_id，工具可通过 runtime.context.user_id 读取。
    response = agent.invoke(
        {"messages": [HumanMessage(content="帮我查询 user_001 的信息")]},
        demo_config,
        context=AppContext(user_id="demo-user"),
    )

    # 打印完整消息链，包含用户消息、工具调用、工具结果和最终回答。
    for message in response["messages"]:
        message.pretty_print()
