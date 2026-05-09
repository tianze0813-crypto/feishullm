from llm.client import llm_client


async def run_chitchat(
    open_id: str,
    question: str,
    *,
    conversation_context: str = "",
    force_general: bool = False,
) -> tuple[str, list[str]]:
    _ = open_id
    answer = await llm_client.general_chat(question, conversation_context=conversation_context)
    if force_general:
        return answer, ["通识问答：DeepSeek 自身知识"]
    return answer, ["意图识别：chitchat", "通识问答：DeepSeek 自身知识"]
