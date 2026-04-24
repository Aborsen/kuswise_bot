"""GPT-4o vision-based food photo analysis."""
import base64
import json

from openai import OpenAI

from lib.config import OPENAI_API_KEY, ANALYSIS_SYSTEM_PROMPT

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # Remove leading ``` or ```json and trailing ```
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def analyze_photo(image_bytes: bytes, retry_prompt: str | None = None) -> tuple[dict, str]:
    """Analyze a food photo. Returns (parsed_dict, raw_response_text).

    Retries parsing once (with a reminder) if the first response isn't valid JSON.
    If retry_prompt is provided (for recalculate), it's appended as an extra instruction.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    client = _get_client()

    user_text = "Analyze this meal."
    if retry_prompt:
        user_text += f"\n\n{retry_prompt}"

    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
                {"type": "text", "text": user_text},
            ],
        },
    ]

    resp = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1500,
        messages=messages,
    )
    raw = resp.choices[0].message.content or ""
    try:
        return json.loads(_strip_fences(raw)), raw
    except json.JSONDecodeError:
        pass

    # Retry once with an explicit reminder
    messages.append({"role": "assistant", "content": raw})
    messages.append({
        "role": "user",
        "content": "Your previous reply was not valid JSON. Reply again with ONLY the JSON object, no markdown, no prose.",
    })
    resp2 = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1500,
        messages=messages,
    )
    raw2 = resp2.choices[0].message.content or ""
    return json.loads(_strip_fences(raw2)), raw2


def analyze_text(description: str, retry_prompt: str | None = None) -> tuple[dict, str]:
    """Analyze a user's free-text description of a meal.

    Returns (parsed_dict, raw_response_text) with the same JSON schema as analyze_photo.
    """
    client = _get_client()

    extra = f"\n\n{retry_prompt}" if retry_prompt else ""
    user_prompt = (
        "Опис страви від користувача: \n"
        f"{description}\n\n"
        "Проаналізуй цей опис так, ніби це фото, і поверни ТОЧНО ту саму JSON-структуру. "
        "Якщо кількість (грами / порція) не вказана, припусти розумну стандартну порцію і вкажи "
        f"її в estimated_portion (наприклад '~300г припущено'). Відповідай лише валідним JSON.{extra}"
    )

    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    resp = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1500,
        messages=messages,
    )
    raw = resp.choices[0].message.content or ""
    try:
        return json.loads(_strip_fences(raw)), raw
    except json.JSONDecodeError:
        pass

    messages.append({"role": "assistant", "content": raw})
    messages.append({
        "role": "user",
        "content": "Your previous reply was not valid JSON. Reply again with ONLY the JSON object, no markdown, no prose.",
    })
    resp2 = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1500,
        messages=messages,
    )
    raw2 = resp2.choices[0].message.content or ""
    return json.loads(_strip_fences(raw2)), raw2
