"""验证 FastAPI 对 PostgreSQL 资源和 thread 归属的最小集成。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import app
from persistence import delete_thread_record, ensure_thread_owner


pytestmark = pytest.mark.integration


def _postgres_uri() -> str:
    """获取测试连接；不把本机密码写进测试源码。"""

    uri = os.getenv("POSTGRES_URI")
    if not uri:
        pytest.skip("未配置 POSTGRES_URI，跳过 PostgreSQL API 集成测试")
    return uri


def test_state_requires_user_id() -> None:
    """状态接口必须同时接收 thread_id 和 user_id。"""

    _postgres_uri()
    with TestClient(app) as client:
        response = client.get("/api/state", params={"thread_id": "missing-user"})
        assert response.status_code == 422


def test_state_rejects_another_threads_owner() -> None:
    """应用层归属表阻止另一个用户读取相同 thread_id 的 checkpoint。"""

    uri = _postgres_uri()
    thread_id = f"test-api-owner-{uuid4()}"
    owner_id = f"owner-{uuid4()}"
    other_user_id = f"other-{uuid4()}"

    try:
        ensure_thread_owner(thread_id, owner_id, uri=uri)
        with TestClient(app) as client:
            response = client.get(
                "/api/state",
                params={"thread_id": thread_id, "user_id": other_user_id},
            )
            assert response.status_code == 403
    finally:
        delete_thread_record(thread_id, owner_id, uri=uri)
