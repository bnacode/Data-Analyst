"""Session metrics: tokens and tool calls from Strands hooks, plus a count of every app log event.

Tool calls are also logged, so the log shows which agent asked what of which tool."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean

import structlog
from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, HookProvider, HookRegistry
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

log = structlog.get_logger(__name__)

# Logged on every turn or call and already summed up by Turns, Questions, Tokens and Tools
BOOKKEEPING = {"thread_started", "turn_started", "turn_completed", "tool_called", "agent_usage", "findings", "sql_executed"}


def token_cost(tokens: Counter, price: tuple[float, float] | None) -> float | None:
    """USD for these tokens at (input, output) USD per 1M, or None for a model without a price."""
    if price is None:
        return None
    return (tokens["input"] * price[0] + tokens["output"] * price[1]) / 1_000_000


class SessionMetrics(HookProvider):
    """One per CLI session, shared by every agent through hooks=[metrics]."""

    def __init__(self) -> None:
        self.tokens: defaultdict[tuple[str, str], Counter] = defaultdict(Counter)  # (agent, model) -> input, output, model_calls
        self.tool_calls: Counter = Counter()
        self.tool_errors: Counter = Counter()
        self.tool_seconds: Counter = Counter()
        self.events: Counter = Counter()
        self.turn_ms: list[int] = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterInvocationEvent, self.on_invocation)
        registry.add_callback(AfterToolCallEvent, self.on_tool_call)

    def on_invocation(self, event: AfterInvocationEvent) -> None:
        """Strands starts a new AgentInvocation on every call, so this is this call's usage only."""
        invocation = event.agent.event_loop_metrics.agent_invocations[-1]
        usage = {
            "input": invocation.usage["inputTokens"],
            "output": invocation.usage["outputTokens"],
            "model_calls": len(invocation.cycles),
        }
        model = event.agent.model.config["model_id"]
        self.tokens[event.agent.name, model].update(usage)
        log.info("agent_usage", agent=event.agent.name, model=model, **usage)

    def on_tool_call(self, event: AfterToolCallEvent) -> None:
        if isinstance(event.selected_tool, StructuredOutputTool):
            return  # how Strands returns Findings or Report, not a tool the agent chose; logged as findings
        name = event.tool_use["name"]
        failed = bool(event.exception) or event.result["status"] == "error"
        self.tool_calls[name] += 1
        self.tool_seconds[name] += event.duration or 0
        self.tool_errors[name] += failed
        status = "error" if failed else "success"
        # run_sql logs its full SQL itself (sql_executed, sql_rejected, ...)
        details = {} if name == "run_sql" else {"input": str(event.tool_use.get("input"))[:300]}
        log.info("tool_called", agent=event.agent.name, tool=name, status=status, **details)

    def count_event(self, _, __, event_dict: dict) -> dict:
        """structlog processor: counts every event the app logs (sql_rejected, pii_redacted, ...)."""
        self.events[event_dict["event"]] += 1
        return event_dict

    def report(self, prices: dict[str, tuple[float, float]]) -> str:
        """Plain-text summary for /metrics. prices: model -> USD per 1M (input, output) tokens."""
        turns = self.turn_ms
        timing = f" (avg {mean(turns) / 1000:.1f}s, max {max(turns) / 1000:.1f}s)" if turns else ""
        costs = {key: token_cost(t, prices.get(key[1])) for key, t in self.tokens.items()}
        # Every ask_data_analyst call is a question; only those that produced findings were answered
        questions = f"Questions: {self.tool_calls['ask_data_analyst']} asked, {self.events['findings']} answered"
        lines = [f"Turns: {len(turns)}{timing}", questions, "Tokens:"]
        lines += [
            f"  {agent:<15} {model:<24} in {t['input']:>9,}  out {t['output']:>7,}  model calls {t['model_calls']:>3}  "
            + ("no price" if costs[agent, model] is None else f"${costs[agent, model]:.4f}")
            for (agent, model), t in self.tokens.items()
        ]
        lines += [f"Estimated cost: ${sum(c for c in costs.values() if c is not None):.4f}", "Tools:"]
        lines += [
            f"  {name:<18} {calls} calls, {self.tool_errors[name]} errors, {self.tool_seconds[name]:.1f}s"
            for name, calls in self.tool_calls.items()
        ]
        signals = sorted((event, n) for event, n in self.events.items() if event not in BOOKKEEPING)
        lines += ["Events:", *(f"  {event:<24} {n}" for event, n in signals)]
        return "\n".join(lines)
