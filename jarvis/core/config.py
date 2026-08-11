"""Configuration loading, merging, and persistence."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir, user_data_dir

from jarvis import __app_name__

_LOCK = threading.RLock()
_CACHE: AppConfig | None = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "default.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_ENV_SECTIONS = (
    "wake_word", "permissions", "assistant", "planner", "memory",
    "logging", "hotkeys", "audio", "paths", "stt", "tts", "llm", "web", "ui",
)


def _coerce_env(raw_val: str):
    low = raw_val.lower()
    if low in {"true", "false"}:
        return low == "true"
    try:
        return int(raw_val)
    except ValueError:
        try:
            return float(raw_val)
        except ValueError:
            return raw_val


def _env_overrides() -> dict:
    """Map JARVIS_<section>_<key> to nested config. Example: JARVIS_LLM_MODEL=phi3."""
    mapping: dict[str, Any] = {}
    prefix = "JARVIS_"
    for raw_key, raw_val in os.environ.items():
        if not raw_key.startswith(prefix) or raw_key == "JARVIS_CONFIG":
            continue
        rest = raw_key[len(prefix):].lower()
        matched = False
        for section in sorted(_ENV_SECTIONS, key=len, reverse=True):
            token = section + "_"
            if rest.startswith(token):
                mapping.setdefault(section, {})[rest[len(token):]] = _coerce_env(raw_val)
                matched = True
                break
        if not matched:
            continue
    return mapping


def config_path() -> Path:
    explicit = os.environ.get("JARVIS_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(user_config_dir(__app_name__.lower(), appauthor=False)) / "config.yaml"


def data_dir(cfg: dict | None = None) -> Path:
    configured = ""
    if cfg:
        configured = (cfg.get("paths") or {}).get("data_dir") or ""
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(user_data_dir(__app_name__.lower(), appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must be a mapping: {path}")
    return loaded


class AppConfig:
    """Mutable nested configuration with attribute and item access."""

    def __init__(self, data: dict):
        self._data = data

    def raw(self) -> dict:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cursor = self._data
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value

    def section(self, name: str) -> dict:
        value = self._data.get(name, {})
        return value if isinstance(value, dict) else {}

    def update_from(self, patch: dict) -> None:
        self._data = _deep_merge(self._data, patch)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data


def load_config(reload: bool = False) -> AppConfig:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not reload:
            return _CACHE
        defaults = _load_yaml(DEFAULT_CONFIG_FILE)
        user = _load_yaml(config_path())
        merged = _deep_merge(defaults, user)
        merged = _deep_merge(merged, _env_overrides())
        # Resolve data dir now so the rest of the app can rely on it.
        resolved = str(data_dir(merged))
        merged.setdefault("paths", {})["data_dir"] = resolved
        _CACHE = AppConfig(merged)
        return _CACHE


def save_config(cfg: AppConfig | dict | None = None) -> Path:
    if cfg is None:
        cfg = load_config()
    data = cfg.raw() if isinstance(cfg, AppConfig) else cfg
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not persist resolved absolute machine data_dir if it was defaulted.
    persist = copy.deepcopy(data)
    to_write = persist
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(to_write, handle, sort_keys=False, allow_unicode=True)
    load_config(reload=True)
    return path
