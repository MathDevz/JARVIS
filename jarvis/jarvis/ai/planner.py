"""Task planner: maintains goal, completed/failed/remaining steps, avoids repeats."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jarvis.ai.llm import LLMRouter, extract_json
from jarvis.ai.nlu import heuristic_plan, normalize_utterance
from jarvis.ai.prompts import REPLAN, render_system
from jarvis.core.config import AppConfig
from jarvis.tools.registry import ToolRegistry

log = logging.getLogger("jarvis.planner")


@dataclass
class Step:
    tool: str
    args: dict[str, Any]
    why: str = ""
    status: str = "pending"  # pending | running | done | failed | skipped
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def fingerprint(self) -> str:
        return f"{self.tool}:{sorted(self.args.items())}"

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args": self.args,
            "why": self.why,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class Plan:
    goal: str
    speak: str
    thought: str
    steps: list[Step]
    source: str = "heuristic"

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "speak": self.speak,
            "thought": self.thought,
            "source": self.source,
            "steps": [s.to_dict() for s in self.steps],
            "completed": [s.to_dict() for s in self.steps if s.status == "done"],
            "failed": [s.to_dict() for s in self.steps if s.status == "failed"],
            "remaining": [s.to_dict() for s in self.steps if s.status in {"pending", "running"}],
        }


class TaskPlanner:
    def __init__(self, cfg: AppConfig, llm: LLMRouter, registry: ToolRegistry, memory=None):
        self.cfg = cfg
        self.llm = llm
        self.registry = registry
        self.memory = memory

    def build(self, user_text: str) -> Plan:
        text = normalize_utterance(user_text)
        # Always compute a heuristic plan as a reliable baseline.
        heur = heuristic_plan(text)
        if self.llm.backend.name == "heuristic":
            return self._from_payload(text, heur, source="heuristic")

        messages = [{"role": "system", "content": render_system(self.registry.schemas())}]
        prefs = self.memory.preference_block() if self.memory else ""
        if prefs:
            messages.append({"role": "system", "content": prefs})
        history = self.memory.recent_messages(8) if self.memory else []
        for msg in history:
            if msg["role"] in {"user", "assistant"}:
                messages.append({"role": msg["role"], "content": msg["content"][:2000]})
        messages.append({"role": "user", "content": text})
        try:
            raw = self.llm.complete(messages)
            payload = extract_json(raw)
            if payload.get("steps") or payload.get("speak"):
                return self._from_payload(text, payload, source=self.llm.backend.name)
        except Exception:
            log.exception("LLM planning failed; using heuristic")
        return self._from_payload(text, heur, source="heuristic-fallback")

    def replan(self, plan: Plan, evidence: dict) -> Plan:
        if not self.cfg.get("planner.replan_on_failure", True):
            return plan
        failed = next((s for s in reversed(plan.steps) if s.status == "failed"), None)
        if self.llm.backend.name == "heuristic":
            # Simple recovery recipes
            extra: list[Step] = []
            if failed and failed.tool == "apps.launch":
                extra.append(Step("apps.list", {}, "inspect available apps after failed launch"))
            if failed and failed.tool.startswith("windows."):
                extra.append(Step("windows.list", {}, "inspect windows after failure"))
            if failed and failed.tool.startswith("screen."):
                extra.append(Step("screen.screenshot", {}, "capture screen after visual failure"))
            plan.steps.extend(extra)
            return plan
        completed = [s.to_dict() for s in plan.steps if s.status == "done"]
        prompt = REPLAN.format(
            goal=plan.goal,
            failed=failed.to_dict() if failed else {},
            error=failed.error if failed else "",
            completed=completed,
            evidence=evidence,
        )
        try:
            raw = self.llm.complete([
                {"role": "system", "content": render_system(self.registry.schemas())},
                {"role": "user", "content": prompt},
            ])
            payload = extract_json(raw)
            new_steps = [self._step_from(item) for item in payload.get("steps") or []]
            seen = {s.fingerprint() for s in plan.steps if s.status == "done"}
            for step in new_steps:
                if step.fingerprint() in seen:
                    continue
                plan.steps.append(step)
            if payload.get("speak"):
                plan.speak = payload["speak"]
        except Exception:
            log.exception("Replan failed")
        return plan

    def _from_payload(self, goal: str, payload: dict, source: str) -> Plan:
        steps = [self._step_from(item) for item in payload.get("steps") or []]
        # Drop unknown tools early except control.stop which is handled by agent
        cleaned = []
        seen: set[str] = set()
        max_steps = int(self.cfg.get("planner.max_steps") or 12)
        for step in steps:
            if step.tool != "control.stop" and not self.registry.has(step.tool):
                log.warning("Dropping unknown tool %s", step.tool)
                continue
            fp = step.fingerprint()
            if fp in seen:
                continue
            seen.add(fp)
            cleaned.append(step)
            if len(cleaned) >= max_steps:
                break
        return Plan(
            goal=goal,
            speak=payload.get("speak") or "",
            thought=payload.get("thought") or "",
            steps=cleaned,
            source=source,
        )

    @staticmethod
    def _step_from(item: Any) -> Step:
        if not isinstance(item, dict):
            return Step(tool="invalid", args={}, why="bad step")
        args = item.get("args") or item.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        return Step(tool=str(item.get("tool") or ""), args=args, why=str(item.get("why") or ""))
