import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
# 先以 utf-8-sig 读一次（兼容 Windows 记事本带 BOM 的 .env），再以 utf-8 兜底。
load_dotenv(dotenv_path=ENV_FILE, override=False, encoding="utf-8-sig")
load_dotenv(dotenv_path=ENV_FILE, override=False, encoding="utf-8")

IGNORED_TOKEN_ENVS = ("TENANT_ACCESS_TOKEN", "USER_ACCESS_TOKEN")


@dataclass(frozen=True)
class Settings:
    # 全部字段都不再用 os.getenv 作为 dataclass 默认值，避免默认值在类定义时固化。
    app_id: str
    app_secret: str
    feishu_base_url: str
    feishu_web_base_url: str
    oauth_redirect_uri: str
    deepseek_api_key: str
    deepseek_base_url: str
    llm_model: str
    include_p2p_message_search: bool
    processing_timeout_seconds: int
    ws_log_level: str


def _build_settings() -> Settings:
    return Settings(
        app_id=os.getenv("APP_ID", ""),
        app_secret=os.getenv("APP_SECRET", ""),
        feishu_base_url=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn"),
        feishu_web_base_url=os.getenv("FEISHU_WEB_BASE_URL", "https://www.feishu.cn"),
        oauth_redirect_uri=os.getenv(
            "OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/oauth/callback"
        ),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
        include_p2p_message_search=os.getenv(
            "INCLUDE_P2P_MESSAGE_SEARCH", "true"
        ).lower()
        == "true",
        processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "8")),
        ws_log_level=os.getenv("WS_LOG_LEVEL", "INFO"),
    )


settings = _build_settings()


def get_ignored_token_envs() -> list[str]:
    return [name for name in IGNORED_TOKEN_ENVS if os.getenv(name)]


def get_settings_summary() -> dict[str, int | str]:
    """只暴露"是否读到"而不是真实值，避免日志里泄露敏感配置。"""
    return {
        "app_id_len": len(settings.app_id),
        "app_secret_len": len(settings.app_secret),
        "deepseek_api_key_len": len(settings.deepseek_api_key),
        "oauth_redirect_uri": settings.oauth_redirect_uri,
        "env_file": str(ENV_FILE),
        "env_file_exists": ENV_FILE.exists(),
    }
