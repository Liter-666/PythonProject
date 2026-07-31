"""组装企业信息 Agent：语义 Store、业务工具、短期 checkpoint 与长期偏好。"""

# Python 标准库：环境变量、哈希、数学计算、文本切分和控制台输出。
import os
import hashlib
import json
import math
import re
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

# 第三方库：配置加载、LangChain Agent/工具/模型，以及 LangGraph 记忆组件。
from dashscope import TextEmbedding
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.embeddings import Embeddings
from langchain.messages import HumanMessage
from langchain.tools import ToolRuntime, tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

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


class DashScopeTextEmbeddings(Embeddings):
    """把阿里云官方 TextEmbedding SDK 适配为 LangChain Embeddings 接口。"""

    def __init__(self, api_key: str, dimensions: int = 1024):
        """保存调用凭据和固定输出维度，不在代码或日志中输出密钥。"""

        self.api_key = api_key
        self.dimensions = dimensions

    def _embed(self, texts: list[str], *, text_type: str) -> list[list[float]]:
        """批量调用 text-embedding-v4，并按输入下标恢复结果顺序。"""

        response = TextEmbedding.call(
            model="text-embedding-v4",
            input=texts,
            api_key=self.api_key,
            text_type=text_type,
            dimension=self.dimensions,
            output_type="dense",
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope Embedding 调用失败：{response.code} {response.message}"
            )

        # 服务端返回 text_index，排序后才能确保向量和原始文本一一对应。
        embeddings = sorted(
            response.output["embeddings"],
            key=lambda item: item["text_index"],
        )
        vectors = [item["embedding"] for item in embeddings]
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise RuntimeError("DashScope Embedding 返回的向量数量或维度不符合预期")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """把待检索文档转换为向量。"""

        return self._embed(texts, text_type="document")

    def embed_query(self, text: str) -> list[float]:
        """把搜索请求转换为查询向量。"""

        return self._embed([text], text_type="query")[0]


class FallbackEmbeddings(Embeddings):
    """优先使用远程 Embedding，调用失败后在当前进程内永久切换到本地后备。"""

    def __init__(
        self,
        primary: Embeddings,
        fallback: Embeddings,
        *,
        primary_name: str,
        fallback_name: str,
    ):
        """保存主后端和后备后端，并初始化线程安全的切换状态。"""

        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        self._fallback_active = False
        self._switch_lock = threading.Lock()

    @property
    def backend_name(self) -> str:
        """返回当前进程实际使用的 Embedding 后端名称。"""

        return self.fallback_name if self._fallback_active else self.primary_name

    def _activate_fallback(self, exc: Exception) -> None:
        """首次失败时原子切换到本地后备，并记录不含密钥的失败原因。"""

        with self._switch_lock:
            if not self._fallback_active:
                print(
                    f"DashScope Embedding 不可用，切换到本地哈希：{exc}",
                    file=sys.stderr,
                )
                self._fallback_active = True

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """优先调用远程文档向量；失败后使用本地向量并保持后备状态。"""

        if self._fallback_active:
            return self.fallback.embed_documents(texts)
        try:
            return self.primary.embed_documents(texts)
        except Exception as exc:
            self._activate_fallback(exc)
            return self.fallback.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """优先调用远程查询向量；失败后使用本地向量并保持后备状态。"""

        if self._fallback_active:
            return self.fallback.embed_query(text)
        try:
            return self.primary.embed_query(text)
        except Exception as exc:
            self._activate_fallback(exc)
            return self.fallback.embed_query(text)


def seed_store(store: BaseStore) -> None:
    """把 SEED_USERS 中的演示资料写入 users namespace。"""

    for user_id, user_info in SEED_USERS.items():
        # namespace=("users",)，key=user_id，value=user_info。
        # Store 配置了 index 时，put 还会同步生成对应的搜索向量。
        store.put(("users",), user_id, user_info)


def create_embedding_model() -> tuple[Embeddings, str]:
    """创建 DashScope 优先、本地哈希兜底的 Embedding。"""

    provider = os.getenv("EMBEDDING_PROVIDER", "dashscope").strip().lower()
    local_embeddings = LocalHashEmbeddings(dimensions=1024)

    if provider == "dashscope":
        dashscope_key = os.getenv("DASHSCOPE_API_KEY")
        if not dashscope_key:
            print(
                "未配置 DASHSCOPE_API_KEY，使用本地哈希 Embedding",
                file=sys.stderr,
            )
            return local_embeddings, "local-hash-1024-fallback"

        embeddings = FallbackEmbeddings(
            DashScopeTextEmbeddings(api_key=dashscope_key, dimensions=1024),
            local_embeddings,
            primary_name="text-embedding-v4",
            fallback_name="local-hash-1024-fallback",
        )
        # 启动时执行一次最小查询，确保健康接口显示的是实际可用后端。
        embeddings.embed_query("连接测试")
        return embeddings, embeddings.backend_name

    if provider == "local":
        return local_embeddings, "local-hash-1024"

    raise RuntimeError(
        f"不支持的 EMBEDDING_PROVIDER={provider!r}，只能使用 local 或 dashscope"
    )


# Embedding 客户端本身不保存长期数据；真正的用户资料和偏好由 PostgreSQL Store 保存。
embedding_model, embedding_backend = create_embedding_model()


def get_embedding_backend() -> str:
    """返回当前实际后端，包含运行期间发生的自动降级。"""

    if isinstance(embedding_model, FallbackEmbeddings):
        return embedding_model.backend_name
    return embedding_backend


def _store_value_text(value: dict[str, Any]) -> str:
    """把 Store 中的 JSON 值稳定转换为参与相似度计算的文本。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度，并处理零向量。"""

    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


@tool
def get_user_info(user_id: str, runtime: ToolRuntime[AppContext, CustomState]) -> str:
    """根据准确的用户 ID 查询用户资料。"""

    # 只有 create_agent(..., store=postgres_store) 后 runtime.store 才不为 None。
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

    # 当前 PostgreSQL 尚未安装 pgvector，因此先读取少量演示数据，再显式计算相似度。
    # 该实现用于暴露检索机制和建立测试基线，不适合数据量较大的生产检索。
    items = runtime.store.search(("users",), limit=100)
    if not items:
        return "没有找到匹配的用户"

    query_vector = embedding_model.embed_query(query)
    document_vectors = embedding_model.embed_documents(
        [_store_value_text(item.value) for item in items]
    )
    ranked_items = sorted(
        zip(items, document_vectors, strict=True),
        key=lambda pair: _cosine_similarity(query_vector, pair[1]),
        reverse=True,
    )

    return "\n".join(
        f"用户信息：{item.value}" for item, _ in ranked_items[:3]
    )


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

def create_app_agent(
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
):
    """使用由应用管理生命周期的 Checkpointer 和 Store 创建 Agent。"""

    # Agent 只依赖抽象接口，不需要知道底层连接的是 PostgreSQL 还是测试替身。
    return create_agent(
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
        store=store,  # 提供跨 thread 的长期数据。
        system_prompt=(
            "你是企业信息助手。每轮请求必须先调用 update_state 更新会话状态。"
            "遇到明确用户 ID 时使用 get_user_info；按姓名、部门或描述查找时使用 "
            "search_users；用户要求记住回答偏好时使用 save_preference；询问长期偏好时使用 "
            "get_preference。不要编造工具没有返回的数据。"
        ),
    )


# 直接运行 store.py 时执行命令行演示；被 app.py 导入时只导出 Agent 工厂和工具。
if __name__ == "__main__":
    from persistence import open_postgres_resources

    # 同一个 thread_id 会继续使用该会话之前的 checkpoint。
    demo_config = {"configurable": {"thread_id": "store-demo"}}

    # 资源在整个 Agent 调用期间保持打开，退出 with 后由驱动可靠关闭连接。
    with open_postgres_resources() as (checkpointer, postgres_store):
        seed_store(postgres_store)
        agent = create_app_agent(checkpointer, postgres_store)

        # AppContext 提供可信 user_id，工具可通过 runtime.context.user_id 读取。
        response = agent.invoke(
            {"messages": [HumanMessage(content="帮我查询 user_001 的信息")]},
            demo_config,
            context=AppContext(user_id="demo-user"),
        )

        # 打印完整消息链，包含用户消息、工具调用、工具结果和最终回答。
        for message in response["messages"]:
            message.pretty_print()
