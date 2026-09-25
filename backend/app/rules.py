"""A deliberately small deterministic rule language; no eval, SQL or LLM code."""

from dataclasses import dataclass, field
from typing import Any


class InvalidRule(ValueError):
    pass


@dataclass
class Fact:
    state: str  # RESOLVED / MISSING / STALE / CONFLICT / NULL
    value: Any = None
    assertion_ids: list[str] = field(default_factory=list)
    reason: str | None = None


OPERATORS = {"eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null"}


def validate_rule(expression: dict, required_facts: list[str]) -> None:
    facts = set(required_facts)
    if len(facts) != len(required_facts) or len(facts) > 32:
        raise InvalidRule("Required facts must be unique and limited to 32")
    nodes = [0]

    def walk(node: dict, depth: int) -> None:
        nodes[0] += 1
        if depth > 8 or nodes[0] > 128 or not isinstance(node, dict):
            raise InvalidRule("Rule exceeds structure limits")
        if "all" in node or "any" in node:
            if len(node) != 1:
                raise InvalidRule("Boolean node has extra fields")
            children = node.get("all", node.get("any"))
            if not isinstance(children, list) or not 1 <= len(children) <= 32:
                raise InvalidRule("Boolean node needs 1–32 children")
            for child in children:
                walk(child, depth + 1)
        elif "not" in node:
            if len(node) != 1:
                raise InvalidRule("NOT node has extra fields")
            walk(node["not"], depth + 1)
        else:
            if set(node) - {"fact", "op", "value"} or node.get("fact") not in facts:
                raise InvalidRule("Unknown fact or condition field")
            if node.get("op") not in OPERATORS:
                raise InvalidRule("Unknown operator")
            if node["op"] == "is_null" and "value" in node:
                raise InvalidRule("is_null takes no value")
            if node["op"] != "is_null" and "value" not in node:
                raise InvalidRule("Comparison requires a value")
            if node["op"] == "in" and (not isinstance(node["value"], list) or len(node["value"]) > 100):
                raise InvalidRule("IN requires a bounded list")

    walk(expression, 0)
    referenced: set[str] = set()

    def collect(node: dict):
        if "fact" in node:
            referenced.add(node["fact"])
        for key in ("all", "any"):
            for child in node.get(key, []):
                collect(child)
        if "not" in node:
            collect(node["not"])

    collect(expression)
    if referenced != facts:
        raise InvalidRule("Required facts must match the expression exactly")


def evaluate(node: dict, facts: dict[str, Fact]) -> tuple[str, dict]:
    if "all" in node:
        children = [evaluate(child, facts) for child in node["all"]]
        states = [state for state, _ in children]
        state = "FALSE" if "FALSE" in states else "CONFLICT" if "CONFLICT" in states else "UNKNOWN" if "UNKNOWN" in states else "TRUE"
        return state, {"all": [trace for _, trace in children], "result": state}
    if "any" in node:
        children = [evaluate(child, facts) for child in node["any"]]
        states = [state for state, _ in children]
        state = "TRUE" if "TRUE" in states else "CONFLICT" if "CONFLICT" in states else "UNKNOWN" if "UNKNOWN" in states else "FALSE"
        return state, {"any": [trace for _, trace in children], "result": state}
    if "not" in node:
        state, trace = evaluate(node["not"], facts)
        result = {"TRUE": "FALSE", "FALSE": "TRUE"}.get(state, state)
        return result, {"not": trace, "result": result}

    fact = facts[node["fact"]]
    trace = {"fact": node["fact"], "op": node["op"], "fact_state": fact.state}
    if fact.state == "CONFLICT":
        return "CONFLICT", {**trace, "result": "CONFLICT"}
    if fact.state in {"MISSING", "STALE"}:
        return "UNKNOWN", {**trace, "result": "UNKNOWN", "reason": fact.reason}
    op, expected, actual = node["op"], node.get("value"), fact.value
    if fact.state == "NULL" and op != "is_null":
        return "UNKNOWN", {**trace, "result": "UNKNOWN", "reason": "EXPLICIT_NULL"}
    try:
        matched = {
            "eq": lambda: actual == expected,
            "ne": lambda: actual != expected,
            "in": lambda: actual in expected,
            "gt": lambda: actual > expected,
            "gte": lambda: actual >= expected,
            "lt": lambda: actual < expected,
            "lte": lambda: actual <= expected,
            "is_null": lambda: fact.state == "NULL",
        }[op]()
    except (TypeError, ValueError):
        return "UNKNOWN", {**trace, "result": "UNKNOWN", "reason": "TYPE_MISMATCH"}
    result = "TRUE" if matched else "FALSE"
    return result, {**trace, "result": result}
