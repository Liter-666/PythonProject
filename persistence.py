"""基于 MySQL 的检查点持久化与应用会话登记。"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, urlparse

import pymysql
from dotenv import load_dotenv
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from pymysql.connections import Connection
from pymysql.cursors import DictCursor


# 在读取 MYSQL_CHECKPOINT_URI 前加载本地开发环境配置。
load_dotenv()

MYSQL_CHECKPOINT_URI_ENV = "MYSQL_CHECKPOINT_URI"

# 检查点相关表由 LangGraph 管理；本表只保存应用层的会话归属和展示信息，
# 避免业务代码直接查询或解析 LangGraph 的检查点二进制数据。
CHAT_THREADS_DDL = """
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    title VARCHAR(255) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (thread_id),
    INDEX idx_chat_threads_user_updated (user_id, updated_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
"""


class PersistenceConfigurationError(RuntimeError):
    """MySQL 持久化配置缺失或格式错误。"""


class ThreadOwnershipError(PermissionError):
    """用户尝试访问不属于自己的会话。"""


def get_mysql_checkpoint_uri(uri: str | None = None) -> str:
    """优先返回显式传入的 URI，否则读取 MySQL 检查点连接配置。"""

    resolved_uri = uri or os.getenv(MYSQL_CHECKPOINT_URI_ENV)
    if not resolved_uri:
        raise PersistenceConfigurationError(
            f"{MYSQL_CHECKPOINT_URI_ENV} is required for MySQL persistence"
        )
    return resolved_uri


def _connection_kwargs(uri: str) -> dict[str, Any]:
    """将 mysql:// URI 转换为 PyMySQL 所需的显式连接参数。"""

    parsed = urlparse(uri)
    if parsed.scheme != "mysql":
        raise PersistenceConfigurationError(
            f"{MYSQL_CHECKPOINT_URI_ENV} must use the mysql:// scheme"
        )
    if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise PersistenceConfigurationError(
            f"{MYSQL_CHECKPOINT_URI_ENV} must include host, user and database"
        )

    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": unquote(parsed.path.lstrip("/")),
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": DictCursor,
    }


def connect_application_database(uri: str | None = None) -> Connection:
    """为应用自行管理的会话元数据创建短生命周期数据库连接。"""

    resolved_uri = get_mysql_checkpoint_uri(uri)
    return pymysql.connect(**_connection_kwargs(resolved_uri))


@contextmanager
def open_mysql_checkpointer(
    uri: str | None = None,
) -> Iterator[PyMySQLSaver]:
    """在 Agent 生命周期内保持 MySQL Checkpointer 连接可用。"""

    resolved_uri = get_mysql_checkpoint_uri(uri)
    with PyMySQLSaver.from_conn_string(resolved_uri) as checkpointer:
        yield checkpointer


def setup_database(uri: str | None = None) -> None:
    """创建或升级检查点表，并创建应用自己的会话登记表。"""

    resolved_uri = get_mysql_checkpoint_uri(uri)

    # setup() 是 Checkpointer 包提供的迁移入口；应用代码不应复制或修改
    # 它所管理的四张内部表结构，防止框架升级时出现表结构不一致。
    with open_mysql_checkpointer(resolved_uri) as checkpointer:
        checkpointer.setup()

    with connect_application_database(resolved_uri) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CHAT_THREADS_DDL)


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
            # INSERT IGNORE 让并发提交的首条消息具备幂等性；插入后仍必须查询
            # 实际归属，确保相同 thread_id 不能被另一个用户复用。
            cursor.execute(
                """
                INSERT IGNORE INTO chat_threads (thread_id, user_id, title)
                VALUES (%s, %s, %s)
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


def touch_thread(
    thread_id: str,
    user_id: str,
    *,
    uri: str | None = None,
) -> None:
    """Agent 调用成功后更新会话的最后活动时间。"""

    with connect_application_database(uri) as connection:
        with connection.cursor() as cursor:
            affected_rows = cursor.execute(
                """
                UPDATE chat_threads
                SET updated_at = CURRENT_TIMESTAMP(6)
                WHERE thread_id = %s
                  AND user_id = %s
                  AND status = 'active'
                """,
                (thread_id, user_id),
            )

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
            affected_rows = cursor.execute(
                """
                DELETE FROM chat_threads
                WHERE thread_id = %s AND user_id = %s
                """,
                (thread_id, user_id),
            )

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
        help="创建或升级 MySQL 检查点表和 chat_threads 表。",
    )
    args = parser.parse_args()

    if not args.setup:
        parser.error("未指定操作，请使用 --setup")

    setup_database()
    print("MySQL 检查点表和会话登记表已准备完成。")


if __name__ == "__main__":
    main()
