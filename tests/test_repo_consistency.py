"""Repository consistency tests.

These do not exercise behaviour — they catch *drift* between the parts of this
repo that have to agree: ``.env.example`` and the code that reads it, the
rotation pool and the provider table, the docs and the routes that exist, the
policy file and its own safety promises.

Every check here corresponds to a defect that was actually present:

* ``.env.example`` documented variables nothing read (all of them were dead).
* ``policy/sovereign.json`` shipped ``dry_run_default: false``.
* docs pointed at scripts and files that did not exist.
* files were missing trailing newlines.

Run with the rest of the suite: ``python3 -m unittest discover -s tests -t .``
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dai.routing import PROVIDERS  # noqa: E402
from tests.support import is_stdlib_module, stdlib_via_find_spec  # noqa: E402

PYTHON_DIRS = ("lib", "services", "skills", "tests")
TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".txt", ".yml", ".yaml", ".toml", ".cfg", ""}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pyc", ".woff", ".woff2"}


def tracked_files() -> list[Path]:
    """Every file that is, or is about to be, part of the repo.

    `--cached --others --exclude-standard` means committed files plus untracked
    ones that .gitignore does not exclude.  The earlier version listed `git
    ls-files` and then hand-added a few untracked directories, which silently
    skipped every newly created file — CHANGELOG.md, SECURITY.md, docs/API.md —
    until someone committed it.  A drift check that cannot see new files drifts
    itself.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except subprocess.CalledProcessError as exc:
        # Fail loudly and say why, rather than surfacing a bare CalledProcessError.
        # On a CI runner the usual cause is git's "dubious ownership" guard.
        # Deliberately not a skipTest: silently skipping every drift check would
        # remove the protection this whole module exists to provide.
        raise RuntimeError(
            "git could not enumerate the repository, so the consistency checks "
            f"cannot run: {exc.stderr.strip() or exc}\n"
            "On a CI runner this is usually git's dubious-ownership guard — add "
            '`git config --global --add safe.directory "$GITHUB_WORKSPACE"`.'
        ) from exc
    files = set()
    for rel in out:
        path = ROOT / rel
        if path.is_file() and "__pycache__" not in path.parts:
            files.add(path)
    return sorted(files)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def is_git_ignored(rel: str) -> bool:
    """True when .gitignore excludes this path, whether or not it exists.

    `git check-ignore` matches against the patterns, not the working tree, so it
    answers correctly on a fresh clone where the file has not been created yet.
    A directory-only pattern (one written with a trailing slash) will not match a
    bare name that does not exist yet, because git cannot tell it is a directory;
    probing a path *under* it settles that, so callers can pass either form.
    """
    for candidate in (rel, rel.rstrip("/") + "/"):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--", candidate],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True
        # A path that reaches through a symlink cannot be tracked by git at all
        # ("beyond a symbolic link"), so it is structurally impossible to commit.
        # Treat that as ignored: it defends the same guarantee a match does.
        if "beyond a symbolic link" in proc.stderr:
            return True
    return False


def python_sources() -> list[Path]:
    out = []
    for d in PYTHON_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" not in p.parts:
                out.append(p)
    return sorted(out)


def runtime_sources() -> list[Path]:
    """Code that ships and runs.

    Deliberately excludes tests/: fixtures invent env var names (``BAD_F``) and
    set safety keys on purpose, so scanning them would report phantom contract
    violations.
    """
    out = []
    for d in ("lib", "services", "skills"):
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" not in p.parts:
                out.append(p)
    return sorted(out)


# --- environment variable discovery ---------------------------------------
#
# Vars are read through five distinct mechanisms; each needs its own pattern.
# If a sixth mechanism is ever added, extend this list — that is the point of
# keeping them together.

# Mechanisms that appear anywhere in runtime code.
RE_ENV_CALL = re.compile(r"""\benv_(?:str|int|float|bool)\(\s*["']([A-Z][A-Z0-9_]*)["']""")
RE_OS_ENVIRON = re.compile(r"""os\.environ(?:\.get\(|\[)\s*["']([A-Z][A-Z0-9_]*)["']""")
RE_ENV_MAP_GET = re.compile(r"""\benv\(\)?\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")
RE_SOURCE_GET = re.compile(r"""\bsource\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")

# Mechanisms specific to one file.  Scoping matters: an unscoped
# `("Word", "UPPER")` tuple pattern also matches ("GET", "HEAD") and, worse,
# re-discovers self.setting() calls while skipping the safety-key exclusion.
RE_PROVIDER_FIELD = re.compile(r"""\b(?:key_env|base_url_env)=["']([A-Z][A-Z0-9_]*)["']""")
RE_EXTRA_HEADER = re.compile(r"""\(\s*["'][A-Za-z][A-Za-z-]*["']\s*,\s*["']([A-Z][A-Z0-9_]*)["']""")
ROUTING_ONLY = ("lib/dai/routing.py",)

# self.setting("policy_key", "ENV_VAR", default) — capture both arguments so the
# safety keys (whose env argument is deliberately ignored) can be excluded.
RE_SETTING = re.compile(r"""self\.setting\(\s*["']([a-z_]+)["']\s*,\s*["']([A-Z][A-Z0-9_]*)["']""")
WORKER_ONLY = ("services/agent-s-worker/server.py",)


def safety_keys() -> tuple[str, ...]:
    src = read(ROOT / "services/agent-s-worker/server.py")
    match = re.search(r"^SAFETY_KEYS\s*=\s*\(([^)]*)\)", src, re.M)
    assert match, "could not find SAFETY_KEYS in the worker"
    return tuple(re.findall(r'"([a-z_]+)"', match.group(1)))


def honored_env_vars() -> dict[str, str]:
    """Map env var -> a human description of where it is read."""
    found: dict[str, str] = {}
    safe = set(safety_keys())

    for path in runtime_sources():
        src = read(path)
        rel = str(path.relative_to(ROOT))
        for regex in (RE_ENV_CALL, RE_OS_ENVIRON, RE_ENV_MAP_GET, RE_SOURCE_GET):
            for name in regex.findall(src):
                found.setdefault(name, rel)
        if rel in ROUTING_ONLY:
            for regex in (RE_PROVIDER_FIELD, RE_EXTRA_HEADER):
                for name in regex.findall(src):
                    found.setdefault(name, rel)
            # The Cerebras model override is read through a plain env mapping.
            for name in re.findall(r"""env\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""", src):
                found.setdefault(name, rel)
        if rel in WORKER_ONLY:
            # self.setting(): the env argument is honoured only for non-safety
            # keys, so those must not be offered as configuration.
            for policy_key, name in RE_SETTING.findall(src):
                if policy_key in safe:
                    continue
                found.setdefault(name, rel)
    return found


def documented_env_vars() -> dict[str, bool]:
    """Map var -> True when set (uncommented) in .env.example."""
    out: dict[str, bool] = {}
    for line in read(ROOT / ".env.example").splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        commented = stripped.startswith("#")
        key = stripped.lstrip("#").strip().split("=", 1)[0].strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
            continue
        out[key] = out.get(key, False) or not commented
    return out


# Vars that are read but intentionally not documented: internal plumbing that
# an operator is not expected to set.
UNDOCUMENTED_OK = {
    # DAI_ENV selects the .env file itself, so documenting it *inside* .env
    # would be circular; .env.example explains this in a comment instead.
    "DAI_ENV",
    "DAI_INSIDE_SMOKE",  # recursion guard set by bin/smoke.sh
    "NO_COLOR",  # conventional, honoured by bin/lib.sh
    "DISPLAY",  # read from the environment, never set by us
    # A single-use approval token is an ephemeral credential handed to one
    # script invocation.  Putting it in .env would be actively wrong: it is
    # spent on first use, so the file would hold a dead token.  Documented in
    # skills/agent-s-delegate/SKILL.md and docs/OPERATIONS.md instead.
    "DAI_APPROVAL_TOKEN",
}

# Vars documented as explicitly *not* used yet.  They must stay commented out
# and must be called out as unused in the same file.
DOCUMENTED_BUT_UNUSED_OK = {
    "VENICE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
}


class TestEnvExampleHonesty(unittest.TestCase):
    """.env.example must describe reality — the old one was entirely dead."""

    def test_every_documented_var_is_actually_read(self):
        honored = honored_env_vars()
        dead = sorted(
            name for name in documented_env_vars() if name not in honored and name not in DOCUMENTED_BUT_UNUSED_OK
        )
        self.assertEqual(dead, [], f".env.example documents vars nothing reads: {dead}")

    def test_unused_vars_are_commented_out_and_flagged(self):
        documented = documented_env_vars()
        text = read(ROOT / ".env.example")
        for name in DOCUMENTED_BUT_UNUSED_OK:
            if name not in documented:
                continue  # absent entirely is fine
            self.assertFalse(
                documented[name],
                f"{name} is documented as unused but is set (uncommented) in .env.example",
            )
        self.assertIn("not used by the spine yet", text.lower(), ".env.example must say plainly which vars are unused")

    def test_every_read_var_is_documented(self):
        documented = documented_env_vars()
        missing = sorted(
            name for name, where in honored_env_vars().items() if name not in documented and name not in UNDOCUMENTED_OK
        )
        self.assertEqual(
            missing,
            [],
            f"code reads vars that .env.example never mentions: {missing}",
        )

    def test_safety_keys_are_not_offered_as_env_vars(self):
        """Safety keys resolve from the policy file only; documenting an env var
        for them would invite an override that silently does nothing."""
        documented = documented_env_vars()
        offenders = [
            name
            for name in ("DAI_AGENT_S_ENABLED", "DAI_AGENT_S_DRY_RUN", "DAI_AGENT_S_MAX_STEPS_CAP")
            if name in documented
        ]
        self.assertEqual(offenders, [], f"safety-key env vars must not be documented: {offenders}")

    def test_safety_keys_are_declared(self):
        keys = set(safety_keys())
        self.assertEqual(
            keys,
            {"enabled", "dry_run_default", "bind_owner_live_desktop", "require_approval_token", "max_steps_hard_cap"},
        )

    def test_env_example_is_not_executable_shell(self):
        """It gets parsed, never sourced; an active line must not carry shell
        substitution.  Prose in comments may mention `Authorization: Bearer`."""
        for line in read(ROOT / ".env.example").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertNotIn("$(", stripped, f"shell substitution in an active line: {stripped}")
            self.assertNotIn("`", stripped, f"backtick in an active line: {stripped}")

    def test_env_example_ships_no_real_looking_key(self):
        for line in read(ROOT / ".env.example").splitlines():
            if line.strip().startswith("#"):
                continue
            _, _, value = line.partition("=")
            self.assertLess(len(value.strip()), 40, f".env.example should ship empty values, got: {line[:60]}")


class TestPolicyFile(unittest.TestCase):
    """The policy file is a safety artifact, so its defaults get tested."""

    def setUp(self):
        self.policy = json.loads(read(ROOT / "policy/sovereign.json"))

    def test_parses_and_declares_a_version(self):
        self.assertIsInstance(self.policy.get("version"), int)
        self.assertGreaterEqual(self.policy["version"], 3)

    def test_ships_safe_agent_s_defaults(self):
        agent_s = self.policy["agent_s"]
        self.assertTrue(agent_s["dry_run_default"], "shipped policy must default to dry-run")
        self.assertFalse(agent_s["bind_owner_live_desktop"], "shipped policy must never bind the owner's live desktop")
        self.assertTrue(
            agent_s["require_approval_token"], "shipped policy must require an approval token for live runs"
        )

    def test_declares_every_safety_key(self):
        agent_s = self.policy["agent_s"]
        for key in safety_keys():
            self.assertIn(key, agent_s, f"policy/sovereign.json omits safety key '{key}'")

    def test_paid_inference_is_off_by_default(self):
        self.assertFalse(self.policy["inference"]["openrouter"]["paid_enabled"], "paid inference must be opt-in")
        self.assertIn("paid_inference", self.policy["ask_before"])
        self.assertIn("openrouter_paid", self.policy["ask_before"])

    def test_live_desktop_control_requires_ask(self):
        self.assertIn("live_owner_desktop_control", self.policy["ask_before"])

    def test_secret_redaction_is_on(self):
        self.assertTrue(self.policy["inference"]["secret_redaction"])

    def test_declares_the_models_the_code_defaults_to(self):
        agent_s = self.policy["agent_s"]
        self.assertEqual(agent_s["model"], "dai/auto")
        self.assertEqual(agent_s["grounding_model"], "dai/vision-auto")
        self.assertEqual(self.policy["inference"]["default_model"], "dai/auto")

    def test_every_enabled_provider_is_known(self):
        inference = self.policy["inference"]
        for name in PROVIDERS:
            self.assertIn(name, inference, f"policy omits provider section '{name}'")

    def test_approvals_section_matches_the_store(self):
        approvals = self.policy["approvals"]
        self.assertEqual(approvals["file"], "policy/approvals.json")
        self.assertGreater(approvals["default_ttl_seconds"], 0)
        self.assertEqual(approvals["default_max_uses"], 1)

    def test_example_approvals_file_matches_the_live_shape(self):
        """The shipped example must describe what the store really writes —
        derived from the store itself, so it cannot drift."""
        import tempfile

        from lib.dai.approvals import ApprovalStore

        example = json.loads(read(ROOT / "policy/approvals.example.json"))
        self.assertEqual(example["version"], 1)
        self.assertIn("tokens", example)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.json"
            store = ApprovalStore(path)
            _, record = store.issue("agent_s_gui_task", "example scope")
        live_fields = set(record)

        for token, documented in example["tokens"].items():
            with self.subTest(token):
                self.assertEqual(
                    set(documented), live_fields, "example record fields disagree with ApprovalStore.issue()"
                )

    def test_example_documents_the_real_actions(self):
        from lib.dai.approvals import KNOWN_ACTIONS

        example = json.loads(read(ROOT / "policy/approvals.example.json"))
        documented = set(example.get("_actions", {}))
        self.assertEqual(
            documented, set(KNOWN_ACTIONS), "approvals.example.json must document exactly the known actions"
        )

    def test_policy_files_parse(self):
        for rel in (
            "policy/sovereign.json",
            "policy/approvals.example.json",
            "config/rotation-pool.json",
            "config/free-models.json",
        ):
            with self.subTest(rel):
                json.loads(read(ROOT / rel))


class TestRotationPool(unittest.TestCase):
    """The pool config and the provider table must describe the same world."""

    def setUp(self):
        self.pool_raw = json.loads(read(ROOT / "config/rotation-pool.json"))
        self.catalog = json.loads(read(ROOT / "config/free-models.json"))
        # The pool file carries metadata keys (version, strategy, notes) next to
        # the actual model lists; only list-valued keys are pools.
        self.metadata = {k: v for k, v in self.pool_raw.items() if not isinstance(v, list)}
        self.pool = {k: v for k, v in self.pool_raw.items() if isinstance(v, list)}

    def test_pool_keys_match_provider_specs(self):
        """Every provider that reads a pool must find it in the file.

        Extra pools are allowed: `openrouter_free_vision` is a boost list the
        planner expands into candidates directly, not a provider's pool_key.
        """
        declared = {spec.pool_key for spec in PROVIDERS.values() if spec.pool_key}
        present = set(self.pool)
        missing = sorted(declared - present)
        self.assertEqual(missing, [], f"providers reference pools absent from rotation-pool.json: {missing}")
        extras = sorted(present - declared)
        self.assertEqual(
            extras,
            ["openrouter_free_vision"],
            f"unexpected extra pools (add them to a provider or explain them): {extras}",
        )

    def test_metadata_declares_the_strategy(self):
        self.assertEqual(self.metadata.get("strategy"), "rotate_on_rate_limit")
        self.assertIsInstance(self.metadata.get("version"), int)

    def test_pool_entries_are_well_formed(self):
        for pool_key, models in self.pool.items():
            with self.subTest(pool_key):
                self.assertIsInstance(models, list)
                self.assertTrue(models, f"{pool_key} is empty")
                for entry in models:
                    self.assertIsInstance(entry, str, f"{pool_key} has a non-string entry")
                    self.assertTrue(entry.strip(), f"{pool_key} has a blank entry")
                self.assertEqual(len(models), len(set(models)), f"{pool_key} lists duplicates")

    def test_openrouter_pool_entries_are_free_tier(self):
        """The whole strategy is 'top free models'; a paid id here would bill."""
        for pool_key, models in self.pool.items():
            if not pool_key.startswith("openrouter"):
                continue
            for model in models:
                with self.subTest(model):
                    self.assertTrue(model.endswith(":free"), f"{pool_key} contains a non-:free model {model}")

    def test_vision_pool_entries_are_well_formed(self):
        """The vision pool is expanded into candidates on its own (routing.py),
        so it need not be a subset of openrouter_free — but every entry must be
        a routable free OpenRouter id, or the planner will 404 upstream."""
        vision = self.pool.get("openrouter_free_vision") or []
        self.assertTrue(vision, "no vision pool declared")
        shape = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+:free$")
        for model in vision:
            with self.subTest(model):
                self.assertRegex(model, shape, f"vision model {model} is not an owner/model:free id")

    def test_planner_expands_the_vision_pool(self):
        """Guard the wiring, not just the data: the pool must actually be read."""
        src = read(ROOT / "lib/dai/routing.py")
        self.assertIn("openrouter_free_vision", src)
        from lib.dai.routing import _vision_models

        models = _vision_models(self.pool_raw, {})
        self.assertTrue(models, "_vision_models returned nothing for the shipped pool")
        for entry in self.pool.get("openrouter_free_vision") or []:
            self.assertIn(entry, models)

    def test_catalog_entries_have_required_fields(self):
        models = self.catalog.get("models") or self.catalog
        self.assertIsInstance(models, (list, dict))
        entries = models if isinstance(models, list) else models.get("data", [])
        self.assertTrue(entries, "free-models.json describes no models")
        for entry in entries[:50]:
            with self.subTest(entry.get("id")):
                self.assertIn("id", entry)

    def test_catalog_ids_are_provider_prefixed_or_bare(self):
        """Whatever the shape, ids must be strings the router can route on."""
        entries = self.catalog.get("models") or self.catalog.get("data") or []
        for entry in entries:
            self.assertIsInstance(entry.get("id"), str)


class TestSkills(unittest.TestCase):
    """A SKILL.md Vellum cannot load is worse than no skill at all."""

    def skills(self) -> list[Path]:
        return sorted(p.parent for p in (ROOT / "skills").glob("*/SKILL.md"))

    def test_the_shipped_skills_exist(self):
        names = {p.name for p in self.skills()}
        self.assertEqual(names, {"agent-s-delegate", "dai-voice", "english-to-code"})

    def test_frontmatter_is_valid_yaml_with_required_fields(self):
        for skill in self.skills():
            text = read(skill / "SKILL.md")
            with self.subTest(skill.name):
                self.assertTrue(text.startswith("---\n"), "missing frontmatter fence")
                body = text.split("---\n", 2)[1]
                for field in ("name:", "description:"):
                    self.assertIn(field, body, f"frontmatter lacks {field}")
                name = re.search(r"^name:\s*(.+)$", body, re.M).group(1).strip().strip("'\"")
                self.assertEqual(name, skill.name, "frontmatter name must match the directory name")

    def test_scripts_referenced_by_a_skill_exist(self):
        for skill in self.skills():
            text = read(skill / "SKILL.md")
            for rel in set(re.findall(r"scripts/([A-Za-z0-9_.-]+\.py)", text)):
                with self.subTest(f"{skill.name}/{rel}"):
                    self.assertTrue(
                        (skill / "scripts" / rel).is_file(), f"SKILL.md references scripts/{rel}, which is missing"
                    )

    def test_referenced_docs_exist(self):
        for skill in self.skills():
            text = read(skill / "SKILL.md")
            for rel in set(re.findall(r"references/([A-Za-z0-9_.-]+\.md)", text)):
                with self.subTest(f"{skill.name}/{rel}"):
                    self.assertTrue((skill / "references" / rel).is_file())

    def test_skill_python_scripts_parse(self):
        for path in (ROOT / "skills").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            with self.subTest(str(path.relative_to(ROOT))):
                ast.parse(read(path))

    def test_delegate_script_reaches_the_worker(self):
        text = read(ROOT / "skills/agent-s-delegate/scripts/delegate_task.py")
        # It reads worker_url from the policy file rather than hardcoding a port,
        # so the assertion is about the route, not the number.
        self.assertIn("worker_url", text, "the delegate must resolve the worker from policy")
        self.assertIn("/v1/tasks", text, "the delegate must post to /v1/tasks")
        self.assertIn("approval", text.lower(), "the delegate must support approval tokens")
        self.assertIn("dry_run", text, "the delegate must be able to request a dry run")

    def test_policy_worker_url_matches_the_worker_default_port(self):
        policy = json.loads(read(ROOT / "policy/sovereign.json"))
        url = policy["agent_s"]["worker_url"]
        self.assertTrue(url.endswith(":8765"), f"policy worker_url {url} disagrees with the worker's default port")

    def test_policy_router_url_matches_the_router_default_port(self):
        policy = json.loads(read(ROOT / "policy/sovereign.json"))
        url = policy["inference"]["router_url"]
        self.assertTrue(url.endswith(":11435/v1"), f"policy router_url {url} disagrees with the router's default port")


class TestStdlibOnly(unittest.TestCase):
    """No third-party dependencies: this repo must run on a bare Debian python3."""

    LOCAL_OK: ClassVar[set] = {"lib", "tests", "services", "skills", "support"}

    def test_no_third_party_imports(self):
        offenders = []
        for path in python_sources():
            tree = ast.parse(read(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import
                        continue
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if not name:
                        continue
                    if is_stdlib_module(name) or name in self.LOCAL_OK:
                        continue
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertEqual(offenders, [], f"third-party imports found: {offenders}")

    def test_the_python39_stdlib_fallback_agrees_with_the_authoritative_set(self):
        """The 3.9 code path must be verified on a modern interpreter too.

        `stdlib_via_find_spec` runs only where `sys.stdlib_module_names` is
        missing, which in practice is CI's 3.9 job and nowhere else. A fix that
        is never exercised on the machine it is written on is how a version
        problem gets "fixed" with another version problem, so on 3.10+ compare
        the fallback against the authoritative set for every name it will really
        be asked about — every top-level import in the repo — plus third-party
        names it must reject and local package names it must not mistake for
        stdlib.
        """
        known = getattr(sys, "stdlib_module_names", None)
        if known is None:
            self.skipTest("on 3.9 the fallback is the only path; nothing to compare against")

        imported = set()
        for path in python_sources():
            tree = ast.parse(read(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imported.add(node.module.split(".")[0])
        self.assertTrue(imported, "no imports discovered; the comparison would be vacuous")

        third_party = {"pytest", "requests", "numpy", "yaml", "ruff", "flask", "httpx", "aiohttp"}
        for name in sorted(imported | third_party):
            with self.subTest(name):
                self.assertEqual(
                    stdlib_via_find_spec(name),
                    name in known,
                    f"the 3.9 fallback disagrees with sys.stdlib_module_names about {name!r}",
                )

    def test_no_pip_install_instructions_without_a_venv(self):
        """Anything telling the user to pip install must isolate it in a venv."""
        for path in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")):
            text = read(path)
            for match in re.finditer(r"pip install\s+(?!-)", text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line = text[line_start : text.find("\n", match.start())]
                with self.subTest(f"{path.name}: {line.strip()[:60]}"):
                    self.assertTrue(
                        "venv" in text[max(0, match.start() - 300) : match.start() + 100].lower() or "--user" in line,
                        "a bare `pip install` should be a venv or --user install",
                    )


class TestPython39Compatibility(unittest.TestCase):
    """The floor is Python 3.9, enforced statically.

    CI does run 3.9, but a violation there costs a round trip and can hide
    itself: `sys.stdlib_module_names` (3.10+) was read in a class body, so on 3.9
    this very module failed at *import* time and 69 drift checks vanished from
    the run instead of failing — the job reported "Ran 365 tests" and everything
    that ran passed.

    These constructs are all detectable from the AST, and this module is itself
    3.9-safe: the newer node types are fetched with getattr because ast.Match and
    ast.TryStar do not exist on 3.9.  Checking the AST rather than the text also
    avoids the false positives a grep produces — `getattr(sys,
    "stdlib_module_names", None)` and a docstring mentioning the name are both
    correct, and neither is an attribute node.
    """

    # attribute or imported name -> the version that introduced it
    NEWER_THAN_39: ClassVar[Dict[str, str]] = {
        "stdlib_module_names": "3.10",
        "bit_count": "3.10",
        "pairwise": "3.10",
        "KW_ONLY": "3.10",
        "TypeAlias": "3.10",
        "ParamSpec": "3.10",
        "Concatenate": "3.10",
        "dataclass_transform": "3.11",
        "ExceptionGroup": "3.11",
        "BaseExceptionGroup": "3.11",
        "StrEnum": "3.11",
        "Self": "3.11",
        "override": "3.12",
        "TypeAliasType": "3.12",
    }
    NEWER_MODULES: ClassVar[Dict[str, str]] = {"tomllib": "3.11"}
    # Keyword arguments added after 3.9, by the callable they belong to.
    NEWER_KWARGS: ClassVar[Dict[str, str]] = {"zip.strict": "3.10"}

    def offenders_in(self, path: Path) -> List[str]:
        tree = ast.parse(read(path))
        # Not always under ROOT: the self-check below feeds this a temp file.
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = path.name
        found: List[str] = []

        match_node = getattr(ast, "Match", None)
        trystar_node = getattr(ast, "TryStar", None)

        for node in ast.walk(tree):
            if match_node is not None and isinstance(node, match_node):
                found.append(f"{rel}:{node.lineno}: match/case statement (3.10+)")
            if trystar_node is not None and isinstance(node, trystar_node):
                found.append(f"{rel}:{node.lineno}: except* (3.11+)")

            if isinstance(node, ast.Attribute) and node.attr in self.NEWER_THAN_39:
                found.append(f"{rel}:{node.lineno}: .{node.attr} ({self.NEWER_THAN_39[node.attr]}+)")

            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in self.NEWER_MODULES:
                        found.append(f"{rel}:{node.lineno}: import {root} ({self.NEWER_MODULES[root]}+)")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in self.NEWER_THAN_39:
                        found.append(
                            f"{rel}:{node.lineno}: from {node.module} import {alias.name} "
                            f"({self.NEWER_THAN_39[alias.name]}+)"
                        )

            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                for kw in node.keywords:
                    key = f"{name}.{kw.arg}"
                    if key in self.NEWER_KWARGS:
                        found.append(f"{rel}:{node.lineno}: {key}= ({self.NEWER_KWARGS[key]}+)")
                    if name == "dataclass" and kw.arg == "slots":
                        found.append(f"{rel}:{node.lineno}: dataclass(slots=) (3.10+)")
        return found

    def test_no_constructs_newer_than_the_supported_floor(self):
        offenders: List[str] = []
        for path in python_sources():
            offenders.extend(self.offenders_in(path))
        self.assertEqual(
            offenders,
            [],
            "Python 3.9 is the supported floor (pyproject requires-python, CI "
            "matrix); these break on it: " + "; ".join(offenders[:12]),
        )

    def test_the_floor_is_still_declared_as_3_9(self):
        """Keep the declared floor and this test's assumption in step.

        If requires-python moves to 3.10+, this class should be revisited rather
        than left asserting a floor the project no longer supports — and the
        pyupgrade rules disabled in pyproject.toml should be re-enabled.
        """
        text = read(ROOT / "pyproject.toml")
        self.assertRegex(text, r'requires-python\s*=\s*">=\s*3\.9"')
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "no CI workflow found")
        joined = "\n".join(read(w) for w in workflows)
        self.assertIn('"3.9"', joined, "CI no longer tests the declared floor")

    def test_the_check_itself_detects_a_violation(self):
        """Guard the guard: an empty result must mean clean, not broken.

        Without this, a typo in the rule tables above would make the check pass
        vacuously forever.
        """
        tmp = Path(tempfile.mkdtemp(prefix="dai-py39-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sample = tmp / "sample.py"
        sample.write_text(
            "import sys\n"
            "import tomllib\n"
            "names = sys.stdlib_module_names\n"
            "pairs = list(zip([1], [2], strict=True))\n"
            "count = (5).bit_count()\n",
            encoding="utf-8",
        )
        found = self.offenders_in(sample)
        for expected in ("stdlib_module_names", "tomllib", "zip.strict", "bit_count"):
            with self.subTest(expected):
                self.assertTrue(
                    any(expected in item for item in found),
                    f"the compatibility check missed {expected}; found: {found}",
                )


class TestFileHygiene(unittest.TestCase):
    """Small things that make a repo look unfinished."""

    def test_text_files_end_with_exactly_one_newline(self):
        for path in tracked_files():
            if path.suffix in BINARY_SUFFIXES:
                continue
            data = path.read_bytes()
            if not data:
                continue
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertTrue(data.endswith(b"\n"), "missing trailing newline")
                self.assertFalse(data.endswith(b"\n\n"), "blank line at end of file")

    def test_python_files_have_no_tabs(self):
        for path in python_sources():
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertNotIn("\t", read(path))

    def test_no_trailing_whitespace(self):
        for path in tracked_files():
            if path.suffix in BINARY_SUFFIXES or path.name == "Makefile":
                continue
            offenders = [i + 1 for i, line in enumerate(read(path).splitlines()) if line != line.rstrip()]
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertEqual(offenders, [], f"trailing whitespace on lines {offenders[:5]}")

    def test_python_files_parse(self):
        for path in python_sources():
            with self.subTest(str(path.relative_to(ROOT))):
                ast.parse(read(path))

    def test_no_debug_leftovers(self):
        patterns = (r"\bbreakpoint\(\)", r"\bpdb\.set_trace\(\)", r"\bprint\(.*DEBUG", r"\bTODO: remove\b", r"\bXXX\b")
        for path in python_sources():
            text = read(path)
            with self.subTest(str(path.relative_to(ROOT))):
                for pat in patterns:
                    self.assertIsNone(re.search(pat, text), f"debug leftover matching {pat}")

    def test_no_absolute_home_paths_hardcoded(self):
        """Paths must be derived, not baked to one machine's layout."""
        bad = re.compile(r"/home/[a-z]+/|/Users/[a-z]+/|C:\\\\Users")
        for path in python_sources() + [p for p in (ROOT / "bin").iterdir() if p.is_file()]:
            text = read(path)
            hits = [m.group(0) for m in bad.finditer(text)]
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertEqual(hits, [], f"hardcoded absolute paths: {set(hits)}")

    def test_docs_do_not_hardcode_a_home_directory(self):
        """Docs must not point at one machine's home directory.

        KEYS.md once opened by telling the reader to edit the .env at an
        absolute path under someone's home directory, which is wrong for every
        other reader and rots the moment that person moves house.  Use a
        repo-relative path, or an explicit angle-bracket placeholder.
        """
        bad = re.compile(r"/home/[a-zA-Z0-9_.-]+/|/Users/[a-zA-Z0-9_.-]+/")
        docs = [p for p in tracked_files() if p.suffix == ".md"]
        self.assertTrue(docs, "no markdown docs found")
        for path in docs:
            hits = []
            for lineno, line in enumerate(read(path).splitlines(), 1):
                for match in bad.finditer(line):
                    hits.append(f"{lineno}: {match.group(0)}")
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertEqual(hits, [], f"docs hardcode a home directory: {hits}")

    def test_readiness_checklist_is_not_pre_checked(self):
        """A shipped checklist must not assert that the reader's steps are done.

        READY.md used to carry "[x] **You:** keys in .env (OPENROUTER + OLLAMA
        present)" — true on the author's machine, false and misleading for
        everyone else, and silently rotting as that machine changed.
        """
        path = ROOT / "READY.md"
        if not path.exists():
            self.skipTest("READY.md is not present")
        checked = [line.strip() for line in read(path).splitlines() if line.strip().startswith("- [x]")]
        self.assertEqual(
            checked,
            [],
            "READY.md ships pre-checked boxes; a checklist for the reader starts unchecked: " + "; ".join(checked[:4]),
        )

    def test_documented_test_count_matches_the_suite(self):
        """A cited test total must equal the real one.

        Counts in prose rot the moment a test is added — README and CHANGELOG
        both went stale within minutes of being written.  Rather than removing
        the number (it is genuinely useful: it tells a reader how much coverage
        they are getting), enforce it.

        Any "<N> tests" in a tracked markdown file must equal the real total.  A
        doc that means a subset should phrase it so it is not a bare total —
        write "24 smoke checks", not "24 tests".
        """
        tests_dir = ROOT / "tests"
        real = 0
        for path in sorted(tests_dir.glob("test_*.py")):
            for line in read(path).splitlines():
                if re.match(r"\s*def test_", line):
                    real += 1
        self.assertGreater(real, 100, "the suite looks suspiciously small")

        pattern = re.compile(r"(\d+)\s+tests\b")
        stale = []
        for path in tracked_files():
            if path.suffix != ".md":
                continue
            for lineno, line in enumerate(read(path).splitlines(), 1):
                for match in pattern.finditer(line):
                    if int(match.group(1)) != real:
                        stale.append(f"{path.relative_to(ROOT)}:{lineno} says {match.group(0)}, suite has {real}")
        self.assertEqual(
            stale,
            [],
            "Docs cite a test count that does not match the suite. Update the "
            "number, or rephrase a subset as '24 smoke checks' rather than "
            "'24 tests': " + "; ".join(stale),
        )

    def test_no_committed_secrets(self):
        shapes = (
            r"sk-or-v1-[A-Za-z0-9]{16,}",
            r"sk-[A-Za-z0-9]{32,}",
            r"gsk_[A-Za-z0-9]{20,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"hf_[A-Za-z0-9]{20,}",
        )
        for path in tracked_files():
            if path.suffix in BINARY_SUFFIXES:
                continue
            text = read(path)
            with self.subTest(str(path.relative_to(ROOT))):
                for pat in shapes:
                    for match in re.finditer(pat, text):
                        # Test fixtures deliberately contain obviously fake
                        # values; so do the shipped examples.
                        rel = path.relative_to(ROOT)
                        allowed = (
                            rel.parts[0] == "tests"
                            or path.suffix == ".example"
                            or "example" in path.name.lower()
                            or "SUPERSECRET" in match.group(0)
                            or "FAKE" in match.group(0).upper()
                            or "abcdef" in match.group(0)
                        )
                        self.assertTrue(allowed, f"{rel} contains a key-shaped string")

    def test_runtime_artifacts_are_gitignored(self):
        text = read(ROOT / ".gitignore")
        for entry in (".env", "logs/", "state/", "policy/approvals.json", "__pycache__/", "vendor/"):
            self.assertIn(entry, text, f".gitignore is missing {entry}")

    def test_env_example_is_explicitly_not_ignored(self):
        text = read(ROOT / ".gitignore")
        self.assertIn("!.env.example", text, ".gitignore must un-ignore .env.example or it cannot ship")

    def test_executable_bits(self):
        for path in (ROOT / "bin").iterdir():
            if not path.is_file() or path.name == "lib.sh":
                continue
            with self.subTest(path.name):
                self.assertTrue(os.access(path, os.X_OK), f"bin/{path.name} is not executable")
        self.assertFalse(os.access(ROOT / "bin/lib.sh", os.X_OK), "bin/lib.sh is sourced; it should not be executable")


class TestDocumentationLinks(unittest.TestCase):
    """Every path a doc or script points at must exist."""

    def doc_files(self) -> list[Path]:
        out = list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md"))
        for skill in (ROOT / "skills").glob("*/SKILL.md"):
            out.append(skill)
        out += list((ROOT / "skills").glob("*/references/*.md"))
        return sorted(out)

    def test_markdown_links_resolve(self):
        link = re.compile(r"\[[^\]]*\]\(([^)#][^)]*)\)")
        for doc in self.doc_files():
            text = read(doc)
            for target in link.findall(text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (doc.parent / target.split("#")[0]).resolve()
                with self.subTest(f"{doc.name} -> {target}"):
                    self.assertTrue(resolved.exists(), f"broken link in {doc.name}: {target}")

    def test_referenced_scripts_exist(self):
        # Require a word boundary before "bin/" so a venv path such as
        # ~/.local/agent-s-venv/bin/pip is not read as a repo script.
        refs = re.compile(r"(?<![\w./~-])(?:\./)?bin/([A-Za-z0-9_.-]+)")
        for doc in self.doc_files():
            for name in set(refs.findall(read(doc))):
                with self.subTest(f"{doc.name} -> bin/{name}"):
                    self.assertTrue(
                        (ROOT / "bin" / name).is_file(), f"{doc.name} references bin/{name}, which does not exist"
                    )

    def test_referenced_repo_paths_exist(self):
        """Only file-shaped references are checked: prose like "native Vellum
        skills/tools" contains a slash but is not a path.

        A reference that neither exists nor is git-ignored is stale documentation
        and fails.  Git-ignored references are exempt, because they are created
        at runtime and are correctly absent from a fresh clone: this test passed
        on a workstation that had run bin/issue-approval.sh and failed in CI on
        seven docs at once, all for policy/approvals.json.  The exemption is
        asked of git rather than hardcoded, so it cannot drift, and
        test_runtime_artifacts_are_gitignored keeps it honest from the other
        side.
        """
        refs = re.compile(
            r"(?<![\w./-])((?:config|policy|services|docs|skills|lib|tests|bin)/"
            r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)"
        )
        for doc in self.doc_files():
            for rel in set(refs.findall(read(doc))):
                rel = rel.rstrip(".,;:)")
                if any(ch in rel for ch in "*<>"):
                    continue
                if (ROOT / rel).exists():
                    continue
                with self.subTest(f"{doc.name} -> {rel}"):
                    self.assertTrue(
                        is_git_ignored(rel),
                        f"{doc.name} references {rel}, which neither exists nor is "
                        "git-ignored as a runtime artifact — the reference is stale",
                    )

    def test_runtime_artifacts_are_gitignored(self):
        """The paths the exemption above relies on must really be ignored.

        Without this, a reference to policy/approvals.json would pass merely
        because the file happens to be missing — and if .gitignore ever lost that
        entry, a file of live approval tokens could be committed.  Checked with
        `git check-ignore` against the pattern rather than the working tree, so
        it holds on a fresh clone where none of these exist yet.
        """
        for rel in (
            ".env",
            "policy/approvals.json",
            "state/agent-s-tasks/task.json",
            "logs/model-router.log",
            "vendor/vellum-assistant/package.json",
            "skills-ready/agent-s-delegate/SKILL.md",
        ):
            with self.subTest(rel):
                self.assertTrue(
                    is_git_ignored(rel),
                    f"{rel} is a secret-bearing or generated runtime artifact and must stay git-ignored",
                )


class TestServiceSurface(unittest.TestCase):
    """Routes and versions the docs and clients depend on."""

    def routes(self, service: str) -> set[str]:
        """/health and /ready are served by the shared JsonHandler in lib/dai,
        so the base module is part of every service's surface."""
        sources = [read(ROOT / f"services/{service}/server.py"), read(ROOT / "lib/dai/httpserver.py")]
        out: set[str] = set()
        for src in sources:
            out |= set(re.findall(r'path == "(/[^"]*)"', src))
            out |= set(re.findall(r'path\.startswith\("(/[^"]*)"\)', src))
            # path in ("/", "/health") — the tuple form both services use.
            for group in re.findall(r"path (?:in|not in) \(([^)]*)\)", src):
                out |= set(re.findall(r'"(/[^"]*)"', group))
        return out

    def test_router_exposes_the_documented_routes(self):
        routes = self.routes("model-router")
        for expected in (
            "/health",
            "/ready",
            "/version",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/status/config",
            "/v1/status/cooldowns",
            "/v1/status/keys",
            "/v1/status/plan",
            "/v1/status/reset",
            "/v1/status/stats",
        ):
            self.assertIn(expected, routes, f"model-router lost route {expected}")

    def test_worker_exposes_the_documented_routes(self):
        routes = self.routes("agent-s-worker")
        for expected in ("/health", "/ready", "/version", "/v1/settings", "/v1/tasks"):
            self.assertIn(expected, routes, f"agent-s-worker lost route {expected}")

    def test_both_services_declare_a_version(self):
        for service in ("model-router", "agent-s-worker"):
            src = read(ROOT / f"services/{service}/server.py")
            with self.subTest(service):
                # VERSION derives from the ServiceInfo literal, so the string
                # printed by --version and the one in /health cannot disagree.
                self.assertRegex(src, r"(?m)^VERSION\s*=\s*SERVICE\.version$")
                self.assertRegex(src, r'ServiceInfo\(\s*"%s"\s*,\s*"[0-9]+\.[0-9]+"' % service)

    def test_services_import_from_the_shared_lib(self):
        for service in ("model-router", "agent-s-worker"):
            src = read(ROOT / f"services/{service}/server.py")
            with self.subTest(service):
                self.assertIn("from lib.dai", src)
                self.assertIn("redact", src, "a service must redact before logging")

    def test_lib_package_exports_a_version(self):
        import lib.dai as dai

        self.assertRegex(dai.__version__, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_every_lib_module_imports_cleanly(self):
        for path in sorted((ROOT / "lib/dai").glob("*.py")):
            name = path.stem
            if name == "__init__":
                continue
            with self.subTest(name):
                __import__(f"lib.dai.{name}")


def _balanced_span(src: str, start: int) -> str:
    """Return the argument text of a call whose "(" ends at `start`.

    Tracks string literals so a paren or comma inside a quoted detail cannot
    end the span early.
    """
    depth, i, quote = 1, start, ""
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[start:i]
        i += 1
    return src[start:]


def _top_level_args(span: str) -> list[str]:
    """Split a call's argument text on commas that are not nested or quoted."""
    args, depth, quote, current = [], 0, "", []
    i = 0
    while i < len(span):
        ch = span[i]
        if quote:
            current.append(ch)
            if ch == "\\":
                if i + 1 < len(span):
                    current.append(span[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    args.append("".join(current))
    return args


def http_error_codes(path: Path) -> set[str]:
    """Every error code a file can raise through HttpError.

    Only the *code* argument (position 2) is read, so kwargs like
    ``extra={...}`` and ``headers={"Connection": "close"}`` cannot contribute
    payload keys or header values.  Taking the whole argument rather than a
    single literal is what catches conditional codes:

        HttpError(503, "no_providers_ready" if not any_key
                       else "requested_model_unavailable", ...)
    """
    src = read(path)
    codes: set[str] = set()
    for match in re.finditer(r"HttpError\(", src):
        args = _top_level_args(_balanced_span(src, match.end()))
        if len(args) < 2:
            continue
        codes |= set(re.findall(r'"([a-z][a-z0-9_]{2,})"', args[1]))
    return codes


def approval_codes() -> set[str]:
    """Codes the approval store returns; services re-raise them verbatim.

    ``\\(?`` matters: two of the returns are parenthesised multi-line tuples,
    so a pattern anchored directly on the quote silently misses them.
    """
    src = read(ROOT / "lib/dai/approvals.py")
    return set(re.findall(r'return\s*\(?\s*"([a-z_]{4,})"\s*,', src))


def task_result_codes() -> set[str]:
    """Codes recorded in ``result.error`` on a finished task."""
    src = read(ROOT / "services/agent-s-worker/server.py")
    return set(re.findall(r'"error":\s*"([a-z_]{4,})"', src))


def documented_error_codes() -> set[str]:
    """Codes docs/API.md claims exist.

    Deliberately narrow. Backticked identifiers appear all over the doc
    (``dry_run``, ``ready_for_chat``, ``task_timeout_seconds``), so only three
    shapes count as a documented error code:

    1. a table whose header column is ``Code`` -> that column
    2. a table whose header has an ``error`` column (``| Status | error | ...``)
       -> that column
    3. the ``**Label:**`` code lists in the "Error codes" section, including
       their wrapped continuation lines
    """
    lines = read(ROOT / "docs/API.md").splitlines()
    codes: set[str] = set()

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    code_column: int | None = None
    in_error_section = False
    in_labelled_list = False

    for line in lines:
        if line.startswith("## "):
            in_error_section = line.strip() == "## Error codes"
            code_column = None
            in_labelled_list = False
            continue

        if line.startswith("|"):
            body = cells(line)
            if all(re.fullmatch(r":?-{3,}:?", c) for c in body if c):
                continue  # separator row
            lowered = [re.sub(r"[`*]", "", c).lower() for c in body]
            if code_column is None and any(c in ("code", "error") for c in lowered):
                # Header row: remember which column carries the code.
                code_column = next(i for i, c in enumerate(lowered) if c in ("code", "error"))
                continue
            if code_column is not None and len(body) > code_column:
                codes |= set(re.findall(r"`([a-z][a-z0-9_]{2,})`", body[code_column]))
            continue

        code_column = None

        if in_error_section and re.match(r"^\*\*[^*]+:\*\*", line):
            in_labelled_list = True
        elif in_labelled_list and not line.strip():
            in_labelled_list = False

        if in_labelled_list:
            codes |= set(re.findall(r"`([a-z][a-z0-9_]{2,})`", line))

    return codes


class TestErrorCodeDocumentation(unittest.TestCase):
    """docs/API.md is what clients code against; it must not drift.

    Writing this doc from memory produced codes that do not exist
    (`no_candidates`, `approval_wrong_action`) and omitted ten that do. Both
    directions are checked.
    """

    def setUp(self):
        self.real = (
            http_error_codes(ROOT / "services/model-router/server.py")
            | http_error_codes(ROOT / "services/agent-s-worker/server.py")
            | http_error_codes(ROOT / "lib/dai/httpserver.py")
            | approval_codes()
            | task_result_codes()
        )
        self.documented = documented_error_codes()

    def test_extraction_finds_the_codes_we_know_exist(self):
        """Guard the extractor itself: a regex that matches nothing would make
        every other assertion here vacuously pass."""
        for known in (
            "all_candidates_failed",
            "invalid_json",
            "queue_full",
            "no_providers_ready",
            "requested_model_unavailable",
            "missing_approval_token",
            "agent_s_not_installed",
        ):
            self.assertIn(known, self.real, f"extractor missed {known}")

    def test_every_real_code_is_documented(self):
        missing = sorted(self.real - self.documented)
        self.assertEqual(missing, [], f"codes exist in the services but not in docs/API.md: {missing}")

    def test_no_documented_code_is_invented(self):
        invented = sorted(self.documented - self.real)
        self.assertEqual(invented, [], f"docs/API.md documents codes no service can emit: {invented}")

    def test_approval_codes_are_documented_as_shared(self):
        for code in approval_codes():
            with self.subTest(code):
                self.assertIn(code, self.documented)

    def test_api_doc_is_the_one_services_advertise(self):
        for service in ("model-router", "agent-s-worker"):
            src = read(ROOT / f"services/{service}/server.py")
            with self.subTest(service):
                self.assertIn('"docs/API.md"', src)
        self.assertTrue((ROOT / "docs/API.md").is_file(), "services advertise docs/API.md, so it must exist")


class TestBinScriptsReferenceRealThings(unittest.TestCase):
    """Scripts must not call helpers or paths that do not exist."""

    def helpers_defined_in_lib(self) -> set[str]:
        return set(re.findall(r"^(dai_[a-z_]+)\(\)", read(ROOT / "bin/lib.sh"), re.M))

    def test_every_dai_helper_called_is_defined(self):
        defined = self.helpers_defined_in_lib()
        self.assertTrue(defined, "no helpers found in bin/lib.sh")
        # Only names in *command position* are calls: `dai_routed` appears as a
        # JSON field name in prose and must not be mistaken for a helper.
        call = re.compile(r"""(?:^|&&|\|\||[;|])\s*(dai_[a-z_]+)\b""")
        for path in sorted((ROOT / "bin").iterdir()):
            if not path.is_file() or path.name == "lib.sh":
                continue
            for name in set(call.findall(read(path))):
                with self.subTest(f"{path.name}: {name}"):
                    self.assertIn(name, defined, f"{path.name} calls undefined helper {name}")

    def test_scripts_reference_existing_repo_paths(self):
        # Paths that are optional or created at runtime: doctor.sh checks for
        # them precisely because they may be absent on a fresh clone.
        optional = ("vendor", "skills-ready", "state", "logs", ".env")
        for path in sorted((ROOT / "bin").iterdir()):
            if not path.is_file():
                continue
            for rel in set(re.findall(r'"\$ROOT/([A-Za-z0-9_./-]+)"', read(path))):
                if any(ch in rel for ch in "*<>$"):
                    continue
                # Optional or created-at-runtime paths (compare without any
                # trailing slash, since scripts reference both forms).
                if rel.rstrip("/").split("/")[0] in optional:
                    continue
                with self.subTest(f"{path.name} -> {rel}"):
                    self.assertTrue((ROOT / rel).exists(), f"{path.name} references $ROOT/{rel}, which does not exist")

    def test_optional_paths_are_actually_treated_as_optional(self):
        """doctor.sh must not FAIL on vendor/ being absent — that broke fresh clones."""
        src = read(ROOT / "bin/doctor.sh")
        for rel in ("vendor/vellum-assistant", "vendor/Agent-S"):
            self.assertIn(rel, src, f"doctor.sh no longer checks {rel}")
        # Find the block that mentions the vendor path and confirm it warns.
        window = src[src.index("vendor/vellum-assistant") :]
        head = window[:600]
        self.assertIn("dai_warn", head, "a missing vendor/ checkout must be a warning, not a failure")

    def test_bin_dai_dispatches_to_real_scripts(self):
        src = read(ROOT / "bin/dai")
        for rel in set(re.findall(r'"\$ROOT/bin/([A-Za-z0-9_.-]+)"', src)):
            with self.subTest(rel):
                self.assertTrue((ROOT / "bin" / rel).is_file())

    def test_no_script_uses_bashisms_under_sh(self):
        """All scripts declare bash; make sure none claims /bin/sh."""
        for path in sorted((ROOT / "bin").iterdir()):
            if not path.is_file():
                continue
            first = read(path).splitlines()[0]
            with self.subTest(path.name):
                self.assertIn("bash", first, f"{path.name} should use a bash shebang")


if __name__ == "__main__":
    unittest.main()
