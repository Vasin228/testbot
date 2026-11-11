import json
import re
import time
from typing import Any
from openai import AsyncOpenAI
from src.db import db, DEFAULT_SYSTEM_PROMPT


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """Robustly extract and parse JSON object from LLM response text."""
    # 1. Check for markdown code fences
    fence_pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
    match = re.search(fence_pattern, raw_text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Find outermost curly braces
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end > start:
        candidate = raw_text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Attempt to fix trailing commas
            cleaned = re.sub(r",\s*([\]}])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not parse valid JSON from LLM output: {raw_text[:200]}")


class LLMEngine:
    """Dynamic OpenAI-compatible client supporting OpenRouter, LocalAI, Ollama, etc."""

    async def get_client_and_params(self) -> tuple[AsyncOpenAI, str, str]:
        """Fetch latest credentials and active model directly from SQLite."""
        base_url = await db.get_setting("openai_base_url") or "https://api.openai.com/v1"
        api_key = await db.get_setting("openai_api_key") or "dummy-key"
        model = await db.get_setting("openai_model") or "gpt-4o-mini"

        client = AsyncOpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
        )
        return client, model, base_url

    async def test_connection(self) -> tuple[bool, str]:
        """Send a lightweight ping request to verify endpoint connectivity and credentials."""
        try:
            client, model, base_url = await self.get_client_and_params()
            start_time = time.perf_counter()
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "user", "content": "Respond with 'OK' and nothing else."}
                ],
                max_tokens=15,
                timeout=15.0,
            )
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            content = (response.choices[0].message.content or "").strip()
            return (
                True,
                f"Подключение успешно! Ответ: '{content}' за {latency_ms} мс.\nМодель: {model}\nURL: {base_url}",
            )
        except Exception as e:
            return False, f"Ошибка подключения к LLM: {str(e)}"

    async def analyze_and_route(
        self,
        user_text: str,
        sandbox_data: dict[str, Any],
        existing_topics: list[dict[str, Any]],
        allow_new_topics: bool,
    ) -> dict[str, Any]:
        """Analyze content and route it to an existing topic or suggest a new one."""
        client, model, _ = await self.get_client_and_params()
        system_prompt = await db.get_setting("system_prompt") or DEFAULT_SYSTEM_PROMPT

        topics_summary = [
            {
                "thread_id": t["thread_id"],
                "name": t["name"],
                "description": t.get("description", ""),
            }
            for t in existing_topics
        ]

        prompt = (
            f"Тебе предоставлен контент, присланный пользователем в группу.\n"
            f"Сообщение пользователя: {user_text or 'Отсутствует'}\n"
            f"Заголовок материала: {sandbox_data.get('title', 'Без названия')}\n"
            f"URL: {sandbox_data.get('url', 'Без ссылки')}\n\n"
            f"--- ДАННЫЕ ИЗ ПЕСОЧНИЦЫ (ТЕХНИЧЕСКИЙ ПАСПОРТ) ---\n"
            f"{sandbox_data.get('raw_context', '')[:10000]}\n\n"
            f"--- СУЩЕСТВУЮЩИЕ ВЕТКИ ФОРУМА ---\n"
            f"{json.dumps(topics_summary, ensure_ascii=False, indent=2)}\n\n"
            f"ПРАВИЛА:\n"
            f"1. Сформируй четкое, структурированное резюме.\n"
            f"2. Если есть подходящая ветка среди существующих — action='route_existing' и укажи target_thread_id.\n"
            f"3. Если подходящей ветки нет и разрешено автосоздание (allow_new_topics={allow_new_topics}) — action='create_new' "
            f"и предложи лаконичное имя с эмодзи (new_topic_name, до 40 символов) и описание.\n"
            f"4. Если автосоздание запрещено, ОБЯЗАТЕЛЬНО выбери наиболее близкую существующую ветку (action='route_existing').\n"
            f"5. Ответ верни СТРОГО в виде валидного JSON-объекта следующей структуры:\n"
            f"{{\n"
            f'  "title": "Краткое и емкое название проекта/статьи/файла",\n'
            f'  "tldr": "Суть в 2-3 предложениях без воды",\n'
            f'  "use_cases": ["Зачем это нужно: сценарий 1", "Сценарий 2"],\n'
            f'  "tech_stack": ["#python", "#llm", "#devops"],\n'
            f'  "action": "route_existing" | "create_new",\n'
            f'  "target_thread_id": 123,\n'
            f'  "new_topic_name": "🤖 AI & Agents",\n'
            f'  "new_topic_description": "Описание новой ветки"\n'
            f"}}\n"
        )

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        try:
            parsed = extract_json_payload(content)
        except Exception:
            # Fallback if model returned unstructured text
            default_target = existing_topics[0]["thread_id"] if existing_topics else 1
            parsed = {
                "title": sandbox_data.get("title", "Материал"),
                "tldr": content[:300],
                "use_cases": ["Изучение материала", "Практическое применение в проектах"],
                "tech_stack": ["#useful", "#dev"],
                "action": "route_existing",
                "target_thread_id": default_target,
                "new_topic_name": "",
                "new_topic_description": "",
            }

        # Guardrails on action when new topics are disallowed or missing target
        if not allow_new_topics and parsed.get("action") == "create_new":
            parsed["action"] = "route_existing"
            if existing_topics:
                parsed["target_thread_id"] = existing_topics[0]["thread_id"]

        if parsed.get("action") == "route_existing" and not parsed.get("target_thread_id"):
            if existing_topics:
                parsed["target_thread_id"] = existing_topics[0]["thread_id"]

        return parsed

    async def vibe_check_audit(
        self, item_record: dict[str, Any], mode: str
    ) -> str:
        """Deep dive technical audit ('audit') or step-by-step rollout guide ('quickstart')."""
        client, model, _ = await self.get_client_and_params()
        title = item_record.get("title", "Материал")
        source_url = item_record.get("source_url", "")
        raw_context = item_record.get("raw_context", "")[:12000]

        if mode == "audit":
            system_msg = (
                "Ты — Senior Tech Lead и Software Architect. Проведи бескомпромиссный "
                "архитектурный аудит предоставленного проекта/материала. Разложи по полочкам: "
                "архитектурные решения, сильные стороны, скрытые подводные камни, масштабируемость, "
                "лицензионные и технические риски. Отвечай структурированно, профессионально и без воды."
            )
            user_msg = (
                f"Материал: {title}\n"
                f"Источник: {source_url}\n\n"
                f"--- КОНТЕКСТ ИСХОДНИКОВ И ПЕСОЧНИЦЫ ---\n{raw_context}\n\n"
                f"Сделай разбор по разделам:\n"
                f"1. 🏛 Архитектура и стек\n"
                f"2. ⚡️ Сильные стороны и киллер-фичи\n"
                f"3. ⚠️ Подводные камни, ограничения и риски\n"
                f"4. 🎯 Итоговый вердикт (готовность к продакшену)"
            )
        else:  # quickstart
            system_msg = (
                "Ты — DevOps Lead и Staff Engineer. Напиши четкую пошаговую инструкцию "
                "по быстрому развертыванию, запуску и внедрению данного инструмента в стек команды. "
                "Включай конкретные команды, переменные окружения и сценарий верификации."
            )
            user_msg = (
                f"Материал: {title}\n"
                f"Источник: {source_url}\n\n"
                f"--- КОНТЕКСТ ИСХОДНИКОВ И ПЕСОЧНИЦЫ ---\n{raw_context}\n\n"
                f"Сделай пошаговый гайд:\n"
                f"1. 📋 Пререквизиты и зависимости\n"
                f"2. 🚀 Быстрый запуск (шаги и команды)\n"
                f"3. 🛠 Интеграция в рабочий процесс\n"
                f"4. ✅ Чек-лист проверки работоспособности"
            )

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or "Не удалось сформировать отчет."

    async def search_synthesize(
        self, query: str, matched_items: list[dict[str, Any]], clean_chat_id: str
    ) -> str:
        """Synthesize a comprehensive answer based on matched knowledge items."""
        client, model, _ = await self.get_client_and_params()
        items_text = []
        for item in matched_items:
            tid = item.get("thread_id") or 1
            mid = item.get("message_id") or 1
            post_link = f"https://t.me/c/{clean_chat_id}/{tid}/{mid}"
            items_text.append(
                f"ID #{item['id']}: <a href=\"{post_link}\">{item['title']}</a>\n"
                f"Суть: {item.get('summary', '')}\n"
                f"Применение: {item.get('use_cases', '')}\n"
                f"Теги: {item.get('tags', '')}\n"
                f"Ссылка на источник: {item.get('source_url', '')}\n"
            )

        context_joined = "\n---\n".join(items_text)
        prompt = (
            f"Пользователь ищет в базе знаний команды: '{query}'\n\n"
            f"Найденные релевантные карточки:\n{context_joined}\n\n"
            f"Сформируй четкий, полезный ответ на вопрос пользователя. "
            f"Если ни одна из карточек не подходит по смыслу под запрос — честно напиши, что в базе знаний ничего подходящего не найдено. "
            f"Для подходящих материалов обязательно дай HTML-ссылки (<a href=\"...\">Название</a>). "
            f"Используй ТОЛЬКО HTML-теги Telegram (<b>, <i>, <code>, <a href=\"...\">), БЕЗ Markdown (** или ###). "
            f"Объясни, как найденные материалы решают задачу пользователя."
        )

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "Ты — AI-ассистент базы знаний команды разработчиков.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or "Ничего не найдено."

    async def general_chat(
        self, user_question: str, context_text: str = ""
    ) -> str:
        """General assistant response for replies and direct mentions in topics."""
        client, model, _ = await self.get_client_and_params()
        system_prompt = await db.get_setting("system_prompt") or DEFAULT_SYSTEM_PROMPT

        messages = [{"role": "system", "content": system_prompt}]
        if context_text:
            messages.append(
                {
                    "role": "system",
                    "content": f"Контекст страницы / материалов:\n{context_text[:8000]}",
                }
            )
        messages.append({"role": "user", "content": user_question})

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
        )
        return response.choices[0].message.content or "Не удалось получить ответ."


llm_engine = LLMEngine()
