"""Central config. Every integration is optional: a missing key switches that
integration into a safe fallback mode instead of crashing the app."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# LLM (agent brain)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
LLM_PROVIDER = env("LLM_PROVIDER", "bedrock" if (env("BEDROCK_MODEL") and not ANTHROPIC_API_KEY) else "anthropic")
MODEL = env("JARVIS_MODEL", "claude-sonnet-5-5")
JUDGE_MODEL = env("JUDGE_MODEL", "claude-haiku-5-5")
BEDROCK_MODEL = env("BEDROCK_MODEL")
BEDROCK_JUDGE_MODEL = env("BEDROCK_JUDGE_MODEL", BEDROCK_MODEL)
AWS_REGION = env("AWS_REGION", "us-east-1")

# Rough price table for the live cost meter (USD per million tokens)
PRICE_IN = float(env("PRICE_IN_PER_MTOK", "3"))
PRICE_OUT = float(env("PRICE_OUT_PER_MTOK", "15"))

# Daytona
DAYTONA_API_KEY = env("DAYTONA_API_KEY")
SANDBOX_NETWORK_BLOCK = env("SANDBOX_NETWORK_BLOCK", "true").lower() == "true"

# Anchor Browser
ANCHOR_API_KEY = env("ANCHOR_BROWSER_API_KEY") or env("ANCHOR_API_KEY") or env("ANCHORBROWSER_API_KEY")
# Public URL of THIS app (e.g. a Cloudflare quick tunnel) so Anchor's cloud browser can reach the docs site
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")

# ElevenLabs
ELEVENLABS_API_KEY = env("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # "George", British
ELEVENLABS_MODEL = env("ELEVENLABS_MODEL", "eleven_flash_v2_5")

# Arize AX
ARIZE_SPACE_ID = env("ARIZE_SPACE_ID")
ARIZE_API_KEY = env("ARIZE_API_KEY")
ARIZE_PROJECT = env("ARIZE_PROJECT", "jarvis-dispatch")
ARIZE_TRANSPORT = env("ARIZE_TRANSPORT", "grpc").lower()

# SurrealDB (embedded by default, no account needed)
SURREAL_URL = env("SURREAL_URL", f"surrealkv://{ROOT / 'data' / 'jarvis.db'}")
SURREAL_USER = env("SURREAL_USER")
SURREAL_PASS = env("SURREAL_PASS")
SURREAL_NS = env("SURREAL_NS", "jarvis")
SURREAL_DB = env("SURREAL_DB", "ops")

# Safety budget
JUDGE_ENABLED = env("JUDGE_ENABLED", "true").lower() == "true"



def llm_ready() -> bool:
    if LLM_PROVIDER == "bedrock":
        return bool(BEDROCK_MODEL)
    return bool(ANTHROPIC_API_KEY)
