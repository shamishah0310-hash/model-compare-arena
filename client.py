import os
import time
import asyncio
from typing import AsyncGenerator, Dict, Any, List, Optional
import yaml
import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

def load_config() -> Dict[str, Any]:
    cfg_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    for p_name, p_data in config.get("providers", {}).items():
        env_var = p_data.get("api_key_env")
        if env_var and not p_data.get("api_key"):
            val = os.getenv(env_var, "")
            if val:
                p_data["api_key"] = val
    return config

def calculate_cost(provider: str, model_id: str, prompt_tokens: int, completion_tokens: int, config: Dict[str, Any]) -> Dict[str, Any]:
    prov = config.get("providers", {}).get(provider, {})
    model_info = next((m for m in prov.get("models", []) if m["id"] == model_id), None)
    
    in_rate = float(model_info.get("input_cost_per_1m", 0.50)) if model_info else 0.50
    out_rate = float(model_info.get("output_cost_per_1m", 1.00)) if model_info else 1.00

    total = (prompt_tokens / 1_000_000 * in_rate) + (completion_tokens / 1_000_000 * out_rate)
    return {
        "total_cost": total,
        "formatted": f"${total:.5f}" if total > 0 else "$0.00000",
        "input_rate": in_rate,
        "output_rate": out_rate
    }

class ArenaClient:
    def __init__(self):
        self.config = load_config()

    def get_available_models(self) -> List[Dict[str, Any]]:
        result = []
        for p_id, p_data in self.config.get("providers", {}).items():
            if not p_data.get("enabled", True):
                continue
            for m in p_data.get("models", []):
                result.append({
                    "provider": p_id,
                    "provider_name": p_data.get("name", p_id),
                    "model_id": m["id"],
                    "model_name": m.get("name", m["id"]),
                    "context_window": m.get("context_window", 8192),
                    "input_cost_per_1m": m.get("input_cost_per_1m", 0.0),
                    "output_cost_per_1m": m.get("output_cost_per_1m", 0.0),
                    "has_key": bool(p_data.get("api_key"))
                })
        return result

    async def _mock_stream(self, prompt: str, model: str) -> AsyncGenerator[Dict[str, Any], None]:
        start = time.perf_counter()
        await asyncio.sleep(0.3)
        ttft = time.perf_counter() - start

        chunks = [
            f"**[{model.upper()} Output]**\n\n",
            f"Analyzing prompt: \"{prompt[:60]}...\"\n\n",
            "1. **Core Thesis**: When comparing LLM providers, raw token velocity and time-to-first-token (TTFT) ",
            "are critical UX performance indicators alongside output reasoning quality.\n",
            "2. **Cost Efficiency**: Smaller instruction-tuned models deliver rapid throughput at pennies per million tokens, ",
            "while 70B+ frontier models excel in nuanced multi-turn instruction following.\n\n",
            "✨ *Comparison stream completed successfully.*"
        ]

        text = ""
        prompt_tokens = max(10, len(prompt.split()) * 2)
        completion_tokens = 0

        for chunk in chunks:
            await asyncio.sleep(0.12)
            text += chunk
            completion_tokens += max(2, len(chunk.split()) * 2)
            elapsed = time.perf_counter() - start
            tps = completion_tokens / max(elapsed, 0.001)

            yield {
                "chunk": chunk,
                "text": text,
                "is_final": False,
                "ttft": round(ttft, 3),
                "latency": round(elapsed, 3),
                "tokens": completion_tokens,
                "tokens_per_sec": round(tps, 1)
            }

        total_time = time.perf_counter() - start
        cost_data = calculate_cost("mock", model, prompt_tokens, completion_tokens, self.config)

        yield {
            "chunk": "",
            "text": text,
            "is_final": True,
            "ttft": round(ttft, 3),
            "latency": round(total_time, 3),
            "tokens": completion_tokens,
            "tokens_per_sec": round(completion_tokens / max(total_time, 0.001), 1),
            "cost": cost_data
        }

    async def generate_stream(self, provider: str, model: str, prompt: str, system_prompt: str = "") -> AsyncGenerator[Dict[str, Any], None]:
        if provider == "mock":
            async for item in self._mock_stream(prompt, model):
                yield item
            return

        prov = self.config.get("providers", {}).get(provider)
        if not prov:
            yield {"chunk": "Unknown provider", "text": "Unknown provider", "is_final": True, "error": True}
            return

        base_url = prov.get("base_url")
        api_key = prov.get("api_key") or "missing-key"
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=httpx.AsyncClient(timeout=60.0))

        start = time.perf_counter()
        ttft = None
        text = ""
        prompt_tokens = max(10, len(prompt.split()) * 2)
        completion_tokens = 0

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt or "You are a concise, helpful assistant."},
                    {"role": "user", "content": prompt}
                ],
                stream=True,
                temperature=0.7
            )

            async for chunk in stream:
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - start

                delta = chunk.choices[0].delta if chunk.choices else None
                c = delta.content if delta and delta.content else ""
                if c:
                    text += c
                    completion_tokens += 1
                    elapsed = now - start
                    yield {
                        "chunk": c,
                        "text": text,
                        "is_final": False,
                        "ttft": round(ttft, 3),
                        "latency": round(elapsed, 3),
                        "tokens": completion_tokens,
                        "tokens_per_sec": round(completion_tokens / max(elapsed, 0.001), 1)
                    }

            total_time = time.perf_counter() - start
            cost_data = calculate_cost(provider, model, prompt_tokens, completion_tokens, self.config)

            yield {
                "chunk": "",
                "text": text,
                "is_final": True,
                "ttft": round(ttft or total_time, 3),
                "latency": round(total_time, 3),
                "tokens": completion_tokens,
                "tokens_per_sec": round(completion_tokens / max(total_time, 0.001), 1),
                "cost": cost_data
            }

        except Exception as e:
            yield {
                "chunk": f"\n\n[Error: {str(e)}]",
                "text": f"Error: {str(e)}",
                "is_final": True,
                "is_error": True,
                "error": str(e),
                "ttft": 0.0,
                "latency": round(time.perf_counter() - start, 3),
                "tokens": 0,
                "tokens_per_sec": 0.0,
                "cost": calculate_cost(provider, model, 0, 0, self.config)
            }
