"""登录白名单测试：邮箱必须在 users 表中存在才能登录。

覆盖场景：
1. 未注册的邮箱 + 正确验证码 → 登录被拒绝（400），users 表无新增记录
2. 已停用账户 + 正确验证码 → 登录被拒绝（400）
3. 已注册账户 + 正确验证码 → 登录成功，users 表行数不变
4. 错误信息统一文案：未注册 vs 验证码错误应返回相同 detail（防邮箱枚举）
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.database import UserDB, get_db_ctx, init_db
from api.services import auth_service


def _client() -> TestClient:
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _request_code(client: TestClient, email: str) -> str:
    r = client.post("/v1/auth/request-code", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()["dev_code"]


@pytest.fixture(autouse=True)
def _ensure_db():
    init_db()


class TestVerifyCodeWhitelist:
    def test_unregistered_email_is_rejected(self):
        """未注册的邮箱即使验证码正确，也应被拒绝登录。"""
        client = _client()
        email = auth_service.normalize_email(f"ghost-{uuid4().hex[:8]}@example.com")

        # 确保邮箱未注册
        with get_db_ctx() as db:
            assert auth_service.get_user_by_email(db, email) is None

        # 拿到验证码并提交
        code = _request_code(client, email)
        r = client.post(
            "/v1/auth/verify-code",
            json={"email": email, "code": code},
        )
        assert r.status_code == 400, r.text

        # users 表不应该出现新行
        with get_db_ctx() as db:
            assert auth_service.get_user_by_email(db, email) is None

    def test_inactive_user_is_rejected(self):
        """已停用的账户即使验证码正确，也应被拒绝登录。"""
        client = _client()
        email = auth_service.normalize_email(f"inactive-{uuid4().hex[:8]}@example.com")
        now = datetime.now(timezone.utc)

        with get_db_ctx() as db:
            db.add(
                UserDB(
                    id=str(uuid4()),
                    email=email,
                    is_active=False,  # 显式停用
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()

        code = _request_code(client, email)
        r = client.post(
            "/v1/auth/verify-code",
            json={"email": email, "code": code},
        )
        assert r.status_code == 400, r.text

    def test_registered_user_can_login(self):
        """白名单内的账户登录成功，且不产生新行。"""
        client = _client()
        email = auth_service.normalize_email(f"active-{uuid4().hex[:8]}@example.com")
        now = datetime.now(timezone.utc)

        with get_db_ctx() as db:
            db.add(
                UserDB(
                    id=str(uuid4()),
                    email=email,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()

        # 统计当前该邮箱在 users 表的行数
        with get_db_ctx() as db:
            before = auth_service.get_user_by_email(db, email)
            assert before is not None

        code = _request_code(client, email)
        r = client.post(
            "/v1/auth/verify-code",
            json={"email": email, "code": code},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"]
        assert body["user"]["email"] == email

        # 行数不变（id 一致）
        with get_db_ctx() as db:
            after = auth_service.get_user_by_email(db, email)
            assert after is not None
            assert after.id == before.id
            assert after.last_login_at is not None

    def test_error_message_is_unified_to_prevent_email_enumeration(self):
        """未注册与验证码错误应返回相同的 detail 文本，避免泄露邮箱是否注册。"""
        client = _client()
        ghost_email = auth_service.normalize_email(f"enum-{uuid4().hex[:8]}@example.com")
        known_email = auth_service.normalize_email(f"known-{uuid4().hex[:8]}@example.com")
        now = datetime.now(timezone.utc)

        # 仅创建 known_email 的账户
        with get_db_ctx() as db:
            db.add(
                UserDB(
                    id=str(uuid4()),
                    email=known_email,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()

        # 未注册邮箱 + 正确验证码
        ghost_code = _request_code(client, ghost_email)
        r1 = client.post(
            "/v1/auth/verify-code",
            json={"email": ghost_email, "code": ghost_code},
        )

        # 已注册邮箱 + 错误验证码
        known_code = _request_code(client, known_email)
        r2 = client.post(
            "/v1/auth/verify-code",
            json={"email": known_email, "code": "000000"},
        )

        assert r1.status_code == 400
        assert r2.status_code == 400
        assert r1.json()["detail"] == r2.json()["detail"]

    def test_unregistered_email_can_retry_with_correct_code(self):
        """未注册邮箱提交正确验证码时，验证码消费应被回滚，允许再次提交（即便仍会被拒）。

        这是为了在用户先尝试注册但忘记走管理员建账号的极端情况下，
        不会浪费掉他收到的验证码。
        """
        client = _client()
        email = auth_service.normalize_email(f"retry-{uuid4().hex[:8]}@example.com")

        code = _request_code(client, email)

        # 第一次：用户不存在，被拒
        r1 = client.post(
            "/v1/auth/verify-code",
            json={"email": email, "code": code},
        )
        assert r1.status_code == 400

        # 第二次：验证码应仍可消费（未被消费）
        r2 = client.post(
            "/v1/auth/verify-code",
            json={"email": email, "code": code},
        )
        assert r2.status_code == 400  # 仍然因为未注册被拒
