import json
from typing import Any

import httpx

from config import settings
from llm.prompts import (
    ANSWER_PROMPT,
    GENERAL_CHAT_PROMPT,
    INTENT_PROMPT,
    KNOWLEDGE_ROUTING_PROMPT,
    KNOWLEDGE_SELF_CHECK_PROMPT,
    PERSON_ROUTING_PROMPT,
    PERSON_SELF_CHECK_PROMPT,
    PERSON_SYNTHESIS_PROMPT,
    QUERY_EXPANSION_PROMPT,
    QUESTION_REWRITE_PROMPT,
    SUMMARY_PROMPT,
    TOPIC_SUMMARY_PROMPT,
    VISION_PROMPT,
)
from utils.http import get_llm_client


class LLMTimeoutError(Exception):
    pass


class DeepSeekClient:
    async def classify_intent(self, question: str, conversation_context: str = "") -> str:
        prompt = INTENT_PROMPT.format(
            question=question,
            conversation_context=conversation_context or "（无）",
        )
        return await self._chat(prompt)

    async def resolve_pronouns(
        self,
        question: str,
        conversation_context: str,
        session_state: str = "",
    ) -> str:
        prompt = QUESTION_REWRITE_PROMPT.format(
            question=question,
            conversation_context=conversation_context or "（无）",
            session_state=session_state or "（无）",
        )
        result = await self._chat(prompt)
        return result.strip()

    async def answer(self, question: str, context: str, conversation_context: str = "") -> str:
        prompt = ANSWER_PROMPT.format(
            question=question,
            context=context or "暂无检索结果",
            conversation_context=conversation_context or "（无）",
        )
        content = await self._chat(prompt)
        return content.strip()

    async def general_chat(self, question: str, conversation_context: str = "") -> str:
        prompt = GENERAL_CHAT_PROMPT.format(
            question=question,
            conversation_context=conversation_context or "（无）",
        )
        content = await self._chat(prompt)
        return content.strip()

    async def topic_summary(
        self,
        question: str,
        context: str,
        conversation_context: str = "",
        preserve_entities: list[str] | None = None,
    ) -> str:
        preserve_entities_text = "（无）"
        if preserve_entities:
            preserve_entities_text = "\n".join(
                f"- {str(item).strip()}"
                for item in preserve_entities
                if str(item or "").strip()
            ) or "（无）"
        prompt = TOPIC_SUMMARY_PROMPT.format(
            question=question,
            context=context or "暂无检索结果",
            conversation_context=conversation_context or "（无）",
            preserve_entities=preserve_entities_text,
        )
        content = await self._chat(prompt)
        return content.strip()

    async def route_search_knowledge(
        self,
        *,
        question: str,
        search_key: str,
        keyword_fallback: str = "",
        conversation_context: str = "",
        summary_mode: bool = False,
    ) -> dict[str, Any]:
        prompt = KNOWLEDGE_ROUTING_PROMPT.format(
            question=question or "",
            search_key=search_key or "",
            keyword_fallback=keyword_fallback or "",
            conversation_context=conversation_context or "（无）",
            summary_mode="true" if summary_mode else "false",
        )
        content = (await self._chat(prompt)).strip()
        data = self._extract_first_json_object(content)
        return data if isinstance(data, dict) else {}

    async def self_check_search_answer(
        self,
        *,
        question: str,
        routing_context: str,
        summary_mode: bool,
        queries: str,
        hit_summary: str,
        context_excerpt: str,
        draft_answer: str,
    ) -> dict[str, Any]:
        prompt = KNOWLEDGE_SELF_CHECK_PROMPT.format(
            question=question or "",
            routing_context=routing_context or "（无）",
            summary_mode="true" if summary_mode else "false",
            queries=queries or "（无）",
            hit_summary=hit_summary or "（无）",
            context_excerpt=context_excerpt or "（无）",
            draft_answer=draft_answer or "（无）",
        )
        content = (await self._chat(prompt)).strip()
        data = self._extract_first_json_object(content)
        return data if isinstance(data, dict) else {}

    async def expand_queries(
        self,
        *,
        intent: str,
        question: str,
        keyword: str,
        keyword_fallback: str = "",
        conversation_context: str = "",
    ) -> list[str]:
        prompt = QUERY_EXPANSION_PROMPT.format(
            intent=intent or "search_knowledge",
            question=question or "",
            keyword=keyword or "",
            keyword_fallback=keyword_fallback or "",
            conversation_context=conversation_context or "（无）",
        )
        content = (await self._chat(prompt)).strip()
        try:
            data = json.loads(content)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in data:
            text = str(item or "").strip()
            if not text or len(text) > 24:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            out.append(text)
            if len(out) >= 5:
                break
        return out

    async def summarize_evidence(self, question: str, content: str, source_label: str = "") -> str:
        prompt = SUMMARY_PROMPT.format(
            question=question,
            source_label=source_label or "未标注来源",
            content=content or "暂无内容",
        )
        content = await self._chat(prompt)
        return content.strip()

    async def analyze_image(self, question: str, image_base64: str, mime_type: str = "image/png") -> str:
        if not settings.deepseek_api_key:
            return "未配置 DeepSeek API Key，暂无法分析图片。"
        url = f"{settings.deepseek_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT.format(question=question)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                        },
                    ],
                }
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        client = await get_llm_client()
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return "模型未返回图片分析内容。"
        return str(choices[0].get("message", {}).get("content", "模型未返回图片分析内容。")).strip()

    async def synthesize_person(
        self,
        question: str,
        contacts: str,
        docs: str,
        messages: str,
        conversation_context: str = "",
    ) -> str:
        prompt = PERSON_SYNTHESIS_PROMPT.format(
            question=question,
            conversation_context=conversation_context or "（无）",
            contacts=contacts or "（无）",
            docs=docs or "（无）",
            messages=messages or "（无）",
        )
        content = await self._chat(prompt)
        return content.strip()

    async def route_find_person(
        self,
        *,
        question: str,
        search_key: str,
        keyword_fallback: str = "",
        conversation_context: str = "",
    ) -> dict[str, Any]:
        prompt = PERSON_ROUTING_PROMPT.format(
            question=question or "",
            search_key=search_key or "",
            keyword_fallback=keyword_fallback or "",
            conversation_context=conversation_context or "（无）",
        )
        content = (await self._chat(prompt)).strip()
        data = self._extract_first_json_object(content)
        return data if isinstance(data, dict) else {}

    async def self_check_person_answer(
        self,
        *,
        question: str,
        routing_context: str,
        queries: str,
        contacts: str,
        docs: str,
        messages: str,
        draft_answer: str,
    ) -> dict[str, Any]:
        prompt = PERSON_SELF_CHECK_PROMPT.format(
            question=question or "",
            routing_context=routing_context or "（无）",
            queries=queries or "（无）",
            contacts=contacts or "（无）",
            docs=docs or "（无）",
            messages=messages or "（无）",
            draft_answer=draft_answer or "（无）",
        )
        content = (await self._chat(prompt)).strip()
        data = self._extract_first_json_object(content)
        return data if isinstance(data, dict) else {}

    async def _chat(self, prompt: str) -> str:
        if not settings.deepseek_api_key:
            return "未配置 DeepSeek API Key，当前返回占位结果。"

        url = f"{settings.deepseek_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        client = await get_llm_client()
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("llm request timeout") from exc
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return "模型未返回内容。"
        return choices[0].get("message", {}).get("content", "模型未返回内容。")

    @staticmethod
    def _extract_first_json_object(text: str) -> Any:
        if not text:
            return None
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[idx:])
                return obj
            except Exception:
                continue
        return None


llm_client = DeepSeekClient()
