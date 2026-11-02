"""Tiny Workers AI client (OpenAI-compatible endpoint), standard library only.

Reads CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID from ../.env or the environment.
If AI_GATEWAY_ID is set, calls go through Cloudflare AI Gateway (logs, cost analytics, caching).
"""
import json
import os
import random
import time
import urllib.error
import urllib.request

# USD per million tokens (input, output), from the Workers AI model catalog (Oct 2026).
PRICES = {
    "@cf/google/gemma-4-26b-a4b-it": (0.10, 0.30),
    "@cf/moonshotai/kimi-k2.6": (0.95, 4.00),
    "@cf/zai-org/glm-4.7-flash": (0.0605, 0.40),
    "@cf/deepseek-ai/deepseek-v4.1-flash-1017": (0.30, 1.20),
    "@cf/zai-org/glm-5.3": (1.40, 4.40),
    "@cf/openai/gpt-oss-120b": (0.35, 0.75),
    "@cf/meta/llama-4-scout-17b-16e-instruct": (0.27, 0.85),
}


def load_env():
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "f1b142072a65e92e0f2913f67917d8e1")
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
GATEWAY = os.environ.get("AI_GATEWAY_ID", "")

if GATEWAY:
    URL = f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT}/{GATEWAY}/workers-ai/v1/chat/completions"
else:
    URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions"


def chat(model, messages, max_tokens=1500, temperature=0, json_mode=True, retries=8, thinking=False):
    """Returns (text, usage). Retries on rate limits and transient errors."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if not thinking:
        # Thinking off: for extraction it costs ~30x the output tokens and ~9x the latency for no gain.
        body["chat_template_kwargs"] = {"enable_thinking": False}
        if "gpt-oss" in model:  # reasoning is mandatory on gpt-oss: keep it minimal
            body["reasoning_effort"] = "low"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(URL, data=data, headers={
            "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
            "User-Agent": "hq-ap-inbox-agent-pipeline/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
            msg = out["choices"][0]["message"]
            return msg.get("content") or "", out.get("usage", {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(min(60, 2 ** attempt) + random.random() * 3)  # capacity errors need patience
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.random())
                continue
            raise RuntimeError(str(e))


def parse_json(text):
    """Lenient JSON parse: strips code fences / leading chatter."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
    return json.loads(text[start:])


def cost(model, usage):
    pin, pout = PRICES.get(model, (0, 0))
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1e6
