"""LLM candidate generation.

Config comes only from env vars: AQ_API_KEY, AQ_BASE_URL, LLM_MODEL.
Generation never raises: an API failure becomes a candidate whose code is the
error text, which then fails in the sandbox and shows up as data.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE_URL = "https://api.aqinference.com/v1"
DEFAULT_MODEL = "openai/gpt-4.1-mini"
TEMPERATURE = 0.8
REQUEST_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = (
    "Return only a single Python code block implementing the requested function(s), "
    "no explanation."
)

_FENCE_RE = re.compile(r"```[ \t]*(?:python3?|py)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_OPEN_FENCE_RE = re.compile(r"^\s*```[ \t]*(?:python3?|py)?[ \t]*\r?\n", re.IGNORECASE)


def strip_fences(text: str) -> str:
    """Extract code from a markdown response; fall back to the raw text."""
    if not text:
        return ""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    # Unterminated fence (e.g. response cut off at max tokens).
    unterminated = _OPEN_FENCE_RE.sub("", text, count=1)
    if unterminated != text:
        return unterminated.rstrip("`").strip() + "\n"
    return text.strip() + "\n"


def _config() -> tuple[str | None, str, str]:
    return (
        os.getenv("AQ_API_KEY") or None,
        os.getenv("AQ_BASE_URL") or DEFAULT_BASE_URL,
        os.getenv("LLM_MODEL") or DEFAULT_MODEL,
    )


def error_candidate(message: str) -> str:
    return f"# LLM generation failed: {message}\n"


def _generate_one(client, model: str, prompt: str) -> str:
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=TEMPERATURE,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        content = resp.choices[0].message.content if resp.choices else None
        if not content:
            return error_candidate("empty response from model")
        return strip_fences(content)
    except Exception as exc:
        return error_candidate(f"{type(exc).__name__}: {exc}"[:1000])


def generate_candidates(prompt: str, n: int) -> list[str]:
    """Return exactly n candidate sources, one API call each."""
    api_key, base_url, model = _config()
    if not api_key:
        return [error_candidate("AQ_API_KEY is not set")] * n

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2)
    with ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
        return list(pool.map(lambda _: _generate_one(client, model, prompt), range(n)))
