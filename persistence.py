"""基于 PostgreSQL 的检查点、长期 Store 与应用会话登记。"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from psycopg import Connection, connect
from psycopg.rows import dict_row


# 在读取 POSTGRES_URI 前加载本地开发环境配置。
load_dotenv()

# 官方包建议限制 Checkpointer 允许反序列化的模块；显式环境变量仍可覆盖该安全默认值。
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

POSTGRES_URI_ENV = "POSTGRES_URI"

# Checkpointer 和 Store 的内部表由 LangGraph 管理；本表只保存应用层会话归属和展示信息，
# 避免业务代码直接查询或解析框架内部数据。
CHAT_THREADS_DDL = """
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    title VARCHAR(255) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id),
    CONSTRAINT ck_chat_threads_status
        CHECK (status IN ('active', 'archived'))
)
"""

CHAT_THREADS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_chat_threads_user_updated
ON chat_threads (user_id, updated_at DESC)
"""


class PersistenceConfigurationError(RuntimeError):
    """PostgreSQL 持久化配置缺失或格式错误。"""


class ThreadOwnershipError(PermissionError):
    """用户尝试访问不属于自己的会话。"""


def get_postgres_uri(uri: str | None = None) -> str:
    """优先返回显式传入的 URI，否则读取 PostgreSQL 连接配置。"""

    resolved_uri = uri or os.getenv(POSTGRES_URI_ENV)
    if not resolved_uri:
        raise PersistenceConfigurationError(
            f"缺少 {POSTGRES_URI_ENV}，无法启用 PostgreSQL 持久化"
        )

    parsed = urlparse(resolved_uri)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise PersistenceConfigurationError(
            f"{POSTGRES_URI_ENV} 必须使用 postgres:// 或 postgresql://"
        )
    if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise PersistenceConfigurationError(
            f"{POSTGRES_URI_ENV} 必须包含主机、用户和数据库名"
        )
    return resolved_uri


def connect_application_database(
    uri: str | None = None,
) -> Connection[dict[str, Any]]:
    """为应用自行管理的会话元数据创建短生命周期数据库连接。"""

    resolved_uri = get_postgres_uri(uri)
    # autocommit 避免只执行一次元数据语句时遗留未提交事务；dict_row 让业务代码按列名读取。
    return connect(resolved_uri, autocommit=True, row_factory=dict_row)


@contextmanager
def open_postgres_checkpointer(
    uri: str | None = None,
) -> Iterator[PostgresSaver]:
    """在 Agent 生命周期内保持 PostgreSQL Checkpointer 连接可用。"""

    resolved_uri = get_postgres_uri(uri)
    with PostgresSaver.from_conn_string(resolved_uri) as checkpointer:
        yield checkpointer


@contextmanager
def open_postgres_store(
    uri: str | None = None,
) -> Iterator[PostgresStore]:
    """在 Agent 生命周期内保持 PostgreSQL 长期 Store 连接可用。"""

    resolved_uri = get_postgres_uri(uri)
    # 当前不传 index 配置，因此只创建持久化 JSON Store；M2 安装 pgvector 后再启用数据库向量索引。
    with PostgresStore.from_conn_string(resolved_uri) as store:
        yield store


@contextmanager
def open_postgres_resources(
    uri: str | None = None,
) -> Iterator[tuple[PostgresSaver, PostgresStore]]:
    """同时打开 Agent 所需的短期 Checkpointer 与长期 Store。"""

    resolved_uri = get_postgres_uri(uri)
    # 两个对象各持有一条连接；即使共用数据库，它们也分别实现不同的 LangGraph 接口。
    with (
        open_postgres_checkpointer(resolved_uri) as checkpointer,
        open_postgres_store(resolved_uri) as store,
    ):
        yield checkpointer, store


def setup_database(uri: str | None = None) -> None:
    """创建或升级 Checkpointer、Store 和应用会话登记表。"""

    resolved_uri = get_postgres_uri(uri)

    # 两个 setup() 都是官方包的迁移入口；应用不能复制其内部表定义，否则框架升级时会失配。
    with open_postgres_resources(resolved_uri) as (checkpointer, store):
        checkpointer.setup()
        store.setup()

    with connect_application_database(resolved_uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CHAT_THREADS_DDL)
            cursor.execute(CHAT_THREADS_INDEX_DDL)


def ensure_thread_owner(
    thread_id: str,
    user_id: str,
    *,
    title: str | None = None,
    uri: str | None = None,
) -> None:
    """登记新会话，或验证已有会话是否属于指定用户。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            # ON CONFLICT DO NOTHING 让并发提交的首条消息具备幂等性；插入后仍查询
            # 实际归属，确保相同 thread_id 不能被另一个用户复用。
            cursor.execute(
                """
                INSERT INTO chat_threads (thread_id, user_id, title)
                VALUES (%s, %s, %s)
                ON CONFLICT (thread_id) DO NOTHING
                """,
                (thread_id, user_id, title),
            )
            cursor.execute(
                """
                SELECT user_id
                FROM chat_threads
                WHERE thread_id = %s AND status = 'active'
                """,
                (thread_id,),
            )
            row = cursor.fetchone()

    if row is None or row["user_id"] != user_id:
        raise ThreadOwnershipError(
            f"Thread {thread_id!r} does not belong to user {user_id!r}"
        )


def verify_thread_owner(
    thread_id: str,
    user_id: str,
    *,
    allow_missing: bool = False,
    uri: str | None = None,
) -> bool:
    """验证会话归属；允许缺失时返回 False，避免只读接口产生新会话。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id
                FROM chat_threads
                WHERE thread_id = %s AND status = 'active'
                """,
                (thread_id,),
            )
            row = cursor.fetchone()

    if row is None and allow_missing:
        return False
    if row is None or row["user_id"] != user_id:
        raise ThreadOwnershipError(
            f"Thread {thread_id!r} does not belong to user {user_id!r}"
        )
    return True


def touch_thread(
    thread_id: str,
    user_id: str,
    *,
    uri: str | None = None,
) -> None:
    """Agent 调用成功后更新会话的最后活动时间。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chat_threads
                SET updated_at = CURRENT_TIMESTAMP
                WHERE thread_id = %s
                  AND user_id = %s
                  AND status = 'active'
                """,
                (thread_id, user_id),
            )
            affected_rows = cursor.rowcount

    if affected_rows != 1:
        raise ThreadOwnershipError(
            f"Thread {thread_id!r} does not belong to user {user_id!r}"
        )


def list_user_threads(
    user_id: str,
    *,
    uri: str | None = None,
) -> list[dict[str, Any]]:
    """查询用户的有效会话列表，不读取 Checkpointer 内部数据。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT thread_id, title, created_at, updated_at
                FROM chat_threads
                WHERE user_id = %s AND status = 'active'
                ORDER BY updated_at DESC
                """,
                (user_id,),
            )
            return list(cursor.fetchall())


def delete_thread_record(
    thread_id: str,
    user_id: str,
    *,
    uri: str | None = None,
) -> None:
    """删除 Checkpointer 会话后，再删除应用层的会话元数据。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM chat_threads
                WHERE thread_id = %s AND user_id = %s
                """,
                (thread_id, user_id),
            )
            affected_rows = cursor.rowcount

    if affected_rows != 1:
        raise ThreadOwnershipError(
            f"Thread {thread_id!r} does not belong to user {user_id!r}"
        )


def main() -> None:
    """执行显式指定的持久化管理命令。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--setup",
        action="store_true",
        help="创建或升级 PostgreSQL Checkpointer、Store 和 chat_threads 表。",
    )
    args = parser.parse_args()

    if not args.setup:
        parser.error("未指定操作，请使用 --setup")

    setup_database()
    print("PostgreSQL Checkpointer、Store 和会话登记表已准备完成。")


if __name__ == "__main__":
    main()
