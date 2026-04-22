"""Configuration loading from TOML file."""

from __future__ import annotations

import dataclasses
import logging
import os
import tomllib

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/claude-code-tmux-notify/config.toml")


@dataclasses.dataclass
class HookServerConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 19836
    ttl: float = 30.0
    require_hook: bool = False  # True = 双确认模式


@dataclasses.dataclass
class PopupConfig:
    width: str = "80%"
    height: str = "60%"
    x: str = "R"  # R = right side
    y: str = "0"  # 0 = top


@dataclasses.dataclass
class TriggerScenario:
    patterns: list[str] = dataclasses.field(default_factory=list)
    keywords: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class TriggersConfig:
    permission: TriggerScenario = dataclasses.field(default_factory=TriggerScenario)
    plan: TriggerScenario = dataclasses.field(default_factory=TriggerScenario)
    completed: TriggerScenario = dataclasses.field(default_factory=TriggerScenario)


def default_triggers() -> TriggersConfig:
    return TriggersConfig(
        permission=TriggerScenario(
            patterns=[r"Do you want to .*\?", r"Would you like to .*\?"],
            keywords=["Do you want to", "Would you like to"],
        ),
        plan=TriggerScenario(
            patterns=[],
            keywords=["approve this plan", "approve the plan"],
        ),
        completed=TriggerScenario(
            patterns=[r"(Brewed|Crunched|Swooped|Drizzled) for\s+"],
            keywords=[],
        ),
    )


@dataclasses.dataclass
class Config:
    popup: PopupConfig = dataclasses.field(default_factory=PopupConfig)
    hook_server: HookServerConfig = dataclasses.field(default_factory=HookServerConfig)
    triggers: TriggersConfig = dataclasses.field(default_factory=default_triggers)
    buffer_lines: int = 100


def _parse_trigger_scenario(data: dict) -> TriggerScenario:
    return TriggerScenario(
        patterns=[str(p) for p in data.get("patterns", [])],
        keywords=[str(k) for k in data.get("keywords", [])],
    )


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

    triggers_data = data.get("triggers", {})
    defaults = default_triggers()
    cfg.triggers = TriggersConfig(
        permission=_parse_trigger_scenario(triggers_data["permission"])
        if "permission" in triggers_data
        else defaults.permission,
        plan=_parse_trigger_scenario(triggers_data["plan"])
        if "plan" in triggers_data
        else defaults.plan,
        completed=_parse_trigger_scenario(triggers_data["completed"])
        if "completed" in triggers_data
        else defaults.completed,
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
        if "require_hook" in hook:
            cfg.hook_server.require_hook = bool(hook["require_hook"])

    return cfg
