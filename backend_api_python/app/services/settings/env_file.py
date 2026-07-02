"""Read and update backend .env files."""

from __future__ import annotations

import os
from typing import Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
ENV_FILE_PATH = os.path.join(BACKEND_DIR, ".env")


def read_env_file(path: str = ENV_FILE_PATH) -> Dict[str, str]:
    """Read key/value pairs from an .env file."""
    env_values: Dict[str, str] = {}

    if not os.path.exists(path):
        logger.warning(".env file not found at %s", path)
        return env_values

    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                env_values[key] = value
    except Exception as exc:
        logger.error("Failed to read .env file: %s", exc)

    return env_values


def write_env_file(env_values: Dict[str, str], path: str = ENV_FILE_PATH) -> bool:
    """Write .env values while preserving existing comments and formatting."""
    lines = []
    existing_keys = set()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    original_line = line
                    stripped = line.strip()

                    if not stripped or stripped.startswith("#"):
                        lines.append(original_line)
                        continue

                    if "=" in stripped:
                        key = stripped.split("=", 1)[0].strip()
                        if key in env_values:
                            existing_keys.add(key)
                            lines.append(_format_env_line(key, env_values[key]))
                        else:
                            lines.append(original_line)
                    else:
                        lines.append(original_line)
        except Exception as exc:
            logger.error("Failed to read .env file for update: %s", exc)

    new_keys = set(env_values.keys()) - existing_keys
    if new_keys:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# Added by Settings UI\n")
        for key in sorted(new_keys):
            lines.append(_format_env_line(key, env_values[key]))

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return True
    except Exception as exc:
        logger.error("Failed to write .env file: %s", exc)
        return False


def _format_env_line(key: str, value) -> str:
    text = str(value)
    if " " in text or '"' in text or "'" in text:
        return f'{key}="{text}"\n'
    return f"{key}={text}\n"
