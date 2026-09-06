"""
Environment variable loading and validation.

Reads all config from os.environ (works with both .env files and
Railway-injected env vars). Fails fast with a clear error listing every
missing variable — the app must never boot silently with partial config.
"""

import json
import base64
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env if it exists (no-op in production where Railway injects vars)
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Immutable settings loaded from environment variables."""

    # Google Drive (required)
    drive_folder_id: str
    service_account_info: dict  # parsed service account JSON

    # Gemini (required)
    gemini_api_key: str

    # GitHub (required)
    github_token: str
    github_repo: str  # "owner/repo"

    # Optional (with defaults)
    gemini_model: str = "gemini-2.5-flash-lite"
    github_branch: str = "main"
    image_assets_path: str = "images"

    @property
    def github_owner(self) -> str:
        return self.github_repo.split("/")[0]

    @property
    def github_repo_name(self) -> str:
        return self.github_repo.split("/")[1]

    @property
    def raw_image_base_url(self) -> str:
        """Base URL for image assets on raw.githubusercontent.com."""
        return (
            f"https://raw.githubusercontent.com/"
            f"{self.github_owner}/{self.github_repo_name}/"
            f"{self.github_branch}/{self.image_assets_path}"
        )


# All required env var names — used for the fail-fast check
_REQUIRED_VARS = [
    "GOOGLE_DRIVE_FOLDER_ID",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
]


def _load_service_account() -> dict:
    """
    Load the Google service account credentials.

    Supports two modes:
    1. GOOGLE_SERVICE_ACCOUNT_JSON — path to a JSON file on disk
    2. GOOGLE_SERVICE_ACCOUNT_B64 — base64-encoded JSON (for Railway env vars)
    """
    # Try base64-encoded first (Railway-friendly)
    b64_val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64")
    if b64_val:
        try:
            decoded = base64.b64decode(b64_val).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            raise ValueError(
                f"GOOGLE_SERVICE_ACCOUNT_B64 is set but could not be decoded: {e}"
            )

    # Try file path
    json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_path:
        path = Path(json_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Service account file not found at: {json_path}\n"
                f"Set GOOGLE_SERVICE_ACCOUNT_JSON to a valid path, or "
                f"set GOOGLE_SERVICE_ACCOUNT_B64 with base64-encoded JSON."
            )
        with open(path, "r") as f:
            return json.load(f)

    raise ValueError(
        "No service account credentials found.\n"
        "Set either GOOGLE_SERVICE_ACCOUNT_JSON (file path) or "
        "GOOGLE_SERVICE_ACCOUNT_B64 (base64-encoded JSON)."
    )


def load_settings() -> Settings:
    """
    Load and validate all settings from environment variables.

    Raises SystemExit with a clear error message if any required
    variable is missing or invalid.
    """
    # Check for missing required vars
    missing = [var for var in _REQUIRED_VARS if not os.environ.get(var, "").strip()]

    # Also check that at least one service account method is set
    has_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    has_sa_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64", "").strip()
    if not has_sa_json and not has_sa_b64:
        missing.append(
            "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_B64"
        )

    if missing:
        msg = (
            "❌ Missing required environment variables:\n"
            + "\n".join(f"  • {var}" for var in missing)
            + "\n\nPlease set these in your .env file or Railway dashboard."
        )
        print(msg, file=sys.stderr)
        raise SystemExit(msg)

    # Load service account
    try:
        sa_info = _load_service_account()
    except (ValueError, FileNotFoundError) as e:
        print(f"❌ {e}", file=sys.stderr)
        raise SystemExit(str(e))

    # Validate GITHUB_REPO format
    github_repo = os.environ["GITHUB_REPO"].strip()
    if "/" not in github_repo or github_repo.count("/") != 1:
        msg = (
            f"❌ GITHUB_REPO must be in 'owner/repo' format, got: '{github_repo}'"
        )
        print(msg, file=sys.stderr)
        raise SystemExit(msg)

    return Settings(
        drive_folder_id=os.environ["GOOGLE_DRIVE_FOLDER_ID"].strip(),
        service_account_info=sa_info,
        gemini_api_key=os.environ["GEMINI_API_KEY"].strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip(),
        github_token=os.environ["GITHUB_TOKEN"].strip(),
        github_repo=github_repo,
        github_branch=os.environ.get("GITHUB_BRANCH", "main").strip(),
        image_assets_path=os.environ.get("IMAGE_ASSETS_PATH", "images").strip(),
    )
