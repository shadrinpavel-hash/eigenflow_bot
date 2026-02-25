
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
    "openai/gpt-4o-mini": {"input": 0.00025, "output": 0.00075},
}

MAX_ROUNDS = 30


async def tool_loop(
    prompt: str,
    tools: list[dict],
    model: str,
    max_rounds: int = MAX_ROUNDS,
    chat_history: list[dict] | None = None,
    available_budget: float = 1000000,  # effectively unlimited
) -> Dict[str, Any]:
    logging.info(f"Starting tool loop with model={model}")

    # Model Fallback
    if model == "google/gemini-2.0-flash-001":
        fallback_model = "openai/gpt-4o-mini"
    else:
        fallback_model = model

    # Initialize variables for tracking cost and rounds
    round_number = 0
    total_cost = 0
    results: list[dict] = []
    interrupted = False
    tool_calls_this_round = 0
    session = aiohttp.ClientSession()

    while round_number < max_rounds:
        round_number += 1
        logging.info(f"--- Round {round_number} ---")
        tool_calls_this_round = 0

        try:
            # Build context
            messages = llm.prepare_messages(
                prompt, tools=tools, model=model, chat_history=chat_history
            )

            # Make the LLM call
            logging.info(f"Calling LLM: {model} task_id={runtime['task']['id']}")

            messages_json = json.dumps(messages, indent=2)

            # Calculate prompt tokens cost
            input_tokens = llm.count_tokens(messages_json, model)

            if model in MODEL_PRICING:
                input_cost = (input_tokens / 1000) * MODEL_PRICING[model]["input"]
            else:
                input_cost = 0
                logging.warning(f"Model {model} not found in MODEL_PRICING")

            # Early budget check
            if total_cost + input_cost > available_budget:
                logging.warning(
                    f"Budget exceeded: cost={total_cost + input_cost} budget={available_budget}"
                )
                break  # Exit the loop if the budget is exceeded

            try:
                response = await llm.call_llm(messages=messages, model=model, session=session)
            except ValueError as e:
                if model != fallback_model:
                    logging.warning(f"Fallback: {model} -> {fallback_model} after {e}")
                    logging.warning(f"Original model call failed with ValueError: {e}")
                    logging.info(f"Calling LLM: {fallback_model}")
                    response = await llm.call_llm(messages=messages, model=fallback_model, session=session)

                    # Calculate prompt tokens cost
                    input_tokens = llm.count_tokens(messages_json, fallback_model)

                    if fallback_model in MODEL_PRICING:
                        input_cost = (input_tokens / 1000) * MODEL_PRICING[fallback_model]["input"]
                    else:
                        input_cost = 0
                        logging.warning(f"Model {fallback_model} not found in MODEL_PRICING")

                else:
                    logging.error(f"Fallback failed: both {model} and {fallback_model} failed with ValueError: {e}")
                    raise

            # Log response content
            logging.info(f"LLM Response: {response}")

            # Calculate completion tokens cost
            if not response or "content" not in response:
                logging.warning(f"Empty response from model {model}")
                logging.warning(f"Fallback: {model} -> {fallback_model} after empty response")
                if model != fallback_model:
                    response = await llm.call_llm(messages=messages, model=fallback_model, session=session)
                    # Calculate prompt tokens cost
                    input_tokens = llm.count_tokens(messages_json, fallback_model)

                    if fallback_model in MODEL_PRICING:
                        input_cost = (input_tokens / 1000) * MODEL_PRICING[fallback_model]["input"]
                    else:
                        input_cost = 0
                        logging.warning(f"Model {fallback_model} not found in MODEL_PRICING")



                else:
                    logging.error(f"Fallback failed: both {model} and {fallback_model} returned empty response")
                    raise ValueError(f"Both models {model} and {fallback_model} returned empty responses")
                    

            if "content" in response:
                completion_tokens = llm.count_tokens(response["content"], model)
            else:
                completion_tokens = 0

            if model in MODEL_PRICING:
                completion_cost = (completion_tokens / 1000) * MODEL_PRICING[model]["output"]
            else:
                completion_cost = 0
            # Track the total cost
            total_cost += input_cost + completion_cost


        except CancelledError:
            logging.info("Task was cancelled")
            interrupted = True
            break

        if not response or "content" not in response:
            logging.warning(f"No content. Stopping tool loop.")
            break

        # Parse tool calls
        tool_calls = response.get("tool_calls", [])
        tool_calls_this_round += len(tool_calls)

        if tool_calls_this_round > 5:
            logging.warning("Too many tool calls in one round. Exiting.")
            break

        # Execute tool calls
        tool_results = []
        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            arguments = tool_call.get("arguments", {})

            logging.info(f"Tool call: {tool_name}({arguments})")
            tool = next((t for t in tools if t["name"] == tool_name), None)

            if not tool:
                error = f"Tool {tool_name} not found"
                logging.error(error)
                tool_results.append({"tool_call_id": tool_call["id"], "error": error})
                continue

            try:
                # Execute the tool
                tool_start_time = time.time()
                tool_output = await tool["function"](**arguments)
                tool_duration = time.time() - tool_start_time
                logging.info(f"Tool {tool_name} duration: {tool_duration:.3f}s")

                # Convert tool_output to string if it is not a string already
                if not isinstance(tool_output, str):
                    tool_output = json.dumps(tool_output, indent=2)

                tool_results.append(
                    {
                        "tool_call_id": tool_call["id"],
                        "result": tool_output,
                    }
                )
                logging.info(f"Tool {tool_name} result: {tool_output}")

            except Exception as e:
                error = f"Tool {tool_name} raised an exception: {e}"
                logging.exception(error)
                tool_results.append({"tool_call_id": tool_call["id"], "error": error})

        # Append the results to the overall results
        results.append(
            {
                "role": "assistant",
                "content": response["content"],
                "tool_calls": tool_calls,
            }
        )

        results.append({
            "role": "tool",
            "content": json.dumps(tool_results)
        })

        logging.info(f"--- Round {round_number} complete ---")

        # Check if the tool loop should stop
        if not tool_calls:
            logging.info("No tool calls. Stopping tool loop.")
            break

    await session.close()
    logging.info(f"Finished tool loop after {round_number} rounds")

    return {
        "interrupted": interrupted,
        "cost": total_cost,
        "rounds": round_number,
        "results": results,
    }
