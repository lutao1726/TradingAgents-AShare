"""用户白名单运维脚本。

自方案 A 实施后，`/v1/auth/verify-code` 不再自动注册账户。管理员必须先在
`users` 表中创建账户，受信任的用户才能通过邮箱验证码登录。

使用示例：
    # 新增用户
    python scripts/manage_users.py add user@example.com

    # 新增并立即启用（默认即启用）
    python scripts/manage_users.py add user1@example.com user2@example.com

    # 列出全部用户
    python scripts/manage_users.py list

    # 停用账户
    python scripts/manage_users.py disable user@example.com

    # 启用账户
    python scripts/manage_users.py enable user@example.com

    # 删除账户（不可恢复）
    python scripts/manage_users.py delete user@example.com

可通过环境变量覆盖数据库路径：
    DATABASE_URL=sqlite:///./data/tradingagents.db python scripts/manage_users.py list
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# 确保脚本独立运行时能找到 api.* 等顶层包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from api.database import UserDB, get_db_ctx, init_db  # noqa: E402
from api.services import auth_service  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cmd_add(emails: list[str]) -> int:
    """新增用户（已存在则跳过并提示）。"""
    init_db()
    added = 0
    skipped = 0
    with get_db_ctx() as db:
        for raw in emails:
            email = auth_service.normalize_email(raw)
            if auth_service.get_user_by_email(db, email):
                print(f"[skip] {email} 已存在")
                skipped += 1
                continue
            now = _utcnow()
            db.add(
                UserDB(
                    id=str(uuid4()),
                    email=email,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            print(f"[add]  {email}")
            added += 1
        db.commit()
    print(f"\n完成：新增 {added} 个，跳过 {skipped} 个。")
    return 0


def cmd_list() -> int:
    """列出所有用户。"""
    init_db()
    with get_db_ctx() as db:
        rows = db.query(UserDB).order_by(UserDB.created_at.asc()).all()
        if not rows:
            print("(users 表为空)")
            return 0
        print(f"{'EMAIL':<40} {'STATUS':<8} {'CREATED_AT':<25} {'LAST_LOGIN':<25}")
        print("-" * 100)
        for u in rows:
            status = "active" if u.is_active else "disabled"
            created = u.created_at.isoformat() if u.created_at else "-"
            last_login = u.last_login_at.isoformat() if u.last_login_at else "-"
            print(f"{u.email:<40} {status:<8} {created:<25} {last_login:<25}")
    return 0


def _set_active(email: str, active: bool) -> int:
    init_db()
    email = auth_service.normalize_email(email)
    with get_db_ctx() as db:
        user = auth_service.get_user_by_email(db, email)
        if not user:
            print(f"[error] {email} 不存在")
            return 1
        user.is_active = active
        user.updated_at = _utcnow()
        db.commit()
    action = "启用" if active else "停用"
    print(f"[ok] 已{action} {email}")
    return 0


def cmd_disable(email: str) -> int:
    return _set_active(email, False)


def cmd_enable(email: str) -> int:
    return _set_active(email, True)


def cmd_delete(email: str, *, force: bool = False) -> int:
    init_db()
    email = auth_service.normalize_email(email)
    with get_db_ctx() as db:
        user = auth_service.get_user_by_email(db, email)
        if not user:
            print(f"[error] {email} 不存在")
            return 1
        if not force:
            confirm = input(f"确认删除 {email}? 该操作不可恢复 [y/N]: ").strip().lower()
            if confirm != "y":
                print("已取消")
                return 0
        db.delete(user)
        db.commit()
    print(f"[ok] 已删除 {email}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TradingAgents-AShare 用户白名单管理",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="新增用户")
    p_add.add_argument("emails", nargs="+", help="邮箱地址（可多个）")

    sub.add_parser("list", help="列出用户")

    p_disable = sub.add_parser("disable", help="停用账户")
    p_disable.add_argument("email")

    p_enable = sub.add_parser("enable", help="启用账户")
    p_enable.add_argument("email")

    p_delete = sub.add_parser("delete", help="删除账户")
    p_delete.add_argument("email")
    p_delete.add_argument(
        "--yes", "-y", action="store_true", help="跳过确认提示",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "add":
        return cmd_add(args.emails)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "disable":
        return cmd_disable(args.email)
    if args.cmd == "enable":
        return cmd_enable(args.email)
    if args.cmd == "delete":
        return cmd_delete(args.email, force=args.yes)

    parser.error(f"未知命令: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
