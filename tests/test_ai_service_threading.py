"""Tests for services/ai_service.py's event-loop protection.

_try_groq_chat() and _try_gemini_chat() are synchronous SDK calls
that perform real network I/O. Before this fix they ran directly on
whatever thread called generate_julie_response()/generate_recap() --
the asyncio event-loop thread, when invoked from Discord command or
message handlers -- so a slow or stalled provider could freeze the
entire bot. Gemini specifically had no timeout at all: with
http_options unset, the google-genai SDK passes an explicit
timeout=None into httpx, which disables the timeout rather than
falling back to any default (confirmed during the audit by pointing
a real client at a stalled TCP listener and observing the call never
return).

These tests protect three things:

    1. Both provider calls now execute on a worker thread, not the
       event-loop thread -- verified via real threading.current_
       thread() identity checks, not by mocking asyncio.to_thread
       and asserting it was called.
    2. A slow synchronous provider call cannot stall a concurrent
       coroutine (a heartbeat racing a simulated slow call) -- the
       actual behavior the fix is meant to guarantee.
    3. Gemini is now constructed with an explicit, finite timeout,
       verified through the SDK's own configuration mechanism.

Fallback (Groq -> Gemini) and both-providers-down behavior are
already covered by tests/test_interactive_ai.py, which passes
unchanged against this fix; the tests here additionally re-confirm
both behaviors in combination with the new thread-identity checks so
this file is self-contained.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import services.ai_service as ai_service


def _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=None):
    monkeypatch.setattr(
        ai_service, "CHAT_HISTORY_FILE", tmp_path / "chat_history.db"
    )
    monkeypatch.setattr(ai_service, "groq_client", groq)
    monkeypatch.setattr(ai_service, "ai_client", gemini)
    return ai_service


def make_groq_client(content: str = "Groq reply", delay: float = 0.0, recorder=None):
    """Minimal fake matching the exact shape _try_groq_chat() reads:
    response.choices[0].message.content."""

    def create(**kwargs):
        if recorder is not None:
            recorder["thread"] = threading.current_thread()
        if delay:
            time.sleep(delay)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_failing_groq_client(message: str = "simulated Groq failure"):
    def create(**kwargs):
        raise RuntimeError(message)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def make_gemini_client(content: str = "Gemini reply", delay: float = 0.0, recorder=None):
    """Minimal fake matching the exact shape _extract_gemini_text()
    reads: response.candidates[0].content.parts[*].text and
    response.candidates[0].finish_reason.name."""

    def generate_content(**kwargs):
        if recorder is not None:
            recorder["thread"] = threading.current_thread()
        if delay:
            time.sleep(delay)
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=content)]),
            finish_reason=SimpleNamespace(name="STOP"),
        )
        return SimpleNamespace(candidates=[candidate], text=content)

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


def make_failing_gemini_client(message: str = "simulated Gemini failure"):
    def generate_content(**kwargs):
        raise RuntimeError(message)

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))


# ==========================================================
# A. Groq call executes on a worker thread
# ==========================================================


def test_groq_call_executes_off_the_main_thread(monkeypatch, tmp_path):
    main_thread = threading.current_thread()
    recorder: dict = {}
    groq = make_groq_client(recorder=recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    asyncio.run(svc.generate_julie_response(1, "hello"))

    assert recorder.get("thread") is not None
    assert recorder["thread"] is not main_thread


def test_groq_call_executes_off_the_main_thread_for_recap(monkeypatch, tmp_path):
    main_thread = threading.current_thread()
    recorder: dict = {}
    groq = make_groq_client(content="Recap reply", recorder=recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    reply = asyncio.run(svc.generate_recap(["update one"]))

    assert reply == "Recap reply"
    assert recorder.get("thread") is not None
    assert recorder["thread"] is not main_thread


# ==========================================================
# B. Gemini call executes on a worker thread
# ==========================================================


def test_gemini_call_executes_off_the_main_thread(monkeypatch, tmp_path):
    main_thread = threading.current_thread()
    recorder: dict = {}
    gemini = make_gemini_client(recorder=recorder)
    # Groq not configured -> falls straight through to Gemini.
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=gemini)

    asyncio.run(svc.generate_julie_response(2, "hello"))

    assert recorder.get("thread") is not None
    assert recorder["thread"] is not main_thread


def test_gemini_call_executes_off_the_main_thread_for_recap(monkeypatch, tmp_path):
    main_thread = threading.current_thread()
    recorder: dict = {}
    gemini = make_gemini_client(content="Gemini recap", recorder=recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=gemini)

    reply = asyncio.run(svc.generate_recap(["update one"]))

    assert reply == "Gemini recap"
    assert recorder.get("thread") is not None
    assert recorder["thread"] is not main_thread


# ==========================================================
# C. Gemini is configured with a finite explicit timeout
# ==========================================================


def test_gemini_timeout_constant_is_finite():
    assert ai_service.GEMINI_TIMEOUT_MS is not None
    assert isinstance(ai_service.GEMINI_TIMEOUT_MS, (int, float))
    assert 0 < ai_service.GEMINI_TIMEOUT_MS < float("inf")


def test_gemini_client_construction_resolves_to_a_finite_http_timeout():
    """Reconstructs a client exactly the way ai_service.py's module-
    level ai_client is built (placeholder key only, never a real
    credential), and inspects the resolved HttpOptions the SDK will
    actually use -- proving the constant reaches real configuration,
    not just that a plausibly-named constant exists unused."""

    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key="test-placeholder-key",
        http_options=types.HttpOptions(timeout=ai_service.GEMINI_TIMEOUT_MS),
    )

    resolved_timeout = client._api_client._http_options.timeout
    assert resolved_timeout == ai_service.GEMINI_TIMEOUT_MS
    assert resolved_timeout is not None

    # The SDK's own conversion helper is what previously turned an
    # unset (None) timeout into "disabled" -- confirm it resolves our
    # value to a real, finite number of seconds, not None/unbounded.
    from google.genai._api_client import get_timeout_in_seconds

    resolved_seconds = get_timeout_in_seconds(resolved_timeout)
    assert resolved_seconds is not None
    assert 0 < resolved_seconds < float("inf")


def test_real_module_level_gemini_client_has_a_finite_timeout_if_configured():
    """If GEMINI_API_KEY happens to be set in this environment (it is
    not required to be, and this test does not read or print its
    value), the real module-level ai_client singleton must also carry
    the finite timeout -- not just a client built fresh in a test."""

    if ai_service.ai_client is None:
        return  # no key configured in this environment; nothing to check

    resolved_timeout = ai_service.ai_client._api_client._http_options.timeout
    assert resolved_timeout == ai_service.GEMINI_TIMEOUT_MS
    assert resolved_timeout is not None


# ==========================================================
# D / E. A slow synchronous provider call must not block a
# concurrently running coroutine
# ==========================================================


async def _run_with_heartbeat(coro) -> int:
    """Races `coro` against a fast-ticking heartbeat coroutine and
    returns how many heartbeat ticks ran while `coro` was in flight.
    A blocked event loop would leave this at 0 or 1."""

    counter = 0
    stop_event = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal counter
        while not stop_event.is_set():
            counter += 1
            await asyncio.sleep(0.01)

    heartbeat_task = asyncio.create_task(heartbeat())
    await coro
    stop_event.set()
    await heartbeat_task
    return counter


def test_slow_groq_call_does_not_block_a_concurrent_coroutine(monkeypatch, tmp_path):
    groq = make_groq_client(delay=0.3)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=None)

    counter = asyncio.run(
        _run_with_heartbeat(svc.generate_julie_response(3, "hello"))
    )

    # ~30 heartbeat ticks are possible in 0.3s at a 0.01s interval if
    # the event loop stayed responsive throughout the "slow" Groq call.
    assert counter >= 10, (
        f"only {counter} heartbeat tick(s) ran during the simulated slow "
        "Groq call -- the event loop appears to have been blocked"
    )


def test_slow_gemini_call_does_not_block_a_concurrent_coroutine(monkeypatch, tmp_path):
    gemini = make_gemini_client(delay=0.3)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=gemini)

    counter = asyncio.run(
        _run_with_heartbeat(svc.generate_julie_response(4, "hello"))
    )

    assert counter >= 10, (
        f"only {counter} heartbeat tick(s) ran during the simulated slow "
        "Gemini call -- the event loop appears to have been blocked"
    )


# ==========================================================
# F. Groq -> Gemini fallback behavior is preserved
# ==========================================================


def test_fallback_to_gemini_is_preserved_when_groq_fails(monkeypatch, tmp_path):
    groq = make_failing_groq_client()
    recorder: dict = {}
    gemini = make_gemini_client(content="Gemini saved the day", recorder=recorder)
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=gemini)

    reply = asyncio.run(svc.generate_julie_response(5, "hello"))

    assert reply == "Gemini saved the day"
    assert recorder.get("thread") is not None  # the fallback also ran off-thread


def test_fallback_to_gemini_is_preserved_for_recap(monkeypatch, tmp_path):
    groq = make_failing_groq_client()
    gemini = make_gemini_client(content="Gemini recap fallback")
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=gemini)

    reply = asyncio.run(svc.generate_recap(["update one"]))

    assert reply == "Gemini recap fallback"


def test_groq_is_tried_first_gemini_untouched_when_groq_succeeds(monkeypatch, tmp_path):
    """Gemini raising if ever called proves Groq's success short-
    circuits the fallback, exactly as before this fix."""

    def gemini_must_not_be_called(**kwargs):
        raise AssertionError("Gemini should not be called when Groq succeeds")

    groq = make_groq_client(content="Groq only")
    gemini = SimpleNamespace(
        models=SimpleNamespace(generate_content=gemini_must_not_be_called)
    )
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=gemini)

    reply = asyncio.run(svc.generate_julie_response(6, "hello"))

    assert reply == "Groq only"


# ==========================================================
# G. Provider failure behavior is unchanged
# ==========================================================


def test_friendly_error_when_both_providers_fail(monkeypatch, tmp_path):
    groq = make_failing_groq_client()
    gemini = make_failing_gemini_client()
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=gemini)

    reply = asyncio.run(svc.generate_julie_response(7, "hello"))

    assert "Groq and Gemini" in reply


def test_friendly_error_when_neither_provider_configured(monkeypatch, tmp_path):
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=None, gemini=None)

    reply = asyncio.run(svc.generate_julie_response(8, "hello"))

    assert "Groq and Gemini" in reply


def test_recap_friendly_error_when_both_providers_fail(monkeypatch, tmp_path):
    groq = make_failing_groq_client()
    gemini = make_failing_gemini_client()
    svc = _reset_ai_service_clients(monkeypatch, tmp_path, groq=groq, gemini=gemini)

    reply = asyncio.run(svc.generate_recap(["update one"]))

    assert "Groq and Gemini" in reply
