"""Business config loader with dot-notation access.

Loads a YAML config file and returns a Config object whose .get() method
walks nested keys via a dotted path (e.g. "creatomate.equipment_post_image.id").
Business-agnostic: works with any YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


load_dotenv(override=False)


class _Unset:
    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET: Any = _Unset()


class Config:
    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def raw(self) -> dict:
        return self._data

    def get(self, dotted_path: str, default: Any = _UNSET) -> Any:
        segments = dotted_path.split(".")
        node: Any = self._data
        traversed: list[str] = []
        for segment in segments:
            traversed.append(segment)
            if isinstance(node, dict) and segment in node:
                node = node[segment]
            else:
                if default is _UNSET:
                    failed = ".".join(traversed)
                    raise KeyError(
                        f"Config path '{dotted_path}' not found "
                        f"(failed at segment '{failed}')"
                    )
                return default
        return node

    def __contains__(self, dotted_path: str) -> bool:
        try:
            self.get(dotted_path)
        except KeyError:
            return False
        return True


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        env_path = os.environ.get("BUSINESS_CONFIG_PATH")
        path = env_path if env_path else "business_config.yaml"

    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Business config file not found: {config_path} "
            f"(resolved to {config_path.resolve()})"
        )

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file {config_path} did not parse to a mapping "
            f"(got {type(data).__name__})"
        )

    return Config(data)
