from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.oauth import router as oauth_router
from config import get_ignored_token_envs, get_settings_summary
from feishu_client.ws_client import ws_client
from utils.http import aclose_all
from utils.logger import get_logger
from utils.token_store import user_token_store

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup：打印配置摘要、预热 token store、启动飞书 WS 长连接
    summary = get_settings_summary()
    logger.info("settings summary: {}", summary)
    if summary["app_id_len"] == 0 or summary["app_secret_len"] == 0:
        logger.error(
            "APP_ID/APP_SECRET not loaded, check .env at {}",
            summary["env_file"],
        )
    ignored_token_envs = get_ignored_token_envs()
    if ignored_token_envs:
        logger.warning(
            "token env vars are ignored by design: {}",
            ",".join(ignored_token_envs),
        )
    # 预热用户 token store：首次请求不付文件 I/O，且可以直观看到重启后恢复了多少用户
    loaded = user_token_store.load()
    logger.info(
        "user token store warmed up: {} user(s) restored from {}",
        loaded,
        user_token_store.path,
    )
    await ws_client.start()
    logger.info("feishu websocket client started")

    try:
        yield
    finally:
        # shutdown：主动 aclose 共享 httpx client，释放 keep-alive 连接池
        try:
            await aclose_all()
            logger.info("shared httpx clients closed")
        except Exception:
            logger.exception("failed to close shared httpx clients")


app = FastAPI(title="Feishu Smart Agent", lifespan=lifespan)
app.include_router(oauth_router)


@app.get("/ping")
async def ping():
    return {"status": "ok"}
