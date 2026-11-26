"""Fog buttons, dropdowns and forms for Hermes' interactive prompts.

Exec approvals, clarify, slash-command confirms, the ``/update`` prompt, the model and choice pickers and the
browser vault's one-time code all render as Fog messages with buttons (a ``card`` frames them like a Discord
embed). Taps come back as ``button`` events; this mixin routes them by the ``data`` prefix, checks who tapped
(refusals are answered privately with a tap alert), resolves through Hermes' gateway helpers, then edits the
card in place to its resolved state and removes the buttons.

``data`` prefixes (shared with Telegram/Discord where Hermes has a convention):
``ea:<choice>:<id>`` approval, ``cl:<id>:<idx|other>`` clarify (``cl:<id>`` dropdown for multi-select),
``sc:<choice>:<id>`` slash confirm, ``up:<y|n>:<id>`` update prompt, ``mp:/mm:/mc:/mb:/mx:<id>`` model picker,
``cp:<id>`` choice picker, ``vc:<id>`` vault button and ``vcf:<id>`` its form.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from gateway.platforms.base import ExecApprovalPrompt, SendResult
from gateway.platforms.helpers import prompt_id_counter

logger = logging.getLogger(__name__)

AMBER, GREEN, RED, GREY, BLUE = "#e0a030", "#2e9d5b", "#d14b4b", "#8a8f98", "#4a7bd0"
_STYLE = {"primary": "primary", "danger": "danger", "success": "success"}
_APPROVAL_STYLE = {"once": "primary", "session": "success", "always": "success", "deny": "danger"}
_APPROVAL_DONE = {"once": "✅ Approved once", "session": "✅ Approved for this session",
                  "always": "✅ Approved permanently", "deny": "❌ Denied"}
_SLASH_DONE = {"once": "✅ Approved once", "always": "✅ Always approved", "cancel": "❌ Cancelled"}
_TOAST_LIMIT = 200
_SELECT_MAX = 25
_MAX_ROWS = 10
UNAUTHORIZED = "You're not allowed to use this bot's controls."
EXPIRED = "This prompt has expired."


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rows(buttons: list, per_row: int = 2) -> list:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _i18n(key: str, default: str, **kwargs: Any) -> str:
    """Hermes' catalog string for ``key`` when it has one, else ``default``."""
    try:
        from agent.i18n import t
        text = t(key, **kwargs)
        if text and text != key:
            return text
    except Exception:
        pass
    return default.format(**kwargs) if kwargs else default


class FogInteractiveMixin:
    """Interactive prompts for :class:`FogAdapter`. Needs ``_client``, ``_send_payload`` and ``_who``."""

    _EA_CMD_BUDGET = 12000  # Fog messages hold 64k characters: show the whole command whenever possible

    def _init_interactive(self) -> None:
        self._prompt_ids = prompt_id_counter()
        self._approvals: dict[str, dict] = {}
        self._clarify_cards: dict[str, dict] = {}
        self._slash_confirms: dict[str, dict] = {}
        self._pickers: dict[str, dict] = {}
        self._vault_cards: dict[str, dict] = {}
        self._update_prompt_id: Optional[str] = None

    def _next_prompt_id(self) -> str:
        return str(next(self._prompt_ids))

    # ------------------------------------------------------------------ helpers

    async def _answer(self, query_id: str, text: Optional[str] = None, *, alert: bool = False,
                      form: Optional[dict] = None) -> None:
        try:
            await self._client.answer_button(query_id, text=_clip(text, _TOAST_LIMIT) if text else None,
                                             alert=alert, form=form)
        except Exception as exc:
            logger.debug("[%s] button answer failed: %s", self.name, exc)

    async def _edit_card(self, chat_id: str, message_id: Optional[str], *, text: Optional[str] = None,
                         card: Optional[dict] = None, buttons: Any = None) -> bool:
        """Edit a prompt message; ``buttons=None`` removes them (pass ``...`` to keep them)."""
        if not message_id:
            return False
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if card is not None:
            body["card"] = card
        if buttons is not ...:
            body["buttons"] = buttons
        try:
            await self._client.edit_message(chat_id, message_id, body)
            return True
        except Exception as exc:
            logger.debug("[%s] prompt edit failed: %s", self.name, exc)
            return False

    def _tap_authorized(self, ev: dict, thread_id: Optional[str] = None) -> bool:
        """The gateway's full admission chain for whoever tapped (allowlists, pairing, admins)."""
        chat = ev.get("chat") or {}
        user_id = str((ev.get("from") or {}).get("id") or "")
        chat_type = "thread" if thread_id else (chat.get("type") or "dm")
        verdict = self._is_sender_authorized(user_id, chat_type=chat_type, chat_id=str(chat.get("id") or ""),
                                             thread_id=thread_id)
        if verdict is None:  # no runner check installed (unit tests, standalone): fall back to the env allowlist
            return self._env_allows(user_id)
        return verdict

    async def _prompt(self, chat_id: str, text: str, *, card: Optional[dict], buttons: list,
                      metadata: Optional[dict], notify: bool = True) -> tuple[SendResult, Optional[dict]]:
        meta = dict(metadata or {})
        if notify:
            meta["notify"] = True
        return await self._send_payload(chat_id, {"text": text, "buttons": buttons, **({"card": card} if card else {})},
                                        metadata=meta)

    # ------------------------------------------------------------------ exec approval

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        pid = self._next_prompt_id()
        buttons = [{"text": label, "data": f"ea:{choice}:{pid}",
                    **({"style": _APPROVAL_STYLE[choice]} if choice in _APPROVAL_STYLE else {})}
                   for label, choice, _style in prompt.actions]
        # Register before sending: a tap can race the send's response.
        state = {"session_key": prompt.session_key, "request_id": prompt.request_id, "chat_id": prompt.chat_id,
                 "message_id": None, "text": prompt.text,
                 "thread_id": (prompt.metadata or {}).get("thread_id")}
        self._approvals[pid] = state
        meta = dict(prompt.metadata or {})
        meta["is_approval_prompt"] = True
        result, _msg = await self._prompt(
            prompt.chat_id, prompt.text, card={"title": "⚠️ Approval needed", "color": AMBER},
            buttons=_rows(buttons), metadata=meta)
        if not result.success:
            self._approvals.pop(pid, None)
            return result
        state["message_id"] = result.message_id
        self._observe_approval_settle(pid, state)
        return result

    def _observe_approval_settle(self, pid: str, state: dict) -> None:
        """Retire the card when its request settles elsewhere (timeout, typed /approve)."""
        if not state.get("request_id"):
            return
        try:
            from tools.approval import observe_gateway_settle
        except Exception:
            return
        loop = asyncio.get_running_loop()

        def settled(reason=None):
            def schedule():
                if self._approvals.get(pid) is state:
                    task = loop.create_task(self._retire_approval(pid, reason))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            loop.call_soon_threadsafe(schedule)

        try:
            observe_gateway_settle(state["session_key"], state["request_id"], settled)
        except Exception:
            logger.debug("[%s] observe_gateway_settle failed", self.name, exc_info=True)

    async def _retire_approval(self, pid: str, reason: Any) -> None:
        state = self._approvals.pop(pid, None)
        if not state:
            return
        footer = "Timed out — the command was not run" if reason == "timeout" else "No longer pending"
        await self._edit_card(state["chat_id"], state["message_id"],
                              card={"title": "⌛ Approval closed", "color": GREY, "footer": footer}, buttons=None)

    async def _on_approval_tap(self, ev: dict, data: str) -> None:
        qid, parts = ev["query_id"], data.split(":", 2)
        if len(parts) != 3:
            return await self._answer(qid)
        choice, pid = parts[1], parts[2]
        state = self._approvals.get(pid)
        if not state:
            return await self._answer(qid, "This approval has already been resolved.")
        if not self._tap_authorized(ev, state.get("thread_id")):
            return await self._answer(qid, UNAUTHORIZED, alert=True)
        user_id = str((ev.get("from") or {}).get("id") or "")
        from gateway.approval_actor import approval_refusal, refusal_text
        if requester := approval_refusal(state["session_key"], user_id, extra=self.config.extra,
                                         request_id=state.get("request_id"), choice=choice):
            return await self._answer(qid, refusal_text(requester), alert=True)
        if self._approvals.pop(pid, None) is None:  # a concurrent tap claimed it first
            return await self._answer(qid, "This approval has already been resolved.")
        try:
            from tools.approval import resolve_gateway_approval_card
            count = resolve_gateway_approval_card(state["session_key"], choice, state.get("request_id"),
                                                  actor_id=user_id, extra=self.config.extra)
        except Exception as exc:
            logger.error("[%s] resolving approval failed: %s", self.name, exc)
            count = 0
        logger.info("[%s] button resolved %d approval(s) (choice=%s)", self.name, count, choice)
        if count:
            label = _APPROVAL_DONE.get(choice, "Resolved")
            card = {"title": label, "color": RED if choice == "deny" else GREEN, "footer": f"by {self._who(ev)}"}
            self.resume_typing_for_chat(state["chat_id"])
        else:
            label = "⌛ Approval expired"
            card = {"title": label, "color": GREY, "footer": "already timed out or resolved elsewhere"}
        await self._answer(qid, label)
        await self._edit_card(state["chat_id"], state["message_id"], card=card, buttons=None)

    # ------------------------------------------------------------------ slash confirm

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str, confirm_id: str,
                                 metadata: Optional[dict] = None) -> SendResult:
        self._slash_confirms[confirm_id] = {"session_key": session_key, "chat_id": chat_id, "message_id": None,
                                            "thread_id": (metadata or {}).get("thread_id")}
        buttons = [[{"text": "Approve once", "data": f"sc:once:{confirm_id}", "style": "primary"},
                    {"text": "Always approve", "data": f"sc:always:{confirm_id}", "style": "success"}],
                   [{"text": "Cancel", "data": f"sc:cancel:{confirm_id}", "style": "danger"}]]
        result, _ = await self._prompt(chat_id, message, card={"title": title or "Confirm", "color": AMBER},
                                       buttons=buttons, metadata=metadata)
        if result.success:
            self._slash_confirms[confirm_id]["message_id"] = result.message_id
        else:
            self._slash_confirms.pop(confirm_id, None)
        return result

    async def _on_slash_confirm_tap(self, ev: dict, data: str) -> None:
        qid, parts = ev["query_id"], data.split(":", 2)
        if len(parts) != 3:
            return await self._answer(qid)
        choice, confirm_id = parts[1], parts[2]
        state = self._slash_confirms.get(confirm_id)
        if not state:
            return await self._answer(qid, "Already resolved.")
        if not self._tap_authorized(ev, state.get("thread_id")):
            return await self._answer(qid, UNAUTHORIZED, alert=True)
        from gateway.approval_actor import refusal_text, slash_confirm_refusal
        user_id = str((ev.get("from") or {}).get("id") or "")
        if requester := slash_confirm_refusal(state["session_key"], confirm_id, user_id, extra=self.config.extra):
            return await self._answer(qid, refusal_text(requester), alert=True)
        if self._slash_confirms.pop(confirm_id, None) is None:
            return await self._answer(qid, "Already resolved.")
        label = _SLASH_DONE.get(choice, "Resolved")
        await self._answer(qid, label)
        await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                              card={"title": label, "color": RED if choice == "cancel" else GREEN,
                                    "footer": f"by {self._who(ev)}"})
        try:
            from tools import slash_confirm
            result_text = await slash_confirm.resolve(state["session_key"], confirm_id, choice)
        except Exception as exc:
            logger.error("[%s] slash confirm failed: %s", self.name, exc, exc_info=True)
            return
        if result_text:
            meta = {"thread_id": state["thread_id"]} if state.get("thread_id") else None
            await self.send(state["chat_id"], result_text, metadata=meta)

    # ------------------------------------------------------------------ clarify

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict] = None) -> SendResult:
        clean = [self._choice_text(c) for c in (choices or [])]
        clean = [c for c in clean if c][:_SELECT_MAX]
        if not clean:  # open-ended: the gateway's text intercept captures the next message
            return await self.send(chat_id, f"❓ {question}", metadata={**(metadata or {}), "notify": True})
        multi = False
        try:
            from tools.clarify_gateway import get_pending
            multi = bool(getattr(get_pending(clarify_id), "multi_select", False))
        except Exception:
            pass
        text = f"❓ {question}\n\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(clean, start=1))
        if multi:
            rows: list = [{"select": {"id": f"cl:{clarify_id}", "placeholder": "Pick one or more", "min": 1,
                                      "max": len(clean),
                                      "options": [{"label": _clip(c, 100), "value": str(i)} for i, c in enumerate(clean)]}}]
        else:
            numbered = [{"text": _clip(f"{i + 1}. {c}", 60), "data": f"cl:{clarify_id}:{i}"} for i, c in enumerate(clean)]
            rows = _rows(numbered, 1 if any(len(c) > 24 for c in clean) else 2)[: _MAX_ROWS - 1]
        rows.append([{"text": "✏️ Other (type an answer)", "data": f"cl:{clarify_id}:other"}])
        state = {"session_key": session_key, "chat_id": chat_id, "message_id": None, "question": question,
                 "choices": clean, "thread_id": (metadata or {}).get("thread_id")}
        self._clarify_cards[clarify_id] = state
        result, _ = await self._prompt(chat_id, text, card={"title": "Question", "color": BLUE}, buttons=rows,
                                       metadata=metadata)
        if result.success:
            state["message_id"] = result.message_id
        else:
            self._clarify_cards.pop(clarify_id, None)
        return result

    @staticmethod
    def _choice_text(choice: Any) -> str:
        if isinstance(choice, str):
            return choice.strip()
        if isinstance(choice, dict):
            for key in ("label", "description", "text", "title"):
                if isinstance(choice.get(key), str) and choice[key].strip():
                    return choice[key].strip()
            return ""
        return str(choice or "").strip()

    async def retire_clarify_card(self, clarify_id: str, notice: Optional[str] = None) -> None:
        state = self._clarify_cards.pop(clarify_id, None)
        if state:
            await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                                  card={"title": "Question closed", "color": GREY, "footer": notice or EXPIRED})

    async def _on_clarify_tap(self, ev: dict, data: str) -> None:
        qid = ev["query_id"]
        _, clarify_id, token = (data.split(":", 2) + [""])[:3]
        state = self._clarify_cards.get(clarify_id)
        if not state:
            return await self._answer(qid, EXPIRED)
        if not self._tap_authorized(ev, state.get("thread_id")):
            return await self._answer(qid, UNAUTHORIZED, alert=True)
        from gateway.approval_actor import clarify_refusal, refusal_text
        user_id = str((ev.get("from") or {}).get("id") or "")
        if requester := clarify_refusal(clarify_id, user_id, extra=self.config.extra):
            return await self._answer(qid, refusal_text(requester), alert=True)
        from tools.clarify_gateway import get_pending, mark_awaiting_text, resolve_gateway_clarify
        who = self._who(ev)
        if token == "other":
            if not mark_awaiting_text(clarify_id):
                self._clarify_cards.pop(clarify_id, None)
                await self._answer(qid, EXPIRED)
                return await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                                             card={"title": "Question closed", "color": GREY, "footer": EXPIRED})
            await self._answer(qid, "Type your answer as a message.")
            return await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                                         card={"title": "Question", "color": BLUE, "footer": f"Waiting for {who} to type an answer"})
        entry = get_pending(clarify_id)
        choices = list(getattr(entry, "choices", None) or state["choices"])
        if token == "" and ev.get("values"):  # multi-select dropdown
            picked = [choices[int(v)] for v in ev["values"] if str(v).isdigit() and int(v) < len(choices)]
            answer = ", ".join(picked)
        else:
            try:
                idx = int(token)
                answer = choices[idx] if 0 <= idx < len(choices) else f"choice {idx + 1}"
            except ValueError:
                return await self._answer(qid, "Invalid choice.")
        if not answer:
            return await self._answer(qid, "Invalid choice.")
        self._clarify_cards.pop(clarify_id, None)
        ok = resolve_gateway_clarify(clarify_id, answer)
        await self._answer(qid, "✓ Sent" if ok else EXPIRED)
        await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                              card={"title": "Answered" if ok else "Question closed", "color": GREEN if ok else GREY,
                                    "footer": f"{who}: {_clip(answer, 120)}" if ok else EXPIRED})

    # ------------------------------------------------------------------ /update prompt

    async def send_update_prompt(self, chat_id: str, prompt: str, default: str = "", session_key: str = "",
                                 metadata: Optional[dict] = None) -> SendResult:
        pid = self._next_prompt_id()
        self._update_prompt_id = pid  # installed before sending: an older card is stale from now on
        hint = f"\n\nDefault: `{default}`" if default else ""
        buttons = [[{"text": "Yes", "data": f"up:y:{pid}", "style": "primary"}, {"text": "No", "data": f"up:n:{pid}"}]]
        result, _ = await self._prompt(chat_id, f"{prompt}{hint}", card={"title": "☤ Hermes update", "color": AMBER},
                                       buttons=buttons, metadata=metadata)
        if result.success:
            self._update_card = (chat_id, result.message_id)
        return result

    async def _on_update_tap(self, ev: dict, data: str) -> None:
        qid = ev["query_id"]
        _, answer, pid = (data.split(":", 2) + ["", ""])[:3]
        if not self._tap_authorized(ev):
            return await self._answer(qid, UNAUTHORIZED, alert=True)
        from gateway.approval_actor import refusal_text, update_prompt_refusal
        if requester := update_prompt_refusal(str((ev.get("from") or {}).get("id") or ""), extra=self.config.extra):
            return await self._answer(qid, refusal_text(requester), alert=True)
        if not self._update_prompt_id or pid != self._update_prompt_id:
            return await self._answer(qid, "Already answered.")
        try:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / ".update_response"
            tmp = path.with_suffix(".tmp")
            tmp.write_text("y" if answer == "y" else "n", encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.error("[%s] writing the update answer failed: %s", self.name, exc)
            return await self._answer(qid, "Couldn't pass the answer on; try again.", alert=True)
        self._update_prompt_id = ""
        word = "Yes" if answer == "y" else "No"
        await self._answer(qid, f"Answered: {word}")
        chat_id, message_id = getattr(self, "_update_card", (str((ev.get("chat") or {}).get("id")), ev.get("message_id")))
        await self._edit_card(chat_id, message_id, buttons=None,
                              card={"title": f"☤ Hermes update — {word}", "color": GREEN, "footer": f"by {self._who(ev)}"})

    # ------------------------------------------------------------------ choice picker

    async def send_choice_picker(self, chat_id: str, title: str, choices: list, session_key: str,
                                 on_choice_selected, metadata: Optional[dict] = None) -> SendResult:
        choices = list(choices)[:_SELECT_MAX]
        if not choices:
            return SendResult(success=False, error="No choices")
        pid = self._next_prompt_id()
        lines = (title or "Choose").splitlines()
        options = [{"label": _clip(str(c.get("label") or c.get("value") or ""), 100), "value": str(c.get("value") or ""),
                    **({"description": "current", "default": True} if c.get("is_current") else {})} for c in choices]
        body = "\n".join(lines[1:]).strip() or lines[0]
        self._pickers[pid] = {"kind": "choice", "chat_id": chat_id, "message_id": None, "session_key": session_key,
                              "callback": on_choice_selected, "title": lines[0]}
        result, _ = await self._prompt(
            chat_id, body, card={"title": f"⚙ {_clip(lines[0], 80)}", "color": BLUE}, metadata=metadata,
            buttons=[{"select": {"id": f"cp:{pid}", "placeholder": "Choose…", "min": 1, "max": 1, "options": options}},
                     [{"text": "Cancel", "data": f"mx:{pid}"}]], notify=False)
        if result.success:
            self._pickers[pid]["message_id"] = result.message_id
        else:
            self._pickers.pop(pid, None)
        return result

    # ------------------------------------------------------------------ model picker

    async def send_model_picker(self, chat_id: str, providers: list, current_model: str, current_provider: str,
                                session_key: str, on_model_selected, metadata: Optional[dict] = None) -> SendResult:
        if not providers:
            return SendResult(success=False, error="No providers")
        pid = self._next_prompt_id()
        state = {"kind": "model", "chat_id": chat_id, "message_id": None, "session_key": session_key,
                 "callback": on_model_selected, "providers": providers, "current_model": current_model,
                 "current_provider": current_provider, "provider": "", "pending": ""}
        self._pickers[pid] = state
        text, buttons = self._provider_view(pid, state)
        result, _ = await self._prompt(chat_id, text, card={"title": "⚙ Model", "color": BLUE}, buttons=buttons,
                                       metadata=metadata, notify=False)
        if result.success:
            state["message_id"] = result.message_id
        else:
            self._pickers.pop(pid, None)
        return result

    @staticmethod
    def _provider_label(slug: str) -> str:
        try:
            from hermes_cli.providers import get_label
            return get_label(slug)
        except Exception:
            return slug

    def _provider_view(self, pid: str, state: dict) -> tuple[str, list]:
        options = []
        for p in state["providers"][:_SELECT_MAX]:
            count = p.get("total_models", len(p.get("models", [])))
            options.append({"label": _clip(f"{p.get('name') or p['slug']} ({count})", 100), "value": str(p["slug"]),
                            **({"description": "current"} if p.get("is_current") else {})})
        text = (f"Current: **{state['current_model'] or 'unknown'}** on "
                f"{self._provider_label(state['current_provider'])}\n\nPick a provider:")
        return text, [{"select": {"id": f"mp:{pid}", "placeholder": "Provider…", "min": 1, "max": 1, "options": options}},
                      [{"text": "Cancel", "data": f"mx:{pid}", "style": "danger"}]]

    def _model_view(self, pid: str, state: dict) -> tuple[str, list]:
        provider = next((p for p in state["providers"] if str(p["slug"]) == state["provider"]), {})
        models = list(provider.get("models", []))
        chunks = [models[i:i + _SELECT_MAX] for i in range(0, len(models), _SELECT_MAX)][: _MAX_ROWS - 1]
        try:
            from hermes_cli.model_picker_usage import picker_row_suffix
        except Exception:
            def picker_row_suffix(_p, _m):
                return ""
        rows: list = []
        for idx, chunk in enumerate(chunks):
            suffix = f" ({idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            rows.append({"select": {"id": f"mm:{pid}", "placeholder": f"Model{suffix}…", "min": 1, "max": 1,
                                    "options": [{"label": _clip(m.split("/")[-1], 100), "value": m,
                                                 **({"description": _clip(s, 100)} if (s := picker_row_suffix(provider, m)) else {})}
                                                for m in chunk]}})
        rows.append([{"text": "Back", "data": f"mb:{pid}"}, {"text": "Cancel", "data": f"mx:{pid}", "style": "danger"}])
        shown = sum(len(c) for c in chunks)
        total = provider.get("total_models", len(models))
        more = f"\n\n_{total - shown} more not shown; use `/model <name>`._" if total > shown else ""
        plan = f"⚠️ {provider['plan_line']}\n\n" if provider.get("plan_line") else ""
        return f"{plan}Pick a model from **{provider.get('name') or state['provider']}**:{more}", rows

    async def _on_picker_tap(self, ev: dict, data: str) -> None:
        qid = ev["query_id"]
        prefix, _, pid = data.partition(":")
        state = self._pickers.get(pid)
        if not state:
            return await self._answer(qid, "This picker has expired; run the command again.")
        if str(ev.get("message_id") or "") not in ("", str(state.get("message_id") or "")):
            return await self._answer(qid, "This picker has expired; run the command again.")
        if not self._tap_authorized(ev):
            return await self._answer(qid, UNAUTHORIZED, alert=True)
        from gateway.approval_actor import picker_refusal, refusal_text
        if requester := picker_refusal(state["callback"], str((ev.get("from") or {}).get("id") or ""),
                                       extra=self.config.extra):
            return await self._answer(qid, refusal_text(requester), alert=True)
        chat_id, message_id = state["chat_id"], state["message_id"]
        value = str((ev.get("values") or [""])[0])
        if prefix == "mx":
            self._pickers.pop(pid, None)
            await self._answer(qid)
            return await self._edit_card(chat_id, message_id, text="Cancelled.", buttons=None,
                                         card={"title": "⚙ " + ("Model" if state["kind"] == "model" else state.get("title", "")),
                                               "color": GREY})
        if prefix == "cp":
            self._pickers.pop(pid, None)
            try:
                result_text = await state["callback"](chat_id, value)
            except Exception as exc:
                logger.error("[%s] choice picker failed: %s", self.name, exc)
                result_text = f"Couldn't apply that: {exc}"
            await self._answer(qid)
            return await self._edit_card(chat_id, message_id, text=result_text or "Done.", buttons=None,
                                         card={"title": f"⚙ {state.get('title', '')}", "color": GREEN})
        if prefix == "mp":
            state["provider"] = value
            text, buttons = self._model_view(pid, state)
            await self._answer(qid)
            return await self._edit_card(chat_id, message_id, text=text, buttons=buttons)
        if prefix == "mb":
            state["provider"] = ""
            text, buttons = self._provider_view(pid, state)
            await self._answer(qid)
            return await self._edit_card(chat_id, message_id, text=text, buttons=buttons,
                                         card={"title": "⚙ Model", "color": BLUE})
        if prefix == "mm":
            try:
                from hermes_cli.model_selection_guards import combined_selection_warning
                warning = await asyncio.to_thread(combined_selection_warning, value, provider=state["provider"])
            except Exception:
                warning = None
            if warning is not None:
                state["pending"] = value
                await self._answer(qid)
                return await self._edit_card(
                    chat_id, message_id, text=warning.message, card={"title": f"⚠ {warning.title}", "color": RED},
                    buttons=[[{"text": "Switch anyway", "data": f"mc:{pid}", "style": "danger"},
                              {"text": "Back", "data": f"mb:{pid}"}]])
            return await self._switch_model(qid, pid, state, value)
        if prefix == "mc":
            if not state.get("pending"):
                return await self._answer(qid, EXPIRED)
            return await self._switch_model(qid, pid, state, state["pending"])
        await self._answer(qid)

    async def _switch_model(self, qid: str, pid: str, state: dict, model_id: str) -> None:
        self._pickers.pop(pid, None)
        await self._answer(qid, f"Switching to {model_id}…")
        try:
            result_text = await state["callback"](state["chat_id"], model_id, state["provider"])
            color = GREEN
        except Exception as exc:
            logger.error("[%s] model switch failed: %s", self.name, exc)
            result_text, color = f"Couldn't switch: {exc}", RED
        await self._edit_card(state["chat_id"], state["message_id"], text=result_text or f"Switched to {model_id}.",
                              buttons=None, card={"title": "⚙ Model", "color": color})

    # ------------------------------------------------------------------ browser vault one-time code

    async def send_vault_code_prompt(self, chat_id: str, site: str, hint: str, *, user_id: str,
                                     submit: Callable[[str], bool], timeout_s: float,
                                     metadata: Optional[dict] = None) -> SendResult:
        """A button that opens a Fog form with a secret field; the code goes straight to ``submit``."""
        pid = self._next_prompt_id()
        body = _i18n("platform.shared.vault_code.body",
                     "{site} is asking for a one-time code (text message or email). Press the button and type it in "
                     "the form that opens: it goes straight into the page and is never posted here.", site=site)
        if hint:
            body += "\n\n" + _clip(hint, 300)
        title = _i18n("platform.shared.vault_code.title", "🔐 Verification code for {site}", site=site)
        state = {"chat_id": chat_id, "message_id": None, "user_id": str(user_id), "submit": submit, "site": site,
                 "title": title}
        self._vault_cards[pid] = state
        result, _ = await self._prompt(
            chat_id, body, card={"title": title, "color": AMBER}, metadata=metadata,
            buttons=[[{"text": _i18n("platform.shared.vault_code.button", "Enter code"), "data": f"vc:{pid}",
                       "style": "primary"}]])
        if not result.success:
            self._vault_cards.pop(pid, None)
            return result
        state["message_id"] = result.message_id
        asyncio.get_running_loop().call_later(
            max(1.0, float(timeout_s)), lambda: asyncio.ensure_future(self._end_vault_card(pid, submitted=False)))
        return result

    async def _end_vault_card(self, pid: str, *, submitted: bool) -> None:
        state = self._vault_cards.pop(pid, None)
        if state:
            footer = (_i18n("platform.shared.vault_code.submitted_footer", "✓ Code submitted") if submitted
                      else _i18n("platform.shared.vault_code.ended", "This code request has ended; nothing was entered."))
            await self._edit_card(state["chat_id"], state["message_id"], buttons=None,
                                  card={"title": state["title"], "color": GREEN if submitted else GREY, "footer": footer})

    async def _on_vault_tap(self, ev: dict, data: str) -> None:
        qid = ev["query_id"]
        prefix, _, pid = data.partition(":")
        state = self._vault_cards.get(pid)
        ended = _i18n("platform.shared.vault_code.ended", "This code request has ended; nothing was entered.")
        if not state:
            return await self._answer(qid, ended)
        user_id = str((ev.get("from") or {}).get("id") or "")
        if user_id != state["user_id"] or not self._tap_authorized(ev):
            return await self._answer(qid, _i18n("platform.shared.vault_code.not_author",
                                                 "Only the person who started this request can enter the code."),
                                      alert=True)
        if prefix == "vc":
            label = _i18n("platform.shared.vault_code.input_label", "One-time code")
            return await self._answer(qid, form={
                "id": f"vcf:{pid}", "title": _clip(_i18n("platform.shared.vault_code.modal_title", "Code for {site}",
                                                         site=state["site"]), 60),
                "fields": [{"id": "code", "label": label, "secret": True, "required": True, "max_length": 32}]})
        code = str((ev.get("fields") or {}).get("code") or "").strip()
        if not code:
            return await self._answer(qid)
        accepted = bool(state["submit"](code))
        code = ""
        await self._answer(qid, _i18n("platform.shared.vault_code.submitted", "Code submitted; entering it into the page.")
                           if accepted else ended)
        await self._end_vault_card(pid, submitted=accepted)

    # ------------------------------------------------------------------ routing

    async def _handle_button(self, ev: dict) -> None:
        data = str(ev.get("data") or "")
        routes = (("ea:", self._on_approval_tap), ("sc:", self._on_slash_confirm_tap), ("cl:", self._on_clarify_tap),
                  ("up:", self._on_update_tap), ("vcf:", self._on_vault_tap), ("vc:", self._on_vault_tap),
                  ("mp:", self._on_picker_tap), ("mm:", self._on_picker_tap), ("mc:", self._on_picker_tap),
                  ("mb:", self._on_picker_tap), ("mx:", self._on_picker_tap), ("cp:", self._on_picker_tap))
        for prefix, handler in routes:
            if data.startswith(prefix):
                try:
                    await handler(ev, data)
                except Exception:
                    logger.exception("[%s] button handler failed (prefix=%s)", self.name, prefix)
                    await self._answer(ev["query_id"], "Something went wrong.")
                return
        logger.debug("[%s] unhandled button data prefix %r", self.name, data[:8])
        await self._answer(ev["query_id"])
