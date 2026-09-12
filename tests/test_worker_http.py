"""End-to-end tests for the Agent S worker over real HTTP.

The live-GUI cases run a fake ``agent_s`` binary and a fake ``xset`` on a temp
``PATH``, so the whole subprocess/timeout/cancel/artifact path is exercised
without touching a real display.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

from tests.support import (
    FAKE_AGENT_S,
    FAKE_AGENT_S_FAIL,
    FAKE_AGENT_S_HANG,
    FAKE_AGENT_S_LEAK,
    StubUpstream,
    PathPrepend,
    ServiceFixture,
    TempSpine,
    XSET_OK,
    get,
    post,
    start_worker,
    wait_for,
    write_executable,
)

INSTRUCTION = "Open Chromium and go to example.com"


class WorkerCase(unittest.TestCase):
    """Worker with no agent_s installed: dry-run and policy paths."""

    policy: Optional[Dict[str, Any]] = None
    env: Dict[str, str] = {}

    def setUp(self) -> None:
        self.spine = TempSpine(policy=self.policy)
        self.service, self.runtime = start_worker(self.spine, env=dict(self.env))
        self.base = self.service.base_url
        self.addCleanup(self.tearDownServices)

    def tearDownServices(self) -> None:
        self.service.stop()
        self.runtime.stop()
        self.spine.cleanup()

    def submit(self, body: Optional[Dict[str, Any]] = None, **kwargs: Any):
        payload: Dict[str, Any] = {"instruction": INSTRUCTION}
        payload.update(body or {})
        return self.service.post("/v1/tasks", payload, **kwargs)

    def wait_terminal(self, task_id: str, *, timeout: float = 20.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            status, body, _h, _r = self.service.get(f"/v1/tasks/{task_id}")
            if status == 200:
                last = body
                if body.get("status") not in ("queued", "running"):
                    return body
            time.sleep(0.05)
        raise AssertionError(f"task {task_id} never finished; last state {last}")

    def issue(self, action: str = "agent_s_gui_task", scope: str = INSTRUCTION, **kw: Any) -> str:
        token, _rec = self.runtime.approvals.issue(action, scope, **kw)
        return token


class TestHealth(WorkerCase):
    def test_health_reports_policy_and_capability(self) -> None:
        status, body, _h, _r = self.service.get("/health")
        self.assertEqual(status, 200)
        for key in (
            "ok", "service", "version", "enabled", "dry_run_default", "display",
            "bind_owner_live_desktop", "require_approval_token", "agent_s_installed",
            "live_capable", "queue_depth", "running", "ready",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["service"], "agent-s-worker")

    def test_dry_run_worker_is_ready_without_agent_s(self) -> None:
        _s, body, _h, _r = self.service.get("/health")
        self.assertFalse(body["agent_s_installed"])
        self.assertTrue(body["ready"], "dry-run must work before gui-agents is installed")
        self.assertFalse(body["live_capable"])
        self.assertEqual(self.service.get("/ready")[0], 200)

    def test_health_never_claims_a_task_can_run_when_it_cannot(self) -> None:
        """Regression: 'ready' used to be hardcoded True."""
        policy = {"agent_s": {"enabled": True, "dry_run_default": False, "require_approval_token": True}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        _s, body, _h, _r = service.get("/health")
        self.assertFalse(body["ready"], "live-by-default without agent_s installed is not ready")
        self.assertEqual(service.get("/ready")[0], 503)

    def test_settings_endpoint_documents_precedence(self) -> None:
        status, body, _h, _r = self.service.get("/v1/settings")
        self.assertEqual(status, 200)
        self.assertIn("display", body["settings"])
        self.assertIn("dry_run_default", body["safety_keys_policy_only"])

    def test_version_endpoint(self) -> None:
        self.assertEqual(self.service.get("/version")[1]["service"], "agent-s-worker")

    def test_unknown_route(self) -> None:
        status, body, _h, _r = self.service.get("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")


class TestDryRun(WorkerCase):
    def test_dry_run_task_completes(self) -> None:
        """Regression: every POST used to 500 because approvals.json was absent."""
        status, body, _h, _r = self.submit({"dry_run": True})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["dry_run"])

        task = self.wait_terminal(body["id"])
        self.assertEqual(task["status"], "dry_run_complete")
        result = task["result"]
        self.assertEqual(result["status"], "dry_run_complete")
        self.assertIn(INSTRUCTION, result["summary"])
        self.assertFalse(result["live_owner_desktop"])
        self.assertEqual(result["actions"], [])

    def test_dry_run_is_the_default_when_policy_says_so(self) -> None:
        _s, body, _h, _r = self.submit()
        self.assertTrue(body["dry_run"])
        self.assertEqual(self.wait_terminal(body["id"])["status"], "dry_run_complete")

    def test_dry_run_shows_the_command_a_live_run_would_use(self) -> None:
        _s, body, _h, _r = self.submit({"dry_run": True})
        result = self.wait_terminal(body["id"])["result"]
        self.assertIn("would_run", result)
        self.assertIn("--task", result["would_run"])
        self.assertIn(INSTRUCTION, result["would_run"])
        self.assertIn("--ground_model", result["would_run"])

    def test_dry_run_reports_what_blocks_a_live_run(self) -> None:
        _s, body, _h, _r = self.submit({"dry_run": True})
        result = self.wait_terminal(body["id"])["result"]
        self.assertIn("agent_s_installed", result["live_blockers"])
        self.assertIn("issue-approval.sh", result["hint"])
        self.assertFalse(result["checks"]["agent_s_installed"])

    def test_dry_run_needs_no_approval_token(self) -> None:
        status, _body, _h, _r = self.submit({"dry_run": True})
        self.assertEqual(status, 202)

    def test_dry_run_does_not_execute_anything(self) -> None:
        _s, body, _h, _r = self.submit({"dry_run": True})
        task = self.wait_terminal(body["id"])
        self.assertEqual(task["result"]["artifacts"], [])
        artifacts = self.spine.dir / "state" / "tasks" / "artifacts"
        self.assertFalse(artifacts.exists() and any(artifacts.iterdir()))

    def test_max_steps_is_clamped_to_the_policy_cap(self) -> None:
        _s, body, _h, _r = self.submit({"dry_run": True, "max_steps": 9999})
        task = self.wait_terminal(body["id"])
        self.assertLessEqual(task["max_steps"], 20)
        self.assertIn("20", task["result"]["would_run"])


class TestTaskValidation(WorkerCase):
    def test_missing_instruction(self) -> None:
        status, body, _h, _r = self.service.post("/v1/tasks", {"dry_run": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "instruction_required")

    def test_blank_instruction(self) -> None:
        status, body, _h, _r = self.submit({"instruction": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "instruction_required")

    def test_oversized_instruction(self) -> None:
        status, body, _h, _r = self.submit({"instruction": "x" * 5000})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "instruction_too_long")

    def test_bad_max_steps(self) -> None:
        status, body, _h, _r = self.submit({"max_steps": "lots"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_max_steps")

    def test_malformed_json_returns_400_not_a_dropped_connection(self) -> None:
        status, body, _h, raw = self.service.post(
            "/v1/tasks", raw_body=b"{oops", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_json")
        self.assertTrue(raw)

    def test_task_id_must_be_a_uuid(self) -> None:
        for bad in ("../../etc/passwd", "not-a-uuid", "12345", "abc-def-ghi"):
            with self.subTest(task_id=bad):
                status, body, _h, _r = self.service.get(f"/v1/tasks/{bad}")
                self.assertIn(status, (400, 404))
                self.assertIn(body["error"], ("invalid_task_id", "not_found"))

    def test_trailing_slash_normalises_to_the_list_endpoint(self) -> None:
        status, body, _h, _r = self.service.get("/v1/tasks/")
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "list")

    def test_unknown_task_is_404(self) -> None:
        status, body, _h, _r = self.service.get("/v1/tasks/11111111-2222-3333-4444-555555555555")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_traversal_id_cannot_escape_the_state_dir(self) -> None:
        status, _body, _h, _r = self.service.get("/v1/tasks/..%2F..%2F..%2Fetc%2Fpasswd")
        self.assertIn(status, (400, 404))
        self.assertFalse((Path(self.spine.dir) / "etc").exists())


class TestLiveTaskGating(WorkerCase):
    def test_live_task_without_a_token_is_refused(self) -> None:
        status, body, _h, _r = self.submit({"dry_run": False})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "missing_approval_token")
        self.assertIn("issue-approval.sh", body["detail"])

    def test_live_task_with_an_unknown_token_is_refused(self) -> None:
        status, body, _h, _r = self.submit({"dry_run": False, "approval_token": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "unknown_approval_token")

    def test_token_for_a_different_instruction_is_refused(self) -> None:
        token = self.issue(scope="some other instruction")
        status, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_scope_mismatch")

    def test_wrong_action_token_is_refused(self) -> None:
        token = self.issue(action="openrouter_paid", scope=INSTRUCTION)
        status, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_action_mismatch")

    def test_expired_token_is_refused(self) -> None:
        token = self.issue(ttl_seconds=1)
        doc = json.loads(self.spine.approvals_path.read_text())
        doc["tokens"][token]["expires_at"] = time.time() - 5
        self.spine.approvals_path.write_text(json.dumps(doc))
        status, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_expired")

    def test_wildcard_token_is_accepted(self) -> None:
        token = self.issue(scope="*")
        status, _body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        self.assertEqual(status, 202)

    def test_single_use_token_cannot_authorise_two_tasks(self) -> None:
        token = self.issue()
        self.assertEqual(self.submit({"dry_run": False, "approval_token": token})[0], 202)
        status, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "approval_exhausted")

    def test_token_is_spent_even_though_agent_s_is_missing(self) -> None:
        """Admission is the spend point; the record must not be reusable."""
        token = self.issue()
        _s, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        task = self.wait_terminal(body["id"], timeout=15)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"]["error"], "agent_s_not_installed")
        self.assertIn("gui-agents", task["result"]["detail"])
        err, _detail = self.runtime.approvals.validate("agent_s_gui_task", token, INSTRUCTION)
        self.assertEqual(err, "approval_exhausted")

    def test_missing_agent_s_is_reported_honestly(self) -> None:
        token = self.issue()
        _s, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        task = self.wait_terminal(body["id"], timeout=15)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["result"]["error"], "agent_s_not_installed")
        self.assertIn("pip install gui-agents", task["result"]["detail"])


class TestPolicySafety(WorkerCase):
    def test_disabled_worker_refuses_tasks(self) -> None:
        policy = {"agent_s": {"enabled": False, "dry_run_default": True}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        status, body, _h, _r = service.post("/v1/tasks", {"instruction": INSTRUCTION, "dry_run": True})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "agent_s_disabled_in_policy")
        self.assertFalse(service.get("/health")[1]["ready"])

    def test_live_desktop_binding_is_refused(self) -> None:
        policy = {"agent_s": {"enabled": True, "dry_run_default": True, "bind_owner_live_desktop": True}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        status, body, _h, _r = service.post("/v1/tasks", {"instruction": INSTRUCTION, "dry_run": True})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "live_desktop_forbidden_until_repolicy")

    def test_live_by_default_still_requires_a_token(self) -> None:
        """dry_run_default=false must not mean 'run live without approval'."""
        policy = {"agent_s": {"enabled": True, "dry_run_default": False, "require_approval_token": True}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        status, body, _h, _r = service.post("/v1/tasks", {"instruction": INSTRUCTION})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "missing_approval_token")

    def test_missing_policy_file_does_not_crash_the_worker(self) -> None:
        """Regression: policy['agent_s'] raised KeyError/500 on a bad file."""
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        spine.policy_path.write_text("{corrupt")
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        self.assertEqual(service.get("/health")[0], 200)
        status, body, _h, _r = service.post("/v1/tasks", {"instruction": INSTRUCTION})
        self.assertIn(status, (202, 403))
        self.assertNotEqual(status, 500)

    def test_env_cannot_weaken_a_safety_flag(self) -> None:
        """Documented rule: safety keys come from the policy file only."""
        policy = {"agent_s": {"enabled": True, "dry_run_default": True, "require_approval_token": True}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(
            spine,
            env={
                "DAI_AGENT_S_DRY_RUN": "false",
                "DAI_AGENT_S_ENABLED": "true",
                "AGENT_S_REQUIRE_APPROVAL": "false",
            },
        )
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        body = service.get("/health")[1]
        self.assertTrue(body["dry_run_default"], "an env var must not switch dry-run off")
        self.assertTrue(body["require_approval_token"])
        status, err, _h, _r = service.post("/v1/tasks", {"instruction": INSTRUCTION})
        self.assertEqual(status, 202)
        self.assertTrue(service.get(f"/v1/tasks/{err['id']}")[1]["dry_run"])


class TestSettingsPrecedence(WorkerCase):
    def test_env_overrides_policy_for_host_knobs(self) -> None:
        policy = {
            "agent_s": {
                "enabled": True,
                "dry_run_default": True,
                "display": ":99",
                "grounding_model": "dai/vision-auto",
                "timeout_seconds": 600,
            }
        }
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(
            spine,
            env={
                "AGENT_S_DISPLAY": ":42",
                "AGENT_S_GROUND_MODEL": "custom/vlm:free",
                "AGENT_S_TIMEOUT": "7",
                "AGENT_S_GROUNDING_WIDTH": "1920",
            },
        )
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        settings = service.get("/v1/settings")[1]["settings"]
        self.assertEqual(settings["display"], ":42")
        self.assertEqual(settings["ground_model"], "custom/vlm:free")
        self.assertEqual(settings["task_timeout_seconds"], 7)
        self.assertEqual(settings["grounding_width"], 1920)

    def test_policy_wins_over_the_builtin_default(self) -> None:
        policy = {"agent_s": {"enabled": True, "dry_run_default": True, "display": ":7"}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        self.assertEqual(service.get("/v1/settings")[1]["settings"]["display"], ":7")

    def test_documented_env_vars_are_all_live(self) -> None:
        """Regression: every AGENT_S_* var in .env.example was dead config."""
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(
            spine,
            env={
                "AGENT_S_PROVIDER": "open_router",
                "AGENT_S_MODEL": "z-ai/glm-5.2:free",
                "AGENT_S_GROUND_URL": "http://127.0.0.1:9999/v1",
                "AGENT_S_GROUND_MODEL": "ui-tars-1.5-7b",
                "AGENT_S_GROUNDING_WIDTH": "1920",
                "AGENT_S_GROUNDING_HEIGHT": "1080",
            },
        )
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        settings = service.get("/v1/settings")[1]["settings"]
        self.assertEqual(settings["provider"], "open_router")
        self.assertEqual(settings["model"], "z-ai/glm-5.2:free")
        self.assertEqual(settings["ground_url"], "http://127.0.0.1:9999/v1")
        self.assertEqual(settings["ground_model"], "ui-tars-1.5-7b")
        self.assertEqual((settings["grounding_width"], settings["grounding_height"]), (1920, 1080))

    def test_policy_timeout_key_is_honoured(self) -> None:
        """Regression: policy said timeout_seconds, code read task_timeout_seconds."""
        policy = {"agent_s": {"enabled": True, "dry_run_default": True, "timeout_seconds": 33}}
        spine = TempSpine(policy=policy)
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine)
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        self.assertEqual(service.get("/v1/settings")[1]["settings"]["task_timeout_seconds"], 33)


class LiveWorkerCase(WorkerCase):
    """Worker with a fake agent_s binary and a fake xset, so live runs work."""

    agent_s_script = FAKE_AGENT_S
    timeout_seconds = "5"

    def setUp(self) -> None:
        self.spine = TempSpine(policy=self.policy)
        self.upstream = StubUpstream().start()
        self.shim_dir = Path(tempfile.mkdtemp(prefix="dai-shim-"))
        write_executable(self.shim_dir / "xset", XSET_OK)
        self.venv = self.spine.dir / "venv"
        write_executable(self.venv / "bin" / "agent_s", self.agent_s_script)
        self._path = PathPrepend(self.shim_dir)
        self._path.__enter__()
        self.addCleanup(self._extra_cleanup)
        self.service, self.runtime = start_worker(
            self.spine,
            env={
                "DAI_AGENT_S_VENV": str(self.venv),
                "DAI_MODEL_ROUTER": self.upstream.base_url[: -len("/v1")],
                "AGENT_S_TIMEOUT": self.timeout_seconds,
            },
        )

    def _extra_cleanup(self) -> None:
        self._path.__exit__(None, None, None)
        self.service.stop()
        self.runtime.stop()
        self.upstream.stop()
        self.spine.cleanup()
        import shutil

        shutil.rmtree(self.shim_dir, ignore_errors=True)

    def tearDownServices(self) -> None:  # overridden: cleanup registered above
        pass

    def live(self, token: Optional[str] = None, instruction: str = INSTRUCTION, **kw: Any):
        if token is None:
            token = self.issue(scope=instruction)
        return self.submit({"dry_run": False, "approval_token": token, "instruction": instruction}, **kw)


class TestLiveExecution(LiveWorkerCase):
    def test_live_task_succeeds_and_records_artifacts(self) -> None:
        _s, body, _h, _r = self.live()
        self.assertEqual(body["status"], "queued")
        task = self.wait_terminal(body["id"], timeout=30)
        self.assertEqual(task["status"], "succeeded", json.dumps(task.get("result"), indent=2))
        result = task["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["display"], ":99")
        self.assertTrue(result["artifacts"])
        self.assertIn("screenshot.png", result["artifacts"][0])
        self.assertEqual(result["actions"], [], "we do not fabricate an action log we never parsed")
        self.assertTrue(result["command"])

    def test_display_is_verified_before_running(self) -> None:
        _s, body, _h, _r = self.live()
        result = self.wait_terminal(body["id"], timeout=30)["result"]
        self.assertIn(":99", result["display_note"])

    def test_model_router_is_checked_so_a_dead_spine_fails_fast(self) -> None:
        """Regression: an unreachable router cost a 600 s timeout, not an error."""
        self.upstream.stop()
        token = self.issue()
        _s, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        task = self.wait_terminal(body["id"], timeout=30)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["result"]["error"], "model_router_not_ready")

class TestLiveFailure(LiveWorkerCase):
    agent_s_script = FAKE_AGENT_S_FAIL

    def test_failing_agent_s_is_reported(self) -> None:
        _s, body, _h, _r = self.live()
        task = self.wait_terminal(body["id"], timeout=30)
        self.assertEqual(task["status"], "failed")
        result = task["result"]
        self.assertEqual(result["error"], "agent_s_nonzero_exit")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("grounding model could not see the screen", result["log_tail"])


class TestLiveTimeout(LiveWorkerCase):
    agent_s_script = FAKE_AGENT_S_HANG
    timeout_seconds = "2"

    def test_hanging_task_is_killed_and_reported_as_a_timeout(self) -> None:
        started = time.time()
        _s, body, _h, _r = self.live()
        task = self.wait_terminal(body["id"], timeout=30)
        self.assertEqual(task["status"], "timeout")
        self.assertEqual(task["result"]["error"], "agent_s_timeout")
        self.assertLess(time.time() - started, 20, "must not wait out the 60 s sleep")

    def test_no_agent_s_process_survives_the_timeout(self) -> None:
        _s, body, _h, _r = self.live()
        self.wait_terminal(body["id"], timeout=30)
        self.assertTrue(wait_for(lambda: self.runtime.running_ids() == [], timeout=5))


class TestLiveCancel(LiveWorkerCase):
    agent_s_script = FAKE_AGENT_S_HANG
    timeout_seconds = "120"

    def test_running_task_can_be_cancelled(self) -> None:
        _s, body, _h, _r = self.live()
        task_id = body["id"]
        self.assertTrue(wait_for(lambda: self.service.get(f"/v1/tasks/{task_id}")[1].get("status") == "running", timeout=15))
        status, cancelled, _h, _r = self.service.post(f"/v1/tasks/{task_id}/cancel", {})
        self.assertEqual(status, 202)
        self.assertTrue(cancelled["cancelling"])
        task = self.wait_terminal(task_id, timeout=30)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["result"]["error"], "cancelled_by_owner")

    def test_cancelling_a_finished_task_is_a_no_op(self) -> None:
        _s, body, _h, _r = self.submit({"dry_run": True})
        self.wait_terminal(body["id"])
        status, result, _h, _r = self.service.post(f"/v1/tasks/{body['id']}/cancel", {})
        self.assertEqual(status, 200)
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["status"], "dry_run_complete")

    def test_cancelling_an_unknown_task_is_404(self) -> None:
        status, _b, _h, _r = self.service.post("/v1/tasks/11111111-2222-3333-4444-555555555555/cancel", {})
        self.assertEqual(status, 404)

    def test_cancel_rejects_a_malformed_id(self) -> None:
        status, body, _h, _r = self.service.post("/v1/tasks/nope/cancel", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_task_id")


class TestLiveSecretHygiene(LiveWorkerCase):
    agent_s_script = FAKE_AGENT_S_LEAK

    def test_secrets_in_agent_s_output_are_redacted_in_the_record(self) -> None:
        token = self.issue()
        _s, body, _h, _r = self.submit({"dry_run": False, "approval_token": token})
        task = self.wait_terminal(body["id"], timeout=30)
        blob = json.dumps(task)
        self.assertNotIn("sk-or-v1-abcdef1234567890", blob)
        self.assertIn("[REDACTED]", task["result"]["log_tail"])

    def test_api_key_argument_is_redacted_in_the_recorded_command(self) -> None:
        _s, body, _h, _r = self.live()
        task = self.wait_terminal(body["id"], timeout=30)
        command = task["result"]["command"]
        index = command.index("--model_api_key")
        self.assertEqual(command[index + 1], "[REDACTED]")


class TestTaskListing(WorkerCase):
    def test_list_returns_summaries(self) -> None:
        ids = [self.submit({"dry_run": True})[1]["id"] for _ in range(3)]
        for task_id in ids:
            self.wait_terminal(task_id)
        status, body, _h, _r = self.service.get("/v1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 3)
        self.assertEqual({t["id"] for t in body["data"]}, set(ids))
        for item in body["data"]:
            self.assertNotIn("result", item)  # summaries stay small
            self.assertIn("status", item)

    def test_list_filters_by_status(self) -> None:
        done = self.submit({"dry_run": True})[1]["id"]
        self.wait_terminal(done)
        body = self.service.get("/v1/tasks?status=dry_run_complete")[1]
        self.assertEqual(body["count"], 1)
        self.assertEqual(self.service.get("/v1/tasks?status=running")[1]["count"], 0)

    def test_list_honours_limit(self) -> None:
        for _ in range(4):
            self.submit({"dry_run": True})
        body = self.service.get("/v1/tasks?limit=2")[1]
        self.assertEqual(body["count"], 2)

    def test_state_files_are_owner_only(self) -> None:
        task_id = self.submit({"dry_run": True})[1]["id"]
        self.wait_terminal(task_id)
        path = self.spine.dir / "state" / "tasks" / f"{task_id}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TestQueueLimits(WorkerCase):
    def test_queue_is_bounded(self) -> None:
        """The docstring promises a bounded queue; maxsize=0 would not be."""
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine, env={"DAI_AGENT_S_MAX_QUEUE": "1"})
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        self.assertEqual(runtime.max_queue, 1)

    def test_zero_max_queue_is_clamped_not_unbounded(self) -> None:
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        _service, runtime = start_worker(spine, env={"DAI_AGENT_S_MAX_QUEUE": "0"})
        self.addCleanup(runtime.stop)
        self.assertGreaterEqual(runtime.max_queue, 1)


class TestArtifacts(WorkerCase):
    def test_unlisted_artifact_is_refused(self) -> None:
        task_id = self.submit({"dry_run": True})[1]["id"]
        self.wait_terminal(task_id)
        status, body, _h, _r = self.service.get(f"/v1/tasks/{task_id}/artifacts/../../.env")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_unknown_subresource(self) -> None:
        task_id = self.submit({"dry_run": True})[1]["id"]
        self.wait_terminal(task_id)
        status, body, _h, _r = self.service.get(f"/v1/tasks/{task_id}/secrets")
        self.assertEqual(status, 404)


class TestWorkerAuth(WorkerCase):
    def test_token_gate_protects_tasks_but_not_health(self) -> None:
        spine = TempSpine()
        self.addCleanup(spine.cleanup)
        service, runtime = start_worker(spine, env={"DAI_AGENT_S_TOKEN": "worker-gate"})
        self.addCleanup(service.stop)
        self.addCleanup(runtime.stop)
        self.assertEqual(service.get("/health")[0], 200)
        self.assertEqual(service.post("/v1/tasks", {"instruction": INSTRUCTION})[0], 401)
        self.assertEqual(service.get("/v1/tasks")[0], 401)
        status, body, _h, _r = service.post(
            "/v1/tasks", {"instruction": INSTRUCTION}, headers={"Authorization": "Bearer worker-gate"}
        )
        self.assertEqual(status, 202)


if __name__ == "__main__":
    unittest.main()
