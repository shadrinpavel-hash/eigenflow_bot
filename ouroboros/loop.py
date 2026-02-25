
import asyncio
import json
import logging
import time
from concurrent.futures import CancelledError
from typing import Any, Coroutine, Dict, List

import aiohttp

from ouroboros import llm

MODEL_PRICING = {
    "openai/gpt-4-turbo-preview": {"input": 0.01, "output": 0.03},
    "openai/gpt-4o": {"input": 0.005, "output": 0.015},
    "openai/gpt-3.5-turbo-0125": {"input": 0.0005, "output": 0.0015},
    "google/gemini-1.5-pro-0514": {"input": 0.007, "output": 0.021},
    "google/gemini-1.5-flash-0514": {"input": 0.00075, "output": 0.00225},
    "anthropic/claude-3-opus-20240229": {"input": 0.015, "output": 0.045},
    "anthropic/claude-3-sonnet-20240229": {"input": 0.003, "output": 0.009},
    "mistralai/mistral-large-latest": {"input": 0.008, "output": 0.024},
    "mistralai/mistral-medium-latest": {"input": 0.0025, "output": 0.0075},
    "mistralai/mistral-small-latest": {"input": 0.0007, "output": 0.0021},
    "google/gemini-2.0-pro-004": {"input": 0.002, "output": 0.006},
    "google/gemini-2.0-flash-001": {"input": 0.00025, "output": 0.00075},
    "google/gemini-2.5-flash-lite": {"input": 0.0001, "output": 0.0004},
    "openai/gpt-4o-mini": {"input": 0.00025, "output": 0.00075},
}

MAX_ROUNDS = 30


def _calc_cost(tokens: int, model: str, token_type: str) -> float:
    """Calculate cost for given tokens. token_type: 'input' or 'output'."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        logging.warning(f"Model {model} not found in MODEL_PRICING")
        return 0.0
    return (tokens / 1000) * pricing[token_type]


async def tool_loop(
    prompt: str,
    tools: list[dict],
    model: str,
    max_rounds: int = MAX_ROUNDS,
    chat_history: list[dict] | None = None,
    messages: list[dict] | None = None,
    available_budget: float = 1000000,  # effectively unlimited
) -> Dict[str, Any]:
    logging.info(f"Starting tool loop with model={model}")

    # Backward-compat: older callers pass messages=
    if messages and (prompt is None or prompt == ""):
        try:
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                prompt = messages[last_user_idx].get("content")
                if chat_history is None:
                    chat_history = messages[:last_user_idx]
        except Exception:
            pass

    fallback_model = "openai/gpt-4o-mini" if model in (
        "google/gemini-2.0-flash-001", "google/gemini-2.5-flash-lite"
    ) else model

    round_number = 0
    total_cost = 0.0
    results: list[dict] = []
    interrupted = False
    session = aiohttp.ClientSession()

    while round_number < max_rounds:
        round_number += 1
        logging.info(f"--- Round {round_number} ---")

        try:
            messages = llm.prepare_messages(
                prompt, tools=tools, model=model, chat_history=chat_history
            )
            messages_json = json.dumps(messages, indent=2)
            input_tokens = llm.count_tokens(messages_json, model)
            input_cost = _calc_cost(input_tokens, model, "input")

            if total_cost + input_cost > available_budget:
                logging.warning(f"Budget exceeded: cost={total_cost + input_cost:.4f}")
                break

            try:
                response = await llm.call_llm(messages=messages, model=model, session=session)
            except ValueError as e:
                if model != fallback_model:
                    logging.warning(f"Fallback: {model} -> {fallback_model} after {e}")
                    response = await llm.call_llm(messages=messages, model=fallback_model, session=session)
                    input_cost = _calc_cost(llm.count_tokens(messages_json, fallback_model), fallback_model, "input")
                else:
                    raise

            if not response or "content" not in response:
                logging.warning(f"Empty response, trying fallback {fallback_model}")
                if model != fallback_model:
                    response = await llm.call_llm(messages=messages, model=fallback_model, session=session)
                    input_cost = _calc_cost(llm.count_tokens(messages_json, fallback_model), fallback_model, "input")
                else:
                    raise ValueError(f"Both {model} and {fallback_model} returned empty responses")

            completion_tokens = llm.count_tokens(response.get("content", ""), model)
            total_cost += input_cost + _calc_cost(completion_tokens, model, "output")

        except CancelledError:
            logging.info("Task was cancelled")
            interrupted = True
            break

        if not response or "content" not in response:
            break

        tool_calls = response.get("tool_calls", [])
        if len(tool_calls) > 5:
            logging.warning("Too many tool calls in one round. Exiting.")
            break

        tool_results = []
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            arguments = tool_call.get("arguments", {})
            tool = next((t for t in tools if t["name"] == tool_name), None)

            if not tool:
                tool_results.append({"tool_call_id": tool_call["id"], "error": f"Tool {tool_name} not found"})
                continue

            try:
                t0 = time.time()
                tool_output = await tool["function"](**arguments)
                logging.info(f"Tool {tool_name} done in {time.time()-t0:.3f}s")
                if not isinstance(tool_output, str):
                    tool_output = json.dumps(tool_output, indent=2)
                tool_results.append({"tool_call_id": tool_call["id"], "result": tool_output})
            except Exception as e:
                logging.exception(f"Tool {tool_name} error")
                tool_results.append({"tool_call_id": tool_call["id"], "error": str(e)})

        results.append({"role": "assistant", "content": response["content"], "tool_calls": tool_calls})
        results.append({"role": "tool", "content": json.dumps(tool_results)})

        if not tool_calls:
            break

    await session.close()
    logging.info(f"Finished tool loop after {round_number} rounds")
    return {"interrupted": interrupted, "cost": total_cost, "rounds": round_number, "results": results}


# Compatibility alias expected by agent.py
run_llm_loop = tool_loop
