"""One-shot approval tokens — the policy gate for anything privileged.

Two things need approval in this spine:

* ``agent_s_gui_task`` — a live (non-dry-run) GUI task on the agent desktop
* ``openrouter_paid``  — a paid OpenRouter model

Tokens live in ``policy/approvals.json`` (git-ignored, mode 0600) and are
issued by ``bin/issue-approval.sh``.  Validation **fails closed**:

* a missing/unknown/expired/action-mismatched token is rejected
* an empty scope is rejected — a token that authorises "anything" must say
  ``"*"`` explicitly, so it is visible in the file and in ``--list``
* spending is atomic under a lock and persisted before the task is admitted,
  so two concurrent requests cannot both use a single-use token

Issue tokens from a shell with ``python3 -m lib.dai.approvals`` or the
wrapper script; see ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .jsonio import AtomicJsonStore

DEFAULT_TTL_SECONDS = 3600
WILDCARD = "*"
TOKEN_BYTES = 18

KNOWN_ACTIONS: Tuple[str, ...] = ("agent_s_gui_task", "openrouter_paid")


def _now() -> float:
    return time.time()


def _new_token() -> str:
    """A fresh token that is safe to pass as a single command-line argument.

    ``secrets.token_urlsafe`` draws from the base64url alphabet, which includes
    ``-``.  A token beginning with ``-`` cannot be handed to a script as a
    separate argv element: ``argparse`` reads it as an option flag and refuses
    the invocation, so a perfectly valid approval surfaces as a usage error.
    ``--approval-token=<tok>`` would survive that, but callers should not have
    to know it, so re-roll instead.  Discarding 1 candidate in 64 costs about
    0.02 bits of entropy on a 144-bit token.
    """
    for _ in range(8):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        if not token.startswith("-"):
            return token
    # Unreachable with a working CSPRNG (64**-8 per attempt).  Fall back to a
    # hex alphabet rather than spinning forever on a broken entropy source.
    return secrets.token_hex(TOKEN_BYTES)


def _token_expiry(rec: Dict[str, Any]) -> Optional[float]:
    """Expiry timestamp for a token record, or ``None`` when it never expires."""
    if rec.get("expires_at") is not None:
        try:
            return float(rec["expires_at"])
        except (TypeError, ValueError):
            return None
    ttl = rec.get("ttl_seconds")
    if ttl is None:
        return None
    try:
        return float(rec.get("created_at") or 0.0) + float(ttl)
    except (TypeError, ValueError):
        return None


def _uses(rec: Dict[str, Any]) -> int:
    """How many times a token has been used (understands the legacy flag)."""
    used = rec.get("used")
    if isinstance(used, int):
        return used
    return 1 if rec.get("spent") else 0


def _max_uses(rec: Dict[str, Any]) -> int:
    value = rec.get("max_uses")
    if isinstance(value, int) and value >= 0:
        return value
    return 1


def _scopes(rec: Dict[str, Any]) -> List[str]:
    scope = rec.get("scope")
    if scope is None:
        return []
    if isinstance(scope, (list, tuple)):
        return [str(s) for s in scope]
    return [str(scope)]


class ApprovalStore:
    """Thread-safe access to ``policy/approvals.json``."""

    def __init__(self, path: Path | str, *, default_ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = Path(path)
        self.store = AtomicJsonStore(self.path, {"version": 1, "tokens": {}}, mode=0o600)
        self.default_ttl = default_ttl

    # --- read -------------------------------------------------------------
    def tokens(self) -> Dict[str, Any]:
        doc = self.store.read()
        if not isinstance(doc, dict):
            return {}
        tokens = doc.get("tokens")
        return tokens if isinstance(tokens, dict) else {}

    def describe(self, token: str) -> Optional[Dict[str, Any]]:
        """Non-secret summary of a token (the token itself is masked)."""
        rec = self.tokens().get(token)
        if not isinstance(rec, dict):
            return None
        expiry = _token_expiry(rec)
        now = _now()
        max_uses = _max_uses(rec)
        used = _uses(rec)
        return {
            "token": f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "…",
            "action": rec.get("action"),
            "scope": _scopes(rec),
            "created_at": rec.get("created_at"),
            "expires_at": expiry,
            "expired": bool(expiry is not None and expiry <= now),
            "used": used,
            "max_uses": max_uses,
            "exhausted": max_uses != 0 and used >= max_uses,
            "note": rec.get("note", ""),
        }

    def list(self) -> List[Dict[str, Any]]:
        out = [self.describe(t) for t in self.tokens()]
        return [item for item in out if item]

    # --- write ------------------------------------------------------------
    def issue(
        self,
        action: str,
        scope: str = WILDCARD,
        *,
        ttl_seconds: Optional[int] = None,
        max_uses: int = 1,
        note: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """Create a token and persist it.  Returns ``(token, record)``."""
        if action not in KNOWN_ACTIONS:
            raise ValueError(f"unknown action {action!r}; expected one of {', '.join(KNOWN_ACTIONS)}")
        if not scope:
            raise ValueError("scope is required; use '*' to authorise any value explicitly")
        token = _new_token()
        now = _now()
        ttl = self.default_ttl if ttl_seconds is None else int(ttl_seconds)
        rec: Dict[str, Any] = {
            "action": action,
            "scope": scope,
            "created_at": now,
            "ttl_seconds": ttl if ttl > 0 else None,
            "expires_at": (now + ttl) if ttl > 0 else None,
            "max_uses": int(max_uses),
            "used": 0,
            "spent": False,
            "note": note,
        }

        def mutate(doc: Any) -> Dict[str, Any]:
            document = doc if isinstance(doc, dict) else {}
            tokens = document.setdefault("tokens", {})
            if not isinstance(tokens, dict):
                tokens = {}
                document["tokens"] = tokens
            tokens[token] = rec
            document.setdefault("version", 1)
            return document

        self.store.update(mutate)
        return token, rec

    def revoke(self, token: str) -> bool:
        """Delete a token.  Returns ``True`` when something was removed."""
        removed = False

        def mutate(doc: Any) -> Dict[str, Any]:
            nonlocal removed
            document = doc if isinstance(doc, dict) else {"version": 1, "tokens": {}}
            tokens = document.get("tokens")
            if isinstance(tokens, dict) and token in tokens:
                del tokens[token]
                removed = True
            return document

        self.store.update(mutate)
        return removed

    def prune(self, *, expired_only: bool = True) -> int:
        """Drop exhausted/expired tokens.  Returns how many were removed."""
        now = _now()
        removed = 0

        def mutate(doc: Any) -> Dict[str, Any]:
            nonlocal removed
            document = doc if isinstance(doc, dict) else {"version": 1, "tokens": {}}
            tokens = document.get("tokens")
            if not isinstance(tokens, dict):
                return document
            keep: Dict[str, Any] = {}
            for key, rec in tokens.items():
                rec = rec if isinstance(rec, dict) else {}
                expiry = _token_expiry(rec)
                expired = expiry is not None and expiry <= now
                exhausted = _max_uses(rec) != 0 and _uses(rec) >= _max_uses(rec)
                if expired or (exhausted and expired_only):
                    removed += 1
                    continue
                keep[key] = rec
            document["tokens"] = keep
            return document

        self.store.update(mutate)
        return removed

    # --- validate + spend -------------------------------------------------
    def validate(self, action: str, token: Optional[str], scope: str) -> Tuple[Optional[str], Optional[str]]:
        """Check a token without spending it.

        Returns ``(error_code, detail)``; both ``None`` when the token is valid.
        """
        if not token:
            return "missing_approval_token", f"This action needs a token: bin/issue-approval.sh {action} <scope>"
        rec = self.tokens().get(token)
        if not isinstance(rec, dict):
            return "unknown_approval_token", "No such token in policy/approvals.json"
        if rec.get("action") != action:
            return (
                "approval_action_mismatch",
                f"Token authorises {rec.get('action')!r}, not {action!r}",
            )
        expiry = _token_expiry(rec)
        if expiry is not None and expiry <= _now():
            return "approval_expired", "Token TTL has elapsed; issue a new one"
        max_uses = _max_uses(rec)
        if max_uses != 0 and _uses(rec) >= max_uses:
            return "approval_exhausted", f"Token was already used {_uses(rec)}/{max_uses} time(s)"
        scopes = _scopes(rec)
        if WILDCARD not in scopes and scope not in scopes:
            return (
                "approval_scope_mismatch",
                "Token scope does not match this request (empty scopes are not wildcards)",
            )
        return None, None

    def consume(self, action: str, token: Optional[str], scope: str) -> Tuple[Optional[str], Optional[str]]:
        """Validate and atomically spend one use.

        The increment is written to disk inside the lock, so concurrent
        requests cannot both succeed with a single-use token.
        """
        with self.store.lock:
            err, detail = self.validate(action, token, scope)
            if err:
                return err, detail

            def mutate(doc: Any) -> Dict[str, Any]:
                document = doc if isinstance(doc, dict) else {"version": 1, "tokens": {}}
                tokens = document.setdefault("tokens", {})
                rec = tokens.get(token) if isinstance(tokens, dict) else None
                if isinstance(rec, dict):
                    used = _uses(rec) + 1
                    rec["used"] = used
                    rec["spent"] = _max_uses(rec) != 0 and used >= _max_uses(rec)
                    rec["last_used_at"] = _now()
                return document

            self.store.update(mutate)
            return None, None


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: ``python3 -m lib.dai.approvals {issue,list,revoke,prune,validate}``."""
    parser = argparse.ArgumentParser(prog="dai-approvals", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--file",
        default=str(Path(__file__).resolve().parents[2] / "policy" / "approvals.json"),
        help="approvals file (default: policy/approvals.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="create a one-shot approval token")
    p_issue.add_argument("action", choices=KNOWN_ACTIONS)
    p_issue.add_argument("scope", help="exact value to authorise, or '*' for any")
    p_issue.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="seconds until expiry (0 = never)")
    p_issue.add_argument("--max-uses", type=int, default=1, help="0 = unlimited")
    p_issue.add_argument("--note", default="", help="free-text reminder")

    sub.add_parser("list", help="list tokens (masked)")
    p_revoke = sub.add_parser("revoke", help="delete a token")
    p_revoke.add_argument("token")
    sub.add_parser("prune", help="remove expired/exhausted tokens")
    p_validate = sub.add_parser("validate", help="check a token without spending it")
    p_validate.add_argument("action", choices=KNOWN_ACTIONS)
    p_validate.add_argument("token")
    p_validate.add_argument("scope")

    args = parser.parse_args(argv)
    store = ApprovalStore(args.file)

    if args.command == "issue":
        token, rec = store.issue(args.action, args.scope, ttl_seconds=args.ttl, max_uses=args.max_uses, note=args.note)
        # The token is the one secret this CLI prints — that is its purpose.
        print(token)
        print(
            json.dumps(
                {
                    "action": rec["action"],
                    "scope": rec["scope"],
                    "expires_at": rec["expires_at"],
                    "max_uses": rec["max_uses"],
                    "file": str(store.path),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 0
    if args.command == "list":
        print(json.dumps(store.list(), indent=2))
        return 0
    if args.command == "revoke":
        print(json.dumps({"revoked": store.revoke(args.token)}))
        return 0
    if args.command == "prune":
        print(json.dumps({"removed": store.prune()}))
        return 0
    if args.command == "validate":
        err, detail = store.validate(args.action, args.token, args.scope)
        print(json.dumps({"ok": err is None, "error": err, "detail": detail}, indent=2))
        return 0 if err is None else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
