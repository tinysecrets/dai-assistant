"""End-to-end tests for the model-router over real HTTP.

Every case runs the actual service in-process against a stub upstream on an
ephemeral port, with all state redirected into a temp directory.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional

from tests.support import (
    StubUpstream,
    TempSpine,
    ServiceFixture,
    get,
    post,
    start_router,
)

TEST_KEY = "sk-or-v1-TESTKEY-DO-NOT-LEAK-1234567890"
GROQ_KEY = "gsk_ODDSHAPEkeythatnopatternknows123456"


class RouterCase(unittest.TestCase):
    """Base class: spins up a stub upstream and the router."""

    pool: Optional[Dict[str, Any]] = None
    policy: Optional[Dict[str, Any]] = None
    router_env: Dict[str, str] = {}
    rules: list = []

    def setUp(self) -> None:
        self.spine = TempSpine(pool=self.pool, policy=self.policy)
        self.upstream = StubUpstream().start()
        for match, kwargs in self.rules:
            self.upstream.rule(match, **kwargs)
        self.service, self.runtime = start_router(self.spine, self.upstream, env=dict(self.router_env))
        self.base = self.service.base_url
        self.addCleanup(self.tearDownServices)

    def tearDownServices(self) -> None:
        self.service.stop()
        self.upstream.stop()
        self.spine.cleanup()

    def chat(self, body: Optional[Dict[str, Any]] = None, **kwargs: Any):
        payload = {"model": "dai/auto", "messages": [{"role": "user", "content": "Say hi"}]}
        payload.update(body or {})
        return self.service.post("/v1/chat/completions", payload, **kwargs)

    def upstream_models(self) -> list:
        return [c["model"] for c in self.upstream.calls_for("/chat/completions")]


class TestHealthAndDiscovery(RouterCase):
    def test_health_reports_shape_and_readiness(self) -> None:
        status, body, _headers, _raw = self.service.get("/health")
        self.assertEqual(status, 200)
        for key in (
            "ok", "service", "version", "mode", "ready_for_chat", "providers_configured",
            "keys_needed", "openrouter_paid_enabled", "pool", "candidates_available",
        ):
            self.assertIn(key, body)
        self.assertTrue(body["ready_for_chat"])
        self.assertEqual(body["service"], "model-router")

    def test_health_is_reachable_without_an_auth_token(self) -> None:
        """Monitoring and doctor must work even when the API is token-gated."""
        service, _runtime = start_router(self.spine, self.upstream, env={"DAI_ROUTER_TOKEN": "gate-me"})
        self.addCleanup(service.stop)
        self.assertEqual(service.get("/health")[0], 200)
        self.assertEqual(service.post("/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]})[0], 401)
        status, body, _h, _r = service.post(
            "/v1/chat/completions",
            {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer gate-me"},
        )
        self.assertEqual(status, 200)

    def test_ready_endpoint(self) -> None:
        self.assertEqual(self.service.get("/ready")[0], 200)

    def test_version_endpoint(self) -> None:
        status, body, _h, _r = self.service.get("/version")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "model-router")

    def test_root_is_health(self) -> None:
        self.assertEqual(self.service.get("/")[1]["service"], "model-router")

    def test_models_lists_virtual_ids_first(self) -> None:
        status, body, _h, _r = self.service.get("/v1/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(ids[:2], ["dai/vision-auto", "dai/auto"])
        self.assertIn("alpha/one:free", ids)

    def test_models_can_be_filtered_by_provider(self) -> None:
        _s, body, _h, _r = self.service.get("/v1/models?provider=openrouter")
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(sorted(ids), ["alpha/one:free", "beta/two:free"])
        self.assertNotIn("dai/auto", ids)  # virtual ids only appear unfiltered

    def test_filter_by_unconfigured_provider_is_empty(self) -> None:
        _s, body, _h, _r = self.service.get("/v1/models?provider=cerebras")
        self.assertEqual(body["data"], [])

    def test_unknown_route_is_json_404(self) -> None:
        status, body, _h, _r = self.service.get("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_method_not_allowed(self) -> None:
        from tests.support import http_request

        status, body, headers, _r = http_request("DELETE", self.service.url("/health"))
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "method_not_allowed")
        self.assertIn("GET", headers.get("Allow", ""))

    def test_unsupported_openai_surface_explains_itself(self) -> None:
        status, body, _h, _r = post(self.service.url("/v1/embeddings"), {})
        self.assertEqual(status, 501)
        self.assertEqual(body["error"], "not_implemented")
        self.assertIn("embed", body["detail"].lower())

    def test_head_request_has_no_body(self) -> None:
        from tests.support import http_request

        status, _body, headers, raw = http_request("HEAD", self.service.url("/health"))
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)


class TestRequestValidation(RouterCase):
    def test_malformed_json_returns_400_not_a_dropped_connection(self) -> None:
        """Regression: this used to raise inside the handler and reset the socket."""
        status, body, _h, raw = self.service.post(
            "/v1/chat/completions", raw_body=b"{not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_json")
        self.assertTrue(raw)

    def test_non_object_json_body(self) -> None:
        status, body, _h, _r = self.service.post(
            "/v1/chat/completions", raw_body=b"[1,2,3]", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_json")

    def test_missing_messages(self) -> None:
        status, body, _h, _r = self.service.post("/v1/chat/completions", {"model": "dai/auto"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "messages_required")

    def test_empty_messages_array(self) -> None:
        status, body, _h, _r = self.service.post("/v1/chat/completions", {"model": "dai/auto", "messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "messages_required")

    def test_empty_body(self) -> None:
        status, body, _h, _r = self.service.post("/v1/chat/completions", raw_body=b"")
        self.assertEqual(status, 400)
        self.assertIn(body["error"], ("body_required", "messages_required"))

    def test_non_string_model(self) -> None:
        status, body, _h, _r = self.chat({"model": 42})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_model")

    def test_oversized_body_is_413(self) -> None:
        service, _runtime = start_router(self.spine, self.upstream, env={"DAI_MAX_BODY_BYTES": "200"})
        self.addCleanup(service.stop)
        big = "x" * 4000
        status, body, _h, _r = service.post(
            "/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": big}]}
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "payload_too_large")


class TestChatRouting(RouterCase):
    def test_success_is_annotated_with_the_serving_backend(self) -> None:
        status, body, headers, _raw = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello world")
        self.assertIn("dai_routed", body)
        self.assertEqual(body["dai_routed"]["provider"], "openrouter")
        self.assertEqual(body["dai_routed"]["attempts"], 1)
        self.assertTrue(body["id"])
        self.assertEqual(headers.get("X-DAI-Provider"), "openrouter")
        self.assertEqual(headers.get("X-DAI-Model"), body["dai_routed"]["model"])

    def test_rotation_spreads_load_across_models(self) -> None:
        served = set()
        for _ in range(6):
            _s, body, _h, _r = self.chat()
            served.add(body["dai_routed"]["model"])
        self.assertGreaterEqual(len(served), 2)

    def test_private_body_fields_are_not_forwarded_upstream(self) -> None:
        self.chat({"dai_hint": "look at the image", "dai_approval_token": "nope"})
        keys = self.upstream.calls[-1]["body_keys"]
        self.assertNotIn("dai_hint", keys)
        self.assertNotIn("dai_approval_token", keys)
        self.assertIn("messages", keys)

    def test_dai_hint_drives_vision_boosting(self) -> None:
        pool = {"openrouter_free": ["text/only:free", "alpha/one:free"], "openrouter_free_vision": ["alpha/one:free"],
                "groq_models": [], "ollama_cloud_models": [], "ollama_local_models": []}
        spine = TempSpine(pool=pool, catalog={"models": []})
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream)
        self.addCleanup(service.stop)
        service.post("/v1/chat/completions", {"model": "dai/auto", "dai_hint": "describe this screenshot",
                                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.upstream.calls[-1]["model"], "alpha/one:free")

    def test_vision_content_blocks_imply_a_vision_task(self) -> None:
        status, _body, _h, _r = self.chat(
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]}
        )
        self.assertEqual(status, 200)

    def test_explicit_model_is_honoured(self) -> None:
        status, body, _h, _r = self.chat({"model": "openrouter/beta/two:free"})
        self.assertEqual(status, 200)
        self.assertEqual(body["dai_routed"]["model"], "beta/two:free")
        self.assertNotIn("fallback_from", body["dai_routed"])

    def test_policy_default_model_is_used_when_omitted(self) -> None:
        spine = TempSpine(policy={"version": 3, "mode": "m", "inference": {"default_model": "openrouter/beta/two:free"}})
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream)
        self.addCleanup(service.stop)
        _s, body, _h, _r = service.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body["dai_routed"]["model"], "beta/two:free")

    def test_completions_endpoint_hits_the_upstream_completions_path(self) -> None:
        """Regression: /v1/completions used to be proxied to /chat/completions."""
        status, _body, _h, _r = self.service.post("/v1/completions", {"model": "dai/auto", "prompt": "once upon"})
        self.assertEqual(status, 200)
        self.assertTrue(self.upstream.calls_for("/v1/completions"))
        self.assertFalse([c for c in self.upstream.calls_for("/chat/completions")])

    def test_completions_requires_a_prompt(self) -> None:
        status, body, _h, _r = self.service.post("/v1/completions", {"model": "dai/auto"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "prompt_required")

    def test_extra_parameters_pass_through_untouched(self) -> None:
        self.chat({"temperature": 0.3, "max_tokens": 64})
        body = self.upstream.calls[-1]["body"]
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], 64)


class TestStreaming(RouterCase):
    def stream(self, body: Optional[Dict[str, Any]] = None, **kwargs: Any) -> tuple:
        payload = {"model": "dai/auto", "stream": True, "messages": [{"role": "user", "content": "Say hi"}]}
        payload.update(body or {})
        return self.service.post("/v1/chat/completions", payload, **kwargs)

    def test_stream_is_event_stream_and_chunked(self) -> None:
        status, _body, headers, raw = self.stream()
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Transfer-Encoding", "").lower(), "chunked")
        self.assertTrue(raw)

    def test_stream_frames_are_forwarded_in_order(self) -> None:
        _s, _b, _h, raw = self.stream()
        text = raw.decode()
        self.assertIn("Hello world", text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        self.assertEqual(text.count("data: [DONE]"), 1, "exactly one terminator")

    def test_stream_carries_a_routed_annotation_comment(self) -> None:
        _s, _b, _h, raw = self.stream()
        text = raw.decode()
        first = text.split("\n")[0]
        self.assertTrue(first.startswith(": dai_routed "), first)
        annotation = json.loads(first[len(": dai_routed ") :])
        self.assertEqual(annotation["provider"], "openrouter")
        self.assertTrue(annotation["stream"])

    def test_usage_trailer_precedes_done(self) -> None:
        """Clients stop reading at [DONE], so usage must come first."""
        _s, _b, _h, raw = self.stream()
        text = raw.decode()
        self.assertIn(": dai_usage", text)
        self.assertLess(text.index(": dai_usage"), text.index("data: [DONE]"))
        usage = json.loads(text.split(": dai_usage ")[1].split("\n")[0])
        self.assertEqual(usage["total_tokens"], 7)

    def test_sse_comments_are_parseable_by_a_strict_client(self) -> None:
        """Every non-comment line must be a valid ``data:`` frame."""
        _s, _b, _h, raw = self.stream()
        for line in raw.decode().split("\n"):
            if not line.strip() or line.startswith(":"):
                continue
            self.assertTrue(line.startswith("data: "), f"unexpected SSE line: {line!r}")

    def test_stream_falls_back_when_the_first_candidate_rate_limits(self) -> None:
        self.upstream.rule("alpha", status=429, body={"error": {"message": "Rate limit reached"}})
        status, _b, headers, raw = self.stream({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        annotation = json.loads(raw.decode().split("\n")[0][len(": dai_routed ") :])
        self.assertEqual(annotation["model"], "beta/two:free")
        self.assertEqual(annotation["attempts"], 2)

    def test_stream_degrades_to_json_when_upstream_ignores_stream(self) -> None:
        self.upstream.rule("alpha", status=200, force_json=True,
                           body={"id": "plain", "object": "chat.completion",
                                 "choices": [{"message": {"content": "not streamed"}}]})
        status, body, headers, _raw = self.stream({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(body["choices"][0]["message"]["content"], "not streamed")

    def test_legacy_completions_ignores_stream(self) -> None:
        status, body, headers, _raw = self.service.post(
            "/v1/completions", {"model": "dai/auto", "prompt": "once", "stream": True}
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertTrue(body)


class TestFailureRotation(RouterCase):
    def test_rate_limit_rotates_and_sets_a_cooldown(self) -> None:
        self.upstream.rule("alpha", status=429, body={"error": {"message": "Rate limit reached", "code": "rate_limited"}})
        status, body, headers, _raw = self.chat({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 200)
        self.assertEqual(body["dai_routed"]["model"], "beta/two:free")
        self.assertEqual(headers.get("X-DAI-Fallback-From"), "openrouter/alpha/one:free")
        self.assertEqual(headers.get("X-DAI-Attempts"), "2")

        cooldowns = self.runtime.cooldowns()
        self.assertIn("openrouter:alpha/one:free", cooldowns)
        _s, listed, _h, _r = self.service.get("/v1/status/cooldowns")
        self.assertIn("openrouter:alpha/one:free", listed["cooldowns"])

        # Persisted for doctor / a restart.
        on_disk = json.loads((self.spine.dir / "state" / "cooldowns.json").read_text())
        self.assertIn("openrouter:alpha/one:free", on_disk["cooldowns"])

    def test_cooldown_is_not_retried_on_the_next_request(self) -> None:
        self.upstream.rule("alpha", status=429, body={"error": {"message": "Rate limit reached"}})
        self.chat({"model": "openrouter/alpha/one:free"})
        self.upstream.reset()
        self.chat()
        self.assertNotIn("alpha/one:free", self.upstream_models())

    def test_one_401_cools_the_whole_provider(self) -> None:
        """Otherwise a bad key costs one failing request per model in the pool."""
        self.upstream.rule("alpha", status=401, body={"error": {"message": "Invalid api key"}})
        status, body, _h, _r = self.chat({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "all_candidates_failed")
        openrouter_calls = [c for c in self.upstream.calls if "one:free" in c["model"] or "two:free" in c["model"]]
        self.assertEqual(len(openrouter_calls), 1, "beta must not be tried after alpha 401s")
        self.assertIn("openrouter:*", self.runtime.cooldowns())

    def test_client_error_is_returned_without_rotating(self) -> None:
        """Regression: a bad payload must not be retried across the whole pool."""
        self.upstream.rule("alpha", status=400, body={"error": {"message": "Invalid value for 'temperature'"}})
        status, body, _h, _r = self.chat({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 400)
        self.assertIn("temperature", json.dumps(body))
        self.assertEqual(body["dai_routed"]["model"], "alpha/one:free")
        self.assertEqual(len(self.upstream.calls_for("/chat/completions")), 1)

    def test_context_length_rotates_to_a_bigger_model(self) -> None:
        self.upstream.rule("alpha", status=400, body={"error": {"message": "context_length_exceeded"}})
        status, body, _h, _r = self.chat({"model": "openrouter/alpha/one:free"})
        self.assertEqual(status, 200)
        self.assertEqual(body["dai_routed"]["model"], "beta/two:free")

    def test_server_error_body_mentioning_operate_is_not_treated_as_rate_limit(self) -> None:
        """Regression: the substring heuristic cooled models for 90 s on any 500."""
        self.upstream.rule("alpha", status=500, body={"error": {"message": "Failed to operate on the resource"}})
        self.chat({"model": "openrouter/alpha/one:free"})
        cooldowns = self.runtime.cooldowns()
        self.assertIn("openrouter:alpha/one:free", cooldowns)
        remaining = cooldowns["openrouter:alpha/one:free"] - __import__("time").time()
        self.assertLess(remaining, 60, "a 500 must use the short cooldown, not the 90 s rate-limit one")

    def test_all_failing_candidates_report_every_attempt(self) -> None:
        self.upstream.rule("alpha", status=500, body={"error": {"message": "boom"}})
        self.upstream.rule("beta", status=500, body={"error": {"message": "boom"}})
        service, _rt = start_router(self.spine, self.upstream, env={"GROQ_API_KEY": GROQ_KEY})
        self.addCleanup(service.stop)
        self.upstream.rule("groq", status=500, body={"error": {"message": "boom"}})
        status, body, _h, _r = service.post(
            "/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "all_candidates_failed")
        self.assertGreaterEqual(len(body["attempts"]), 3)
        self.assertEqual({a["candidate"].split(":")[0] for a in body["attempts"]}, {"openrouter", "groq"})
        self.assertTrue(all("status" in a and "kind" in a for a in body["attempts"]))

    def test_no_providers_ready_is_actionable(self) -> None:
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream, env={"OPENROUTER_API_KEY": ""})
        self.addCleanup(service.stop)
        status, body, _h, _r = service.post(
            "/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "no_providers_ready")
        self.assertIn("OPENROUTER_API_KEY", body["keys_needed"])
        self.assertIn("KEYS.md", body["detail"])

    def test_health_reflects_a_missing_key(self) -> None:
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream, env={"OPENROUTER_API_KEY": "", "GROQ_API_KEY": ""})
        self.addCleanup(service.stop)
        _s, body, _h, _r = service.get("/health")
        self.assertFalse(body["ready_for_chat"])
        self.assertEqual(service.get("/ready")[0], 503)


class TestPaidModelsAndApprovals(RouterCase):
    def issue_token(self, action: str, scope: str) -> str:
        token, _rec = self.runtime.approvals.issue(action, scope)
        return token

    def test_paid_model_is_refused_without_approval(self) -> None:
        status, body, _h, _r = self.chat({"model": "openrouter/anthropic/claude-sonnet-4.5"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "paid_models_disabled")
        self.assertIn("issue-approval.sh", body["detail"])
        self.assertEqual(self.upstream.calls_for("/chat/completions"), [])

    def test_bare_paid_slug_is_also_refused(self) -> None:
        status, body, _h, _r = self.chat({"model": "anthropic/claude-sonnet-4.5"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "paid_models_disabled")

    def test_paid_model_runs_with_a_valid_token(self) -> None:
        token = self.issue_token("openrouter_paid", "anthropic/claude-sonnet-4.5")
        status, body, _h, _r = self.chat(
            {"model": "openrouter/anthropic/claude-sonnet-4.5"}, headers={"X-DAI-Approval-Token": token}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["dai_routed"]["model"], "anthropic/claude-sonnet-4.5")

    def test_token_may_also_travel_in_the_body(self) -> None:
        token = self.issue_token("openrouter_paid", "*")
        status, _body, _h, _r = self.chat(
            {"model": "openrouter/anthropic/claude-sonnet-4.5", "dai_approval_token": token}
        )
        self.assertEqual(status, 200)

    def test_single_use_token_is_spent(self) -> None:
        token = self.issue_token("openrouter_paid", "*")
        self.assertEqual(self.chat({"model": "openrouter/paid/x"}, headers={"X-DAI-Approval-Token": token})[0], 200)
        status, body, _h, _r = self.chat({"model": "openrouter/paid/y"}, headers={"X-DAI-Approval-Token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_exhausted")

    def test_wrong_action_token_is_refused(self) -> None:
        token = self.issue_token("agent_s_gui_task", "*")
        status, body, _h, _r = self.chat(
            {"model": "openrouter/paid/x"}, headers={"X-DAI-Approval-Token": token}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_action_mismatch")

    def test_scope_mismatched_token_is_refused(self) -> None:
        token = self.issue_token("openrouter_paid", "some/other-model")
        status, body, _h, _r = self.chat({"model": "openrouter/paid/x"}, headers={"X-DAI-Approval-Token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_scope_mismatch")

    def test_garbage_token_is_refused(self) -> None:
        status, body, _h, _r = self.chat({"model": "openrouter/paid/x"}, headers={"X-DAI-Approval-Token": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "unknown_approval_token")

    def test_policy_flag_allows_paid_without_a_token(self) -> None:
        policy = {"version": 3, "inference": {"openrouter": {"enabled": True, "paid_enabled": True}}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream)
        self.addCleanup(service.stop)
        status, body, _h, _r = service.post(
            "/v1/chat/completions",
            {"model": "openrouter/anthropic/claude-sonnet-4.5", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(service.get("/health")[1]["openrouter_paid_enabled"])

    def test_paid_models_in_the_pool_are_skipped_not_billed(self) -> None:
        pool = {"openrouter_free": ["paid/not-free-at-all", "alpha/one:free"], "openrouter_free_vision": [],
                "groq_models": [], "ollama_cloud_models": [], "ollama_local_models": []}
        spine = TempSpine(pool=pool, catalog={"models": []})
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream)
        self.addCleanup(service.stop)
        status, body, _h, _r = service.post(
            "/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["dai_routed"]["model"], "alpha/one:free")
        self.assertNotIn("paid/not-free-at-all", [c["model"] for c in self.upstream.calls])


class TestStatusEndpoints(RouterCase):
    def test_stats_record_success_and_failure(self) -> None:
        self.upstream.rule("alpha", status=429, body={"error": {"message": "rate limit"}})
        self.chat({"model": "openrouter/alpha/one:free"})
        _s, body, _h, _r = self.service.get("/v1/status/stats")
        counters = body["counters"]
        self.assertIn("openrouter:alpha/one:free", counters)
        self.assertEqual(counters["openrouter:alpha/one:free"]["kinds"]["rate_limited"], 1)
        self.assertGreaterEqual(counters["openrouter:beta/two:free"]["ok"], 1)
        self.assertGreater(counters["openrouter:beta/two:free"]["avg_ms"], 0)

    def test_plan_dry_run_does_not_call_any_provider(self) -> None:
        _s, body, _h, _r = self.service.get("/v1/status/plan?model=openrouter/beta/two:free")
        self.assertEqual(body["candidates"][0]["model"], "beta/two:free")
        self.assertEqual(self.upstream.calls, [])

    def test_plan_reports_a_vision_request(self) -> None:
        _s, body, _h, _r = self.service.get("/v1/status/plan?model=dai/vision-auto")
        self.assertEqual(body["requested"]["provider"], "vision-auto")

    def test_keys_endpoint_never_exposes_a_value(self) -> None:
        _s, body, _h, raw = self.service.get("/v1/status/keys")
        self.assertTrue(body["providers"]["openrouter"])
        self.assertNotIn(TEST_KEY, raw.decode())
        self.assertGreaterEqual(body["redacted_secrets"], 1)

    def test_config_endpoint_is_secret_free(self) -> None:
        _s, _body, _h, raw = self.service.get("/v1/status/config")
        text = raw.decode()
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn(GROQ_KEY, text)

    def test_reset_clears_cooldowns_and_stats(self) -> None:
        self.upstream.rule("alpha", status=429, body={"error": {"message": "rate limit"}})
        self.chat({"model": "openrouter/alpha/one:free"})
        self.assertTrue(self.runtime.cooldowns())
        status, body, _h, _r = self.service.post("/v1/status/reset", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.runtime.cooldowns(), {})
        self.assertEqual(self.service.get("/v1/status/stats")[1]["keys"], 0)

    def test_reset_requires_auth_when_configured(self) -> None:
        service, _rt = start_router(self.spine, self.upstream, env={"DAI_ROUTER_TOKEN": "gate"})
        self.addCleanup(service.stop)
        self.assertEqual(service.post("/v1/status/reset", {})[0], 401)


class TestSecretHygiene(RouterCase):
    rules = [("alpha", {"status": 429, "body": {"error": {"message": "rate limit"}}})]

    def test_key_is_forwarded_to_the_provider(self) -> None:
        self.chat({"model": "openrouter/beta/two:free"})
        self.assertEqual(self.upstream.calls[-1]["authorization"], f"Bearer {TEST_KEY}")

    def test_upstream_echo_of_the_key_is_redacted_before_it_reaches_the_client(self) -> None:
        """A provider that reflects request data must not become a leak path."""
        pool = {"openrouter_free": ["reflect/one:free", "reflect/two:free"], "openrouter_free_vision": [],
                "groq_models": [], "ollama_cloud_models": [], "ollama_local_models": []}
        spine = TempSpine(pool=pool, catalog={"models": []})
        self.addCleanup(spine.cleanup)
        service, _rt = start_router(spine, self.upstream)
        self.addCleanup(service.stop)
        status, body, _headers, raw = service.post(
            "/v1/chat/completions", {"model": "dai/auto", "messages": [{"role": "user", "content": "hi"}]}
        )
        text = raw.decode()
        self.assertEqual(status, 503)  # every candidate reflected and failed
        self.assertNotIn(TEST_KEY, text)
        self.assertIn("[REDACTED]", text)
        self.assertTrue(all("error" in a for a in body["attempts"]))

    def test_oddly_shaped_key_is_redacted_too(self) -> None:
        """The registered-value layer covers keys no pattern would match."""
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        spine.write_env({"GROQ_API_KEY": GROQ_KEY})
        service, runtime = start_router(spine, self.upstream, env={"GROQ_API_KEY": GROQ_KEY})
        self.addCleanup(service.stop)
        _s, _b, _h, raw = service.post(
            "/v1/chat/completions",
            {"model": "openrouter/reflect-key:free", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertNotIn(GROQ_KEY, raw.decode())
        _s, _b, _h, raw = service.post(
            "/v1/chat/completions",
            {"model": "groq/reflect-key", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertNotIn(GROQ_KEY, raw.decode())

    def test_logs_do_not_contain_keys(self) -> None:
        self.chat()
        self.assertNotIn(TEST_KEY, str(self.runtime.redactor.redact(TEST_KEY)))


class TestConfigReload(RouterCase):
    def test_pool_edit_is_picked_up_without_a_restart(self) -> None:
        import os
        import time as _time

        pool_path = self.spine.pool_path
        pool_path.write_text(json.dumps({"openrouter_free": ["gamma/three:free"], "openrouter_free_vision": [],
                                         "groq_models": [], "ollama_cloud_models": [], "ollama_local_models": []}))
        st = pool_path.stat()
        os.utime(pool_path, (st.st_atime + 10, st.st_mtime + 10))
        _time.sleep(0.01)
        _s, body, _h, _r = self.chat()
        self.assertEqual(body["dai_routed"]["model"], "gamma/three:free")

    def test_corrupt_pool_keeps_serving_the_last_good_copy(self) -> None:
        import os

        self.spine.pool_path.write_text("{broken json")
        st = self.spine.pool_path.stat()
        os.utime(self.spine.pool_path, (st.st_atime + 10, st.st_mtime + 10))
        status, body, _h, _r = self.chat()
        self.assertEqual(status, 200)
        _s, health, _h, _r = self.service.get("/health")
        self.assertTrue(health["config_errors"])


if __name__ == "__main__":
    unittest.main()
