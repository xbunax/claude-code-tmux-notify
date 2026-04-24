"""Configuration loading from TOML file."""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/agent-tmux-notify/config.toml")


@dataclasses.dataclass
class HookServerConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 19836
    ttl: float = 30.0
    dump_payloads: bool = False
    dump_path: str = "/tmp/claude-code-hook-payloads.jsonl"


@dataclasses.dataclass
class PopupConfig:
    width: str = "80%"
    height: str = "60%"
    x: str = "R"  # R = right side
    y: str = "0"  # 0 = top


@dataclasses.dataclass
class ParseRuleConfig:
    patterns: list[str] = dataclasses.field(default_factory=list)
    keywords: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ParseRulesConfig:
    permission: ParseRuleConfig = dataclasses.field(default_factory=ParseRuleConfig)
    plan: ParseRuleConfig = dataclasses.field(default_factory=ParseRuleConfig)


def default_parse_rules() -> ParseRulesConfig:
    return ParseRulesConfig(
        permission=ParseRuleConfig(
            patterns=[r"Do you want to .*\?", r"Would you like to .*\?"],
            keywords=["Do you want to", "Would you like to"],
        ),
        plan=ParseRuleConfig(
            patterns=[],
            keywords=["approve this plan", "approve the plan"],
        ),
    )


def _parse_rule_config(data: dict) -> ParseRuleConfig:
    return ParseRuleConfig(
        patterns=[str(p) for p in data.get("patterns", [])],
        keywords=[str(k) for k in data.get("keywords", [])],
    )


@dataclasses.dataclass
class Config:
    popup: PopupConfig = dataclasses.field(default_factory=PopupConfig)
    hook_server: HookServerConfig = dataclasses.field(default_factory=HookServerConfig)
    parse_rules: ParseRulesConfig = dataclasses.field(default_factory=default_parse_rules)
    buffer_lines: int = 100


def load_config(path: str | None = None) -> Config:
    """Load config from TOML file. Returns defaults if file missing or invalid."""
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        log.debug("No config file at %s, using defaults", path)
        return Config()

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        log.warning("Failed to parse config %s, using defaults", path, exc_info=True)
        return Config()

    cfg = Config()

    popup = data.get("popup", {})
    if popup:
        for field in ("width", "height", "x", "y"):
            if field in popup:
                setattr(cfg.popup, field, str(popup[field]))

    # New name: parse_rules. Legacy name: triggers.
    rules_data = data.get("parse_rules")
    if rules_data is None:
        rules_data = data.get("triggers", {})
    defaults = default_parse_rules()
    cfg.parse_rules = ParseRulesConfig(
        permission=_parse_rule_config(rules_data["permission"])
        if isinstance(rules_data, dict) and "permission" in rules_data
        else defaults.permission,
        plan=_parse_rule_config(rules_data["plan"])
        if isinstance(rules_data, dict) and "plan" in rules_data
        else defaults.plan,
    )

    if "buffer_lines" in data:
        cfg.buffer_lines = int(data["buffer_lines"])

    hook = data.get("hook_server", {})
    if hook:
        if "enabled" in hook:
            cfg.hook_server.enabled = bool(hook["enabled"])
        if "host" in hook:
            cfg.hook_server.host = str(hook["host"])
        if "port" in hook:
            cfg.hook_server.port = int(hook["port"])
        if "ttl" in hook:
            cfg.hook_server.ttl = float(hook["ttl"])
        if "dump_payloads" in hook:
            cfg.hook_server.dump_payloads = bool(hook["dump_payloads"])
        if "dump_path" in hook:
            cfg.hook_server.dump_path = str(hook["dump_path"])

    return cfg
