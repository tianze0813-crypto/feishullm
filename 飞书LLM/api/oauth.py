import asyncio

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from core.event_handler import handle_message_event
from core.formatter import build_notice_card
from feishu_client.auth import auth_client
from feishu_client.message import message_client
from utils.cache import cache
from utils.feishu_oauth import build_authorize_url
from utils.logger import get_logger

router = APIRouter(prefix="/oauth", tags=["oauth"])
logger = get_logger()


@router.get("/authorize-url")
async def authorize_url():
    return JSONResponse(content={"authorize_url": build_authorize_url()})


def _render_oauth_result_page(success: bool, title: str, detail: str) -> str:
    status = "授权成功" if success else "授权失败"
    hint = "你可以关闭此页面，回到飞书继续对话。" if success else "请返回飞书重试，或联系管理员检查应用配置。"
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial; background:#f6f7f9; margin:0; }}
    .wrap {{ max-width: 560px; margin: 72px auto; padding: 0 16px; }}
    .card {{ background:#fff; border-radius: 12px; padding: 20px 18px; box-shadow: 0 6px 24px rgba(0,0,0,.06); }}
    .title {{ font-size: 18px; font-weight: 600; margin: 0 0 8px; }}
    .status {{ display:inline-block; font-size: 13px; padding: 4px 10px; border-radius: 999px; background: {"#e8f7ee" if success else "#fdecec"}; color: {"#167a3f" if success else "#b42318"}; }}
    .detail {{ color:#1f2329; margin: 12px 0 6px; line-height: 1.5; }}
    .hint {{ color:#646a73; margin: 0; line-height: 1.5; }}
    .btns {{ margin-top: 14px; display:flex; gap:10px; flex-wrap:wrap; }}
    .btn {{ border: 1px solid #d0d3d6; background:#fff; border-radius: 10px; padding: 10px 12px; font-size: 14px; cursor: pointer; }}
    .btn.primary {{ background:#3370ff; border-color:#3370ff; color:#fff; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <div class=\"status\">{status}</div>
      <h1 class=\"title\">{title}</h1>
      <p class=\"detail\">{detail}</p>
      <p class=\"hint\">{hint}</p>
      <div class=\"btns\">
        <button class=\"btn primary\" onclick=\"try{{window.close()}}catch(e){{}}\">关闭页面</button>
        <button class=\"btn\" onclick=\"location.href='/'\">回到首页</button>
      </div>
    </div>
  </div>
</body>
</html>"""


@router.get("/callback")
async def oauth_callback(code: str | None = None, format: str = "html"):
    if not code:
        if format == "json":
            return JSONResponse(status_code=400, content={"message": "missing code"})
        return HTMLResponse(
            status_code=400,
            content=_render_oauth_result_page(False, "授权失败", "缺少必要参数 code。"),
        )

    auth_succeeded = False
    token_data: dict[str, object] = {}
    open_id: str | None = None
    resume_error = ""

    try:
        try:
            token_data = await auth_client.exchange_oauth_code(code)
            auth_succeeded = True
        except Exception as exc:
            logger.exception("oauth callback exchange failed")
            detail = f"授权码换取用户身份失败：{exc}"
            if format == "json":
                return JSONResponse(status_code=400, content={"message": detail})
            return HTMLResponse(
                status_code=400,
                content=_render_oauth_result_page(False, "授权失败", detail),
            )

        raw_open_id = token_data.get("open_id")
        if isinstance(raw_open_id, str) and raw_open_id:
            open_id = raw_open_id
            try:
                pending_key = f"pending:{open_id}"
                pending_value = cache.get(pending_key)
                pending_question = ""
                pending_chat_id: str | None = None
                if isinstance(pending_value, dict):
                    pending_question = str(pending_value.get("text") or "").strip()
                    raw_chat = str(pending_value.get("chat_id") or "").strip()
                    pending_chat_id = raw_chat or None
                elif isinstance(pending_value, str):
                    pending_question = pending_value.strip()
                if pending_question:
                    await message_client.send_card(
                        open_id,
                        build_notice_card("飞书智能助手 - 授权成功", "已为你开启权限，正在继续处理原问题..."),
                    )
                else:
                    await message_client.send_card(
                        open_id,
                        build_notice_card("飞书智能助手 - 授权成功", "已为你开启权限。"),
                    )
                if pending_question:
                    cache.delete(pending_key)
                    asyncio.create_task(
                        handle_message_event(
                            user_open_id=open_id,
                            message_text=pending_question,
                            chat_id=pending_chat_id,
                        )
                    )
            except Exception as exc:
                logger.exception("oauth callback resume pending question failed open_id={}", open_id)
                resume_error = f"授权已成功，但恢复原问题失败：{exc}"
        else:
            logger.warning("oauth callback success but open_id missing: {}", token_data)
            resume_error = "授权响应缺少 open_id，请联系管理员检查应用权限配置。"
    except Exception:
        logger.exception(
            "oauth callback unexpected failure after auth success={} open_id={}",
            auth_succeeded,
            open_id or "",
        )
        if auth_succeeded:
            resume_error = resume_error or "授权已成功，但回调页渲染失败；请返回飞书重试原问题。"
        else:
            detail = "回调处理发生异常，请重试授权。"
            if format == "json":
                return JSONResponse(status_code=500, content={"message": detail})
            return HTMLResponse(
                status_code=500,
                content=_render_oauth_result_page(False, "授权失败", detail),
            )

    if format == "json":
        return JSONResponse(
            status_code=200,
            content={
                "message": "oauth success" if not resume_error else resume_error,
                "auth_succeeded": auth_succeeded,
                "open_id": open_id,
                "scope": token_data.get("scope"),
            },
        )

    if resume_error:
        return HTMLResponse(
            status_code=200,
            content=_render_oauth_result_page(True, "授权部分成功", resume_error),
        )

    return HTMLResponse(
        content=_render_oauth_result_page(True, "授权成功", "已完成授权，你可以回到飞书继续使用智能助手。")
    )
