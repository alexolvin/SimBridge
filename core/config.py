"""Unified configuration: single YAML file, strict validation, no silent defaults.

Config is loaded from the path in ``SIMBRIDGE_CONFIG`` env var (or
``/etc/simbridge/simbridge.yaml``). Secrets are never stored in the YAML —
the schema references env var names; ``load_config`` raises if they are unset.

Usage::

    cfg = load_config("/etc/simbridge/simbridge.yaml")
    agent_token = os.environ[cfg["agent.token_env"]]
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# DotDict — dict with dot-access, used so that config references read like
# ``cfg["node.role"]`` instead of ``cfg["node"]["role"]``.
# ---------------------------------------------------------------------------

class DotDict(dict):
    """dict subclass with ``d["a.b.c"]`` access."""

    def __getitem__(self, key: str) -> Any:
        # Try direct key first (handles keys that literally contain dots)
        if key in self:
            return super().__getitem__(key)
        # Try dotted path access
        if "." in key:
            parts = key.split(".", 1)
            sub = super().__getitem__(parts[0])
            if isinstance(sub, dict):
                dd = DotDict(sub)
                return dd[parts[1]]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict:
        """Return a plain nested dict (for serialization)."""
        result: dict = {}
        for k, v in super().items():
            result[k] = v.to_dict() if isinstance(v, DotDict) else v
        return result


def _to_dot_dict(d: dict) -> DotDict:
    return DotDict(
        {k: _to_dot_dict(v) if isinstance(v, dict) else v for k, v in d.items()}
    )


# ---------------------------------------------------------------------------
# Env var expansion — handles $VAR and ${VAR}, leaves unknown refs intact.
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(
    r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'  # ${VAR}
    r'|\$([A-Za-z_][A-Za-z0-9_]*)'      # $VAR
)


def _split_listen(value: str) -> tuple[str, str]:
    """Split a ``host:port`` string into (host, port). Returns ("", "") on failure."""
    try:
        host, port = value.rsplit(":", 1)
        return (host, port)
    except (ValueError, TypeError):
        return ("", "")


def _expand(obj: Any) -> Any:
    """Expand ``$VAR`` / ``${VAR}`` references in string values.

    Unknown variables are left as-is (not expanded) so that literal ``$``
    in paths like ``$HOME`` don't silently vanish.
    """
    if isinstance(obj, str):
        def _repl(m: re.Match) -> str:
            var_name = m.group(1) or m.group(2)
            return os.environ.get(var_name, m.group(0))
        return _ENV_RE.sub(_repl, obj)
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Schema — declarative: required paths, types, and env-secret references.
# ---------------------------------------------------------------------------

@dataclass
class _SchemaEntry:
    key: str          # dotted path, e.g. "node.role"
    type: type = str
    required: bool = True
    env: bool = False     # if True, this key holds an env var NAME, not the value
    enum: Optional[list] = None
    roles: tuple = ()     # if non-empty: entry is required only for these node.roles


def _is_required(entry: _SchemaEntry, role: Optional[str]) -> bool:
    """Whether *entry* is required for the given node role.

    Entries with a non-empty ``roles`` tuple are required only when the
    node's role matches (a GSM node does not need Telegram credentials,
    a Telegram node does not need the AMI password). Otherwise the plain
    ``required`` flag applies.
    """
    if entry.roles:
        return role in entry.roles
    return entry.required


_CONFIG_SCHEMA: list[_SchemaEntry] = [
    # -- node --
    _SchemaEntry("node.role", str, enum=["all-in-one", "gsm", "telegram"]),
    _SchemaEntry("node.id", str),
    # -- telegram --
    _SchemaEntry("telegram.master_username", str),
    _SchemaEntry("telegram.session_path", str),
    _SchemaEntry("telegram.acl_file", str),
    # Required for roles that run the userbot (Telegram credentials).
    _SchemaEntry("telegram.api_id_env", str, env=True, required=False,
                 roles=("telegram", "all-in-one")),
    _SchemaEntry("telegram.api_hash_env", str, env=True, required=False,
                 roles=("telegram", "all-in-one")),
    # -- agent --
    _SchemaEntry("agent.listen", str),
    _SchemaEntry("agent.token_env", str, env=True),
    _SchemaEntry("agent.allowed_peers", list, required=False),
    # Base URL of the userbot HTTP server — the agent POSTs delivery
    # notifications here after matching a carrier report.
    _SchemaEntry("agent.userbot_url", str),
    # -- userbot_http --
    _SchemaEntry("userbot_http.listen", str),
    # Required for roles that run the userbot HTTP server (event receiver).
    _SchemaEntry("userbot_http.secret_env", str, env=True, required=False,
                 roles=("telegram", "all-in-one")),
    _SchemaEntry("userbot_http.allowed_peers", list, required=False),
    # -- asterisk --
    _SchemaEntry("asterisk.ari_url", str),
    _SchemaEntry("asterisk.dongle", str),
    _SchemaEntry("asterisk.ring_wait_seconds", int),
    _SchemaEntry("asterisk.max_record_seconds", int),
    # S03.1: recording shorter than this means the caller hung up right
    # after the greeting (early_hangup). Default 3 (voicemail_forward).
    _SchemaEntry("asterisk.early_hangup_max_seconds", int, required=False),
    # Sweep timer: a *.wav older than this is an orphan, safe to forward.
    _SchemaEntry("asterisk.sweep_max_age_seconds", int, required=False),
    _SchemaEntry("asterisk.prompt", str),
    _SchemaEntry("asterisk.ami_host", str, required=False),
    _SchemaEntry("asterisk.ami_port", int, required=False),
    _SchemaEntry("asterisk.ami_username", str, required=False),
    # Required for roles that run the agent (AMI access to Asterisk).
    _SchemaEntry("asterisk.ami_password_env", str, env=True, required=False,
                 roles=("gsm", "all-in-one")),
    # -- sim --
    _SchemaEntry("sim.phone", str, required=False),
    _SchemaEntry("sim.modem_model", str, required=False),
    # -- voice --
    _SchemaEntry("voice.bridge_endpoint", str),
    _SchemaEntry("voice.bridge_host", str),
    _SchemaEntry("voice.bridge_port", int),
    _SchemaEntry("voice.srtp", bool),
    _SchemaEntry("voice.outbound_answer_timeout", int),
    # -- limits --
    _SchemaEntry("limits.sms_per_hour", int),
    _SchemaEntry("limits.calls_per_minute", int),
    _SchemaEntry("limits.max_call_seconds", int),
    # -- paths --
    _SchemaEntry("paths.blacklist", str),
    _SchemaEntry("paths.contacts_cache", str),
    _SchemaEntry("paths.audit_log", str),
    _SchemaEntry("paths.recordings_dir", str, required=False),
    # JSONL log of SMS correlation records (delivery reports survive restarts)
    _SchemaEntry("paths.sms_correlation", str),
]


class ConfigError(Exception):
    """Raised when configuration validation fails."""


def _validate(cfg: DotDict, schema: list[_SchemaEntry]) -> list[str]:
    """Validate *cfg* against *schema*. Return list of error strings (empty = OK)."""
    errors: list[str] = []
    role: Optional[str] = cfg.get("node.role")

    # Build a set of all known dotted keys and their top-level prefixes
    known_keys: set[str] = {e.key for e in schema}
    known_top: set[str] = {e.key.split(".")[0] for e in schema}

    for entry in schema:
        parts = entry.key.split(".")
        obj: Any = cfg
        found = True
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                found = False
                break

        if not found:
            if _is_required(entry, role):
                errors.append(f"Missing required key: {entry.key}")
            continue

        if entry.type and obj is not None and not isinstance(obj, entry.type):
            errors.append(
                f"Key {entry.key}: expected {entry.type.__name__}, "
                f"got {type(obj).__name__}"
            )

        if entry.enum is not None and obj not in entry.enum:
            errors.append(
                f"Key {entry.key}: value {obj!r} not in allowed {entry.enum}"
            )

    # Check for unknown keys at all nesting levels (recursive walk)
    def _check_unknown(d: dict, prefix: str) -> None:
        for key in d:
            dotted = f"{prefix}.{key}" if prefix else key
            if dotted not in known_keys:
                # Check if any known key starts with this prefix (sub-key of known)
                is_sub_of_known = any(
                    k.startswith(dotted + ".") or k == dotted
                    for k in known_keys
                )
                if not is_sub_of_known:
                    errors.append(f"Unknown key: {dotted}")
            if isinstance(d[key], dict):
                _check_unknown(d[key], dotted)

    _check_unknown(cfg, "")

    return errors


def _redact(cfg: DotDict) -> dict:
    """Return a deep-copy dict with secret env-var values replaced by ``<env:NAME>``."""
    import copy

    result: dict = copy.deepcopy(cfg.to_dict())

    # Collect env-ref keys and build the nested path to each
    env_ref_keys = {e.key for e in _CONFIG_SCHEMA if e.env}

    for dotted_key in env_ref_keys:
        parts = dotted_key.split(".")
        # Navigate to the parent dict
        obj: Any = result
        for part in parts[:-1]:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                obj = None
                break
        # Replace the leaf value
        if obj is not None and isinstance(obj, dict):
            leaf = parts[-1]
            if leaf in obj and isinstance(obj[leaf], str):
                obj[leaf] = f"<env:{obj[leaf]}>"

    return result


def load_config(path: Optional[str] = None) -> DotDict:
    """Load and validate a SimBridge config file.

    Raises ``ConfigError`` if required keys are missing, types are wrong,
    or env-referenced secrets are not set.
    """
    if path is None:
        path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")

    if not os.path.isfile(path):
        raise ConfigError(f"Config file not found: {path}")

    with open(path) as fh:
        raw: dict = yaml.load(fh, Loader=yaml.SafeLoader)

    # Expand env var references in string values
    raw = _expand(raw)
    cfg = _to_dot_dict(raw)

    role: Optional[str] = cfg.get("node.role")

    # Structural validation
    errors = _validate(cfg, _CONFIG_SCHEMA)
    if errors:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(errors))

    # Verify all env-referenced secrets are actually set in the environment
    for entry in _CONFIG_SCHEMA:
        if not entry.env:
            continue
        parts = entry.key.split(".")
        obj: Any = cfg
        found = True
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                found = False
                break
        if not found:
            continue

        env_name = obj
        if isinstance(env_name, str) and env_name not in os.environ:
            if _is_required(entry, role):
                errors.append(
                    f"Secret env var {env_name!r} (referenced by {entry.key}) is not set"
                )

    # S06.1: Validate bind addresses — refuse 0.0.0.0 (binds all interfaces)
    for listen_key in ("agent.listen", "userbot_http.listen"):
        parts = listen_key.split(".")
        obj: Any = cfg
        found = True
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                found = False
                break
        if not found:
            continue

        if isinstance(obj, str):
            host, _ = _split_listen(obj)
            if host == "0.0.0.0":
                errors.append(
                    f"Key {listen_key}: bind address 0.0.0.0 binds all interfaces. "
                    f"Use the Tailscale interface or 127.0.0.1 instead."
                )

    if errors:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(errors))

    return cfg


def redact_config(cfg: DotDict) -> dict:
    """Return config dict with secrets replaced by ``<env:NAME>``."""
    return _redact(cfg)
