from __future__ import annotations

from llm.client import llm_client
from utils.logger import get_logger

logger = get_logger()

_SUMMARY_TRIGGER_CHARS = 900


async def summarize_context_sections(question: str, context_sections: list[str]) -> tuple[list[str], bool]:
    if not context_sections:
        return [], False

    summarized: list[str] = []
    changed = False
    for section in context_sections:
        label, body = _split_section(section)
        if len(body) < _SUMMARY_TRIGGER_CHARS:
            summarized.append(section)
            continue
        try:
            summary = await llm_client.summarize_evidence(question, body, source_label=label)
        except Exception:
            logger.exception("summarize evidence failed for source {}", label)
            summarized.append(section)
            continue
        summary = (summary or "").strip()
        if not summary or summary == "（无关键摘要）":
            summarized.append(section)
            continue
        summarized.append(f"[来源：{label} 摘要]\n{summary}")
        changed = True
    return summarized, changed


def _split_section(section: str) -> tuple[str, str]:
    text = (section or "").strip()
    if not text:
        return "未标注来源", ""
    if "\n" not in text:
        return text.strip("[]"), text
    first, rest = text.split("\n", 1)
    label = first.strip()
    if label.startswith("[来源：") and label.endswith("]"):
        label = label[len("[来源：") : -1]
    return label or "未标注来源", rest.strip()
