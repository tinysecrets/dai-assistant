"""Rotation planning and failure classification (the router's decision core)."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from lib.dai.routing import (
    KIND_AUTH,
    KIND_CLIENT_ERROR,
    KIND_CONTEXT_LENGTH,
    KIND_MODEL_UNAVAILABLE,
    KIND_NETWORK,
    KIND_OK,
    KIND_QUOTA,
    KIND_RATE_LIMITED,
    KIND_SERVER_ERROR,
    KIND_TIMEOUT,
    KIND_UNSUPPORTED_INPUT,
    PROVIDER_ORDER,
    error_text,
    is_vision_request,
    plan_candidates,
    provider_status,
    resolve_explicit,
)
from tests.support import default_catalog, default_pool

KEY_ENV = {"OPENROUTER_API_KEY": "sk-or-v1-test", "GROQ_API_KEY": "gsk_test"}
NOW = 1_700_000_000.0


def plan(
    *,
    env: Optional[Dict[str, str]] = None,
    pool: Optional[Dict[str, Any]] = None,
    catalog: Optional[Dict[str, Any]] = None,
    cooldowns: Optional[Dict[str, float]] = None,
    rr: int = 0,
    hint: str = "",
    preferred: Optional[str] = None,
    strict: bool = False,
    enabled: Optional[List[str]] = None,
) -> Any:
    return plan_candidates(
        pool=pool if pool is not None else default_pool(),
        catalog=catalog if catalog is not None else default_catalog(),
        env=env if env is not None else KEY_ENV,
        cooldowns=cooldowns or {},
        now=NOW,
        rr_index=rr,
        task_hint=hint,
        preferred=preferred,
        strict_models=strict,
        enabled_providers=enabled,
    )


def models(result: Any) -> List[str]:
    return [f"{c.provider}/{c.model}" for c in result.candidates]


class TestResolveExplicit(unittest.TestCase):
    def test_auto_ids_resolve_to_none(self) -> None:
        for value in ("dai/auto", "auto", "router/auto", None, ""):
            self.assertIsNone(resolve_explicit(value), value)

    def test_vision_marker(self) -> None:
        self.assertEqual(resolve_explicit("dai/vision-auto"), {"provider": "vision-auto", "model": "dai/vision-auto"})

    def test_provider_prefixes(self) -> None:
        cases = {
            "groq/llama-3.3-70b-versatile": ("groq", "llama-3.3-70b-versatile"),
            "ollama-cloud/gpt-oss:120b": ("ollama_cloud", "gpt-oss:120b"),
            "ollama/hermes3:8b": ("ollama_local", "hermes3:8b"),
            "local/hermes3:8b": ("ollama_local", "hermes3:8b"),
            "cerebras/llama-3.3-70b": ("cerebras", "llama-3.3-70b"),
            "openrouter/z-ai/glm-5.2:free": ("openrouter", "z-ai/glm-5.2:free"),
        }
        for requested, (provider, model) in cases.items():
            with self.subTest(requested=requested):
                self.assertEqual(resolve_explicit(requested), {"provider": provider, "model": model})

    def test_bare_ids(self) -> None:
        # An Ollama-style tag is local; a slug goes to OpenRouter.
        self.assertEqual(resolve_explicit("hermes3:8b"), {"provider": "ollama_local", "model": "hermes3:8b"})
        self.assertEqual(
            resolve_explicit("z-ai/glm-5.2:free"), {"provider": "openrouter", "model": "z-ai/glm-5.2:free"}
        )
        self.assertEqual(
            resolve_explicit("anthropic/claude-sonnet-4.5"),
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"},
        )


class TestCandidateBuilding(unittest.TestCase):
    def test_no_keys_leaves_only_local(self) -> None:
        result = plan(env={}, pool={**default_pool(), "ollama_local_models": ["hermes3:8b"]})
        self.assertEqual(models(result), ["ollama_local/hermes3:8b"])
        self.assertTrue(any("no providers" in n or n for n in result.notes) or result.candidates)

    def test_no_keys_and_no_local_models_is_empty_with_a_note(self) -> None:
        result = plan(env={})
        self.assertEqual(result.candidates, [])
        self.assertTrue(result.notes)

    def test_cloud_pools_come_before_local(self) -> None:
        env = {**KEY_ENV, "OLLAMA_API_KEY": "oll"}
        pool = {**default_pool(), "ollama_cloud_models": ["cloud-model"], "ollama_local_models": ["hermes3:8b"]}
        result = plan(env=env, pool=pool)
        order = [c.provider for c in result.candidates]
        self.assertEqual(order[0], "openrouter")
        self.assertEqual(order[-1], "ollama_local")
        self.assertEqual(order, sorted(order, key=PROVIDER_ORDER.index))

    def test_local_defaults_when_pool_key_is_absent(self) -> None:
        pool = dict(default_pool())
        pool.pop("ollama_local_models")
        result = plan(env={}, pool=pool)
        self.assertEqual(models(result), ["ollama_local/llama3.2:3b"])

    def test_explicit_empty_local_list_means_no_local_models(self) -> None:
        """An operator who empties the list means it; do not inject defaults."""
        result = plan(env={}, pool={**default_pool(), "ollama_local_models": []})
        self.assertEqual(result.candidates, [])

    def test_round_robin_rotates_the_pool(self) -> None:
        first = models(plan(rr=0))
        second = models(plan(rr=1))
        self.assertEqual(sorted(first), sorted(second))
        self.assertNotEqual(first, second)

    def test_round_robin_wraps_within_a_provider_group(self) -> None:
        """Rotation is per provider group, so the wrap point is the group size."""
        group = [c for c in plan(rr=0).candidates if c.provider == "openrouter"]
        self.assertEqual(models(plan(rr=0)), models(plan(rr=len(group))))
        self.assertEqual(models(plan(rr=1)), models(plan(rr=len(group) + 1)))

    def test_group_order_is_preserved_while_members_rotate(self) -> None:
        """Cloud preference must survive round-robin: OpenRouter stays ahead of Groq."""
        for rr in range(5):
            with self.subTest(rr=rr):
                providers = [c.provider for c in plan(rr=rr).candidates]
                self.assertEqual(providers, sorted(providers, key=PROVIDER_ORDER.index))

    def test_candidates_are_deduplicated(self) -> None:
        pool = {**default_pool(), "openrouter_free": ["alpha/one:free", "alpha/one:free", "beta/two:free"]}
        ids = models(plan(pool=pool))
        self.assertEqual(len(ids), len(set(ids)))

    def test_explicit_model_is_pinned_first_across_rotation(self) -> None:
        """Regression: round-robin used to rotate the requested model away."""
        for rr in range(6):
            with self.subTest(rr=rr):
                result = plan(rr=rr, preferred="openrouter/beta/two:free")
                self.assertEqual(models(result)[0], "openrouter/beta/two:free")
                self.assertIsNone(result.fallback_from)
                self.assertEqual(result.requested, {"provider": "openrouter", "model": "beta/two:free"})

    def test_explicit_model_without_a_key_falls_back_and_says_so(self) -> None:
        result = plan(
            env={}, preferred="groq/groq-fast", pool={**default_pool(), "ollama_local_models": ["hermes3:8b"]}
        )
        self.assertEqual(models(result), ["ollama_local/hermes3:8b"])
        self.assertEqual(result.fallback_from, "groq/groq-fast")
        self.assertTrue(any("groq" in n for n in result.notes))

    def test_strict_mode_refuses_to_substitute(self) -> None:
        result = plan(env={}, preferred="groq/groq-fast", strict=True)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.requested, {"provider": "groq", "model": "groq-fast"})

    def test_explicit_model_on_a_cooled_provider_is_skipped(self) -> None:
        result = plan(
            preferred="openrouter/alpha/one:free",
            cooldowns={"openrouter:alpha/one:free": NOW + 30},
            pool={**default_pool(), "ollama_local_models": ["hermes3:8b"]},
            env={**KEY_ENV, "OLLAMA_API_KEY": ""},
        )
        self.assertNotIn("openrouter/alpha/one:free", models(result))
        self.assertEqual(result.fallback_from, "openrouter/alpha/one:free")


class TestCooldowns(unittest.TestCase):
    def test_model_cooldown_removes_only_that_model(self) -> None:
        result = plan(cooldowns={"openrouter:alpha/one:free": NOW + 30})
        self.assertNotIn("openrouter/alpha/one:free", models(result))
        self.assertIn("openrouter/beta/two:free", models(result))

    def test_expired_cooldown_is_ignored(self) -> None:
        result = plan(cooldowns={"openrouter:alpha/one:free": NOW - 1})
        self.assertIn("openrouter/alpha/one:free", models(result))

    def test_provider_wildcard_cooldown_removes_the_whole_provider(self) -> None:
        """One 401 must not be retried against every model on that provider."""
        result = plan(cooldowns={"openrouter:*": NOW + 60})
        self.assertFalse([c for c in result.candidates if c.provider == "openrouter"])

    def test_local_provider_is_not_affected_by_a_cloud_cooldown(self) -> None:
        pool = {**default_pool(), "ollama_local_models": ["hermes3:8b"]}
        result = plan(env={"OPENROUTER_API_KEY": "sk-or-v1-test"}, cooldowns={"openrouter:*": NOW + 60}, pool=pool)
        self.assertEqual(models(result), ["ollama_local/hermes3:8b"])


class TestVision(unittest.TestCase):
    def test_vision_plan_uses_the_vision_pool(self) -> None:
        result = plan(preferred="dai/vision-auto", catalog={"models": []})
        self.assertEqual(models(result), ["openrouter/alpha/one:free"])
        self.assertEqual(result.requested["provider"], "vision-auto")
        self.assertEqual(result.candidates[0].origin, "vision")

    def test_catalog_vision_models_are_appended(self) -> None:
        result = plan(preferred="dai/vision-auto")
        self.assertIn("openrouter/vision/extra:free", models(result))

    def test_paid_catalog_vision_models_never_enter_the_rotation(self) -> None:
        """They would only be skipped later, wasting a rotation slot."""
        self.assertNotIn("openrouter/paid/vision-model", models(plan(preferred="dai/vision-auto")))

    def test_text_only_models_are_excluded_from_vision(self) -> None:
        self.assertNotIn("openrouter/text/only:free", models(plan(preferred="dai/vision-auto")))

    def test_no_text_model_is_ever_substituted_into_a_vision_request(self) -> None:
        """The guarantee docs/CONFIG.md makes, asserted positively.

        Checking that one known text model is absent would still pass if the
        planner fell through to the rotating pools and picked up a *different*
        text model.  So assert the whole candidate set is a subset of the
        vision-capable ids, with the text pools and every other provider fully
        populated — a fall-through then shows up immediately.

        The expected set is written out from the fixture data rather than
        computed with the planner's own helper, so a regression in that helper
        is caught too.  With default_pool + default_catalog the vision-capable
        free ids are exactly these two: paid/vision-model is not ":free", and
        text/only:free has no image modality.
        """
        pool = {
            **default_pool(),
            "openrouter_free": ["text/only:free", "alpha/one:free", "beta/two:free"],
            "openrouter_free_vision": ["alpha/one:free"],
            "groq_models": ["groq-fast", "groq-also-fast"],
            "ollama_cloud_models": ["cloud-model"],
            "ollama_local_models": ["hermes3:8b"],
        }
        expected_vision = {"alpha/one:free", "vision/extra:free"}
        variants = {
            "plain": {},
            "vision hint": {"hint": "look at this screenshot"},
            "rotated": {"rr": 5},
            "every provider keyed": {"env": {**KEY_ENV, "OLLAMA_API_KEY": "oll-key", "CEREBRAS_API_KEY": "csk-key"}},
            "no other providers enabled": {"enabled": ["openrouter"]},
        }
        for label, kwargs in variants.items():
            with self.subTest(label):
                result = plan(preferred="dai/vision-auto", pool=pool, **kwargs)
                self.assertTrue(result.candidates, "a populated vision pool must produce candidates")
                for cand in result.candidates:
                    self.assertIn(cand.model, expected_vision, f"{cand.provider}/{cand.model} is not vision-capable")
                    self.assertEqual(cand.provider, "openrouter", "the vision rotation must not reach another provider")
                    self.assertEqual(cand.origin, "vision")

    def test_vision_hint_boosts_vision_models_in_the_text_pool(self) -> None:
        pool = {**default_pool(), "openrouter_free": ["text/only:free", "alpha/one:free"]}
        plain = [c.model for c in plan(pool=pool).candidates if c.provider == "openrouter"]
        boosted = [
            c.model for c in plan(pool=pool, hint="look at this screenshot").candidates if c.provider == "openrouter"
        ]
        self.assertEqual(plain[0], "text/only:free")
        self.assertEqual(boosted[0], "alpha/one:free")

    def test_vision_without_a_key_reports_it(self) -> None:
        result = plan(env={}, preferred="dai/vision-auto")
        self.assertEqual(result.candidates, [])
        self.assertTrue(any("vision" in n for n in result.notes))

    def test_is_vision_request(self) -> None:
        for text in ("describe this image", "take a screenshot", "what is in the photo", "vision task"):
            self.assertTrue(is_vision_request(text), text)
        for text in ("write a poem", "summarise this text", ""):
            self.assertFalse(is_vision_request(text), text)


class TestPolicySwitches(unittest.TestCase):
    def test_disabled_provider_is_excluded(self) -> None:
        result = plan(enabled=["openrouter", "ollama_local"])
        self.assertFalse([c for c in result.candidates if c.provider == "groq"])

    def test_none_means_all_allowed(self) -> None:
        self.assertTrue([c for c in plan(enabled=None).candidates if c.provider == "groq"])


class TestProviderStatus(unittest.TestCase):
    def test_reports_keys(self) -> None:
        status = provider_status(KEY_ENV)
        self.assertTrue(status["openrouter"])
        self.assertTrue(status["groq"])
        self.assertFalse(status["cerebras"])
        self.assertTrue(status["ollama_local"])  # keyless

    def test_base_url_override(self) -> None:
        result = plan(env={**KEY_ENV, "DAI_OPENROUTER_BASE_URL": "http://127.0.0.1:9/v1/"})
        self.assertEqual(result.candidates[0].base_url, "http://127.0.0.1:9/v1")

    def test_openrouter_sends_referer_and_title(self) -> None:
        headers = plan().candidates[0].headers
        self.assertIn("HTTP-Referer", headers)
        self.assertIn("X-Title", headers)
        self.assertTrue(headers["Authorization"].startswith("Bearer "))

    def test_local_provider_sends_no_auth_header(self) -> None:
        result = plan(env={}, pool={**default_pool(), "ollama_local_models": ["hermes3:8b"]})
        self.assertEqual(result.candidates[0].headers, {})

    def test_public_id_shape(self) -> None:
        result = plan(pool={**default_pool(), "ollama_local_models": ["hermes3:8b"]})
        first_by_provider: Dict[str, str] = {}
        for cand in result.candidates:
            first_by_provider.setdefault(cand.provider, cand.public_id)
        # OpenRouter ids are used verbatim (clients already know the slug form);
        # every other provider is prefixed so the backend is unambiguous.
        self.assertEqual(first_by_provider["openrouter"], "alpha/one:free")
        self.assertEqual(first_by_provider["ollama_local"], "ollama_local/hermes3:8b")
        self.assertEqual(first_by_provider["groq"], "groq/groq-fast")


class TestErrorText(unittest.TestCase):
    def test_only_error_fields_are_read(self) -> None:
        """Regression: scanning the whole body made 'operate' look like 'rate'."""
        payload = {
            "model": "great-rate-limited-thing",
            "choices": [{"message": {"content": "rate limit reached"}}],
            "error": {"message": "internal failure"},
        }
        text = error_text(payload)
        self.assertIn("internal failure", text)
        self.assertNotIn("great-rate", text)
        self.assertNotIn("rate limit reached", text)

    def test_handles_strings_and_nones(self) -> None:
        self.assertEqual(error_text(None), "")
        self.assertEqual(error_text("Rate Limit"), "rate limit")
        self.assertEqual(error_text([1, 2]), "")
        self.assertIn("nested", error_text({"error": {"error": {"message": "nested"}}}))


class TestClassifyFailure(unittest.TestCase):
    def classify(self, status: int, payload: Any = None) -> Any:
        from lib.dai.routing import classify_failure

        return classify_failure(status, payload, cooldown_seconds=90.0, short_cooldown=30.0, provider_cooldown=600.0)

    def test_success(self) -> None:
        for status in (200, 201, 204):
            self.assertEqual(self.classify(status).kind, KIND_OK)

    def test_429_is_rate_limited(self) -> None:
        failure = self.classify(429, {"error": {"message": "Rate limit reached"}})
        self.assertEqual(failure.kind, KIND_RATE_LIMITED)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.cooldown, 90.0)
        self.assertEqual(failure.provider_cooldown, 0.0)

    def test_500_mentioning_operate_is_not_rate_limited(self) -> None:
        """The exact false positive that caused spurious 90 s cooldowns."""
        failure = self.classify(500, {"error": {"message": "Failed to operate on the resource"}})
        self.assertEqual(failure.kind, KIND_SERVER_ERROR)
        self.assertEqual(failure.cooldown, 30.0)

    def test_500_mentioning_generate_is_not_rate_limited(self) -> None:
        self.assertEqual(self.classify(500, {"error": {"message": "cannot generate response"}}).kind, KIND_SERVER_ERROR)

    def test_401_cools_the_whole_provider(self) -> None:
        failure = self.classify(401, {"error": {"message": "Invalid api key"}})
        self.assertEqual(failure.kind, KIND_AUTH)
        self.assertEqual(failure.provider_cooldown, 600.0)
        self.assertEqual(failure.cooldown, 0.0)

    def test_402_is_quota(self) -> None:
        self.assertEqual(self.classify(402, {"error": {"message": "Insufficient credits"}}).kind, KIND_QUOTA)

    def test_403_quota_wording_is_quota(self) -> None:
        self.assertEqual(self.classify(403, {"error": {"message": "exceeded your current credits"}}).kind, KIND_QUOTA)

    def test_403_rate_wording_is_rate_limited(self) -> None:
        self.assertEqual(self.classify(403, {"error": {"message": "rate limit exceeded"}}).kind, KIND_RATE_LIMITED)

    def test_404_is_model_unavailable(self) -> None:
        self.assertEqual(self.classify(404, {"error": {"message": "Model not found"}}).kind, KIND_MODEL_UNAVAILABLE)

    def test_400_bad_parameter_is_a_non_retryable_client_error(self) -> None:
        """Rotating here would burn 20 models to re-report the same mistake."""
        failure = self.classify(400, {"error": {"message": "Invalid value for 'temperature'"}})
        self.assertEqual(failure.kind, KIND_CLIENT_ERROR)
        self.assertFalse(failure.retryable)

    def test_400_context_length_is_retryable(self) -> None:
        """A bigger model may well fit the same prompt."""
        failure = self.classify(400, {"error": {"message": "context_length_exceeded: reduce the prompt"}})
        self.assertEqual(failure.kind, KIND_CONTEXT_LENGTH)
        self.assertTrue(failure.retryable)

    def test_400_image_unsupported_is_retryable(self) -> None:
        failure = self.classify(400, {"error": {"message": "This model does not support images"}})
        self.assertEqual(failure.kind, KIND_UNSUPPORTED_INPUT)
        self.assertTrue(failure.retryable)

    def test_400_no_providers_is_retryable(self) -> None:
        failure = self.classify(400, {"error": {"message": "No providers available for this model"}})
        self.assertEqual(failure.kind, KIND_MODEL_UNAVAILABLE)
        self.assertTrue(failure.retryable)

    def test_transport_and_timeout(self) -> None:
        self.assertEqual(self.classify(502, {"error": {"message": "connection refused"}}).kind, KIND_NETWORK)
        self.assertEqual(self.classify(504, {"error": {"message": "gateway timeout"}}).kind, KIND_TIMEOUT)
        self.assertEqual(self.classify(408, None).kind, KIND_TIMEOUT)

    def test_503_is_server_error(self) -> None:
        self.assertEqual(self.classify(503, {"error": {"message": "overloaded"}}).kind, KIND_SERVER_ERROR)

    def test_unknown_status_is_not_retryable(self) -> None:
        failure = self.classify(418, {"error": {"message": "teapot"}})
        self.assertEqual(failure.kind, KIND_CLIENT_ERROR)
        self.assertFalse(failure.retryable)

    def test_non_dict_payload_does_not_crash(self) -> None:
        self.assertEqual(self.classify(500, "plain text body").kind, KIND_SERVER_ERROR)
        self.assertEqual(self.classify(500, None).kind, KIND_SERVER_ERROR)

    def test_failure_as_dict_is_serialisable(self) -> None:
        import json

        json.dumps(self.classify(429, {"error": {"message": "slow down"}}).as_dict())


if __name__ == "__main__":
    unittest.main()
