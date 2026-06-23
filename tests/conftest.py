"""测试全局配置。

主要职责：
1. 清除 SMTP 相关环境变量，让 `auth_service.send_login_code` 走"开发环境返回验证码"分支，
   这样 `/v1/auth/request-code` 响应里会带上 `dev_code` 字段供测试使用。
2. 强制 `APP_ENV=development`，避免生产模式下不返回验证码。

注意：必须在 `pytest_configure` 中执行清理，因为 `api/main.py` 在 import 时会
调用 `load_dotenv()` 把 `.env` 里的 SMTP 配置加载到 os.environ 中。模块级
的清理会被覆盖，必须等 import 完成后再次清理。
"""
import os


_SMTP_ENV_KEYS = [
    "MAIL_HOST",
    "MAIL_SERVER",
    "SMTP_HOST",
    "MAIL_PORT",
    "SMTP_PORT",
    "MAIL_USER",
    "MAIL_USERNAME",
    "SMTP_USER",
    "MAIL_PASS",
    "MAIL_PASSWORD",
    "SMTP_PASSWORD",
    "MAIL_FROM",
    "SMTP_FROM",
    "MAIL_STARTTLS",
    "SMTP_TLS",
    "MAIL_SSL",
    "MAIL_SSL_TLS",
]


def _strip_smtp_env() -> None:
    """从 os.environ 中移除 SMTP 相关变量，强制 send_login_code 走开发分支。"""
    for key in _SMTP_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["APP_ENV"] = "development"


def pytest_configure(config):  # noqa: ARG001 - pytest hook
    """pytest 启动钩子：先于任何 fixture 执行。"""
    _strip_smtp_env()


def pytest_runtest_setup(item):  # noqa: ARG001 - pytest hook
    """每个测试运行前再次清理 SMTP 环境变量，防止其他代码路径加载 .env。"""
    _strip_smtp_env()
