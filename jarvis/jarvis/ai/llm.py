"""Orchestrates NLU → plan → confirm → execute → verify → speak."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable

from jarvis.ai.llm import LLMRouter
from jarvis.ai.nlu import is_stop_command, normalize_utterance
from jarvis.ai.planner import Plan, Step, TaskPlanner
from jarvis.core.config import AppConfig
from jarvis.core.emergency import GLOBAL_STOP
from jarvis.core.events import EventBus
from jarvis.core.exceptions import Cancelled, ConfirmationRequired
from jarvis.core.state import AssistantState, StateMachine
from jarvis.memory.store import MemoryStore
from jarvis.tools.executor import ToolExecutor

log = logging.getLogger("jarvis.agent")


class Agent:
    def __init__(
        self,
        cfg: AppConfig,
        bus: EventBus,
        state: StateMachine,
        planner: TaskPlanner,
        executor: ToolExecutor,
        memory: MemoryStore,
        llm: LLMRouter,
        speak: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.bus = bus
        self.state = state
        self.planner = planner
        self.executor = executor
        self.memory = memory
        self.llm = llm
        self.speak = speak or (lambda _t: None)
        self._pending_confirm: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self.active_plan: Plan | None = None

    def handle_text(self, text: str, *, source: str = "user") -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        stop_phrases = list(self.cfg.get("wake_word.stop_phrases") or [])
        if is_stop_command(text, stop_phrases):
            GLOBAL_STOP.trigger("voice")
            self.state.set(AssistantState.STOPPED, last_user_text=text, active_task="")
            self.bus.emit("stopped", reason="voice")
            reply = "Stopped."
            self._say(reply)
            return {"ok": True, "stopped": True, "speak": reply}

        GLOBAL_STOP.reset()
        cleaned = normalize_utterance(text)
        self.state.set(AssistantState.THINKING, last_user_text=cleaned, last_error="", active_task=cleaned)
        self.memory.add_message("user", cleaned, meta={"source": source})
        self.bus.emit("user_message", text=cleaned, source=source)

        plan = self.planner.build(cleaned)
        self.active_plan = plan
        self.bus.emit("plan", plan=plan.to_dict())
        self.state.set(AssistantState.PLANNING, active_task=plan.goal)

        try:
            result = self._execute_plan(plan)
        except ConfirmationRequired as exc:
            self._pending_confirm = {
                "id": exc.request_id,
                "tool": exc.tool,
                "args": exc.args,
                "risk": exc.risk,
                "plan": plan,
                "user_text": cleaned,
            }
            self.state.set(AssistantState.AWAITING_CONFIRM)
            message = f"Confirm {exc.tool} ({exc.risk})?"
            self.bus.emit("assistant_message", text=message, pending_confirm=exc.details)
            return {
                "ok": False,
                "needs_confirmation": True,
                "confirmation": exc.details,
                "speak": message,
                "plan": plan.to_dict(),
            }
        except Cancelled:
            reply = "Stopped."
            self._finish(reply, plan)
            return {"ok": False, "stopped": True, "speak": reply, "plan": plan.to_dict()}
        except Exception as exc:
            log.exception("Agent failed")
            self.state.set(AssistantState.ERROR, last_error=str(exc))
            reply = f"Something went wrong: {exc}"
            self._finish(reply, plan)
            return {"ok": False, "error": str(exc), "speak": reply, "plan": plan.to_dict()}

        reply = self._compose_reply(plan, result)
        self._finish(reply, plan)
        return {"ok": result.get("ok", True), "speak": reply, "plan": plan.to_dict(), "result": result}

    def confirm(self, request_id: str, accepted: bool) -> dict:
        pending = self._pending_confirm
        if not pending or pending["id"] != request_id:
            return {"ok": False, "error": "No matching confirmation request."}
        self._pending_confirm = None
        if not accepted:
            self.state.set(AssistantState.IDLE, active_task="")
            reply = "Cancelled."
            self._say(reply)
            self.memory.add_message("assistant", reply, meta={"confirmation": "denied"})
            return {"ok": True, "accepted": False, "speak": reply}
        self.executor.gate.grant_session(pending["tool"], pending["args"])
        # Re-run the original request; granted fingerprint will pass.
        return self.handle_text(pending["user_text"], source="confirm")

    def pending_confirmation(self) -> dict | None:
        if not self._pending_confirm:
            return None
        return {k: v for k, v in self._pending_confirm.items() if k != "plan"}

    def _execute_plan(self, plan: Plan) -> dict:
        self.state.set(AssistantState.EXECUTING, active_task=plan.goal)
        last: dict[str, Any] = {"ok": True}
        retries = int(self.cfg.get("planner.max_retries") or 1)
        i = 0
        while i < len(plan.steps):
            GLOBAL_STOP.throw_if_stopped()
            step = plan.steps[i]
            if step.tool == "control.stop":
                GLOBAL_STOP.trigger("plan")
                step.status = "done"
                raise Cancelled()
            if step.status in {"done", "skipped"}:
                i += 1
                continue
            # Fill default organize path
            if step.tool == "files.organize" and not step.args.get("path"):
                from jarvis.ai.nlu import _default_workspace
                step.args["path"] = _default_workspace()
            step.status = "running"
            self.bus.emit("step", step=step.to_dict(), index=i)
            try:
                tool_result = self.executor.run(step.tool, step.args)
            except ConfirmationRequired:
                step.status = "pending"
                raise
            step.result = tool_result.to_dict()
            last = step.result
            if tool_result.ok:
                verify = {"verified": True}
                if self.cfg.get("planner.verify_actions", True):
                    verify = self.executor.verify(step.tool, step.args, tool_result)
                if verify.get("verified"):
                    step.status = "done"
                else:
                    step.status = "failed"
                    step.error = verify.get("reason") or "verification failed"
            else:
                step.status = "failed"
                step.error = tool_result.error or "tool failed"
            if step.status == "failed":
                self.state.set(AssistantState.ERROR, last_error=step.error)
                if retries > 0:
                    retries -= 1
                    before = len(plan.steps)
                    self.planner.replan(plan, evidence={"last": last, "failed": step.to_dict()})
                    if len(plan.steps) > before:
                        i += 1
                        continue
                break
            i += 1
        ok = all(s.status == "done" for s in plan.steps) if plan.steps else True
        return {"ok": ok, "last": last, "plan": plan.to_dict()}

    def _compose_reply(self, plan: Plan, result: dict) -> str:
        failed = [s for s in plan.steps if s.status == "failed"]
        if failed:
            err = failed[-1].error or "unknown error"
            return f"I couldn't finish that. {err}"
        last = result.get("last") or {}
        if plan.speak and last.get("ok", True):
            extra = _summarize_tool(last)
            if extra and extra not in plan.speak:
                return f"{plan.speak} {extra}".strip()
            return plan.speak
        if last.get("summary"):
            return str(last["summary"])
        if last.get("ok"):
            return "Done."
        return last.get("error") or "Done."

    def _finish(self, reply: str, plan: Plan | None) -> None:
        self.state.set(AssistantState.IDLE, last_assistant_text=reply, active_task="")
        self.memory.add_message("assistant", reply, meta={"plan": plan.to_dict() if plan else {}})
        self.bus.emit("assistant_message", text=reply, plan=plan.to_dict() if plan else None)
        self._say(reply)

    def _say(self, text: str) -> None:
        if not text:
            return
        try:
            self.state.set(AssistantState.SPEAKING)
            self.speak(text)
        except Exception:
            log.exception("TTS failed")
        finally:
            if self.state.status.state is AssistantState.SPEAKING:
                self.state.set(AssistantState.IDLE)


def _summarize_tool(data: dict) -> str:
    if not data:
        return ""
    if data.get("summary"):
        return str(data["summary"])
    if "cpu_percent" in data:
        mem = (data.get("memory") or {}).get("percent")
        gpu = data.get("gpu") or {}
        gpu_txt = "GPU unavailable"
        if gpu.get("available") and gpu.get("devices"):
            d0 = gpu["devices"][0]
            gpu_txt = f"GPU {d0.get('name', '')} {d0.get('utilization_percent')}%"
        return f"CPU {data.get('cpu_percent')}%, memory {mem}%, {gpu_txt}."
    if data.get("url"):
        return f"Opened {data['url']}."
    if data.get("launched"):
        return f"Launched {data['launched']}."
    if data.get("count") is not None and data.get("moved") is not None:
        return f"Organized {data.get('count')} files."
    if data.get("text") and data.get("path") and "ocr" not in str(data.get("tool", "")):
        text = str(data["text"])
        return text[:280] + ("…" if len(text) > 280 else "")
    if data.get("stdout") is not None:
        out = (data.get("stdout") or "").strip() or (data.get("stderr") or "").strip()
        if out:
            return out[:280]
        return f"Exit code {data.get('returncode')}."
    return ""
