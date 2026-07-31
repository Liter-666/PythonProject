"""验证不依赖外部模型的本地检索基线。"""

from langchain_core.embeddings import Embeddings

from store import (
    FallbackEmbeddings,
    LocalHashEmbeddings,
    SEED_USERS,
    _cosine_similarity,
    _store_value_text,
)


class FailingEmbeddings(Embeddings):
    """模拟无法连接的远程 Embedding，确保测试不会调用真实网络。"""

    def __init__(self):
        self.query_calls = 0
        self.document_calls = 0

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        raise ConnectionError("模拟连接失败")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        raise ConnectionError("模拟连接失败")


def test_local_hash_search_ranks_technical_user_first() -> None:
    """“技术部开发人员”应比市场部资料更接近技术部资料。"""

    embeddings = LocalHashEmbeddings(dimensions=1024)
    query_vector = embeddings.embed_query("技术部开发人员")
    user_vectors = {
        user_id: embeddings.embed_documents([_store_value_text(user_info)])[0]
        for user_id, user_info in SEED_USERS.items()
    }

    technical_score = _cosine_similarity(query_vector, user_vectors["user_001"])
    marketing_score = _cosine_similarity(query_vector, user_vectors["user_002"])

    assert technical_score > marketing_score


def test_embedding_failure_switches_to_local_for_process_lifetime() -> None:
    """远程调用失败一次后，后续请求直接使用本地后备。"""

    primary = FailingEmbeddings()
    fallback = LocalHashEmbeddings(dimensions=1024)
    embeddings = FallbackEmbeddings(
        primary,
        fallback,
        primary_name="text-embedding-v4",
        fallback_name="local-hash-1024-fallback",
    )

    query_vector = embeddings.embed_query("技术部")
    document_vectors = embeddings.embed_documents(["技术部"])

    assert len(query_vector) == 1024
    assert len(document_vectors[0]) == 1024
    assert embeddings.backend_name == "local-hash-1024-fallback"
    assert primary.query_calls == 1
    assert primary.document_calls == 0
