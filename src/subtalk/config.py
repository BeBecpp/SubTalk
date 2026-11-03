"""Configuration: config/default.yaml < config/local.yaml < SUBTALK_CONFIG file.

Secrets never live in YAML: they come from the process environment or the
gitignored ``.env`` file (ANTHROPIC_API_KEY or CLAUDE_KEY, SUBTALK_AUDIO_TOKEN).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from subtalk.state import JaPolicy


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    session_hours: float = 16.0
    lease_grace_s: float = 15.0
    # Extra origins allowed to send commands, e.g. "b-name.tailnet.ts.net" when a proxy rewrites Host.
    allowed_origins: list[str] = Field(default_factory=list)


class ZoomCfg(BaseModel):
    allow_real_zoom: bool = False
    allow_event_target: bool = False
    attempt_timeout_s: float = 1.5
    retry_budget_s: float = 5.0
    backoff_base_s: float = 0.1
    caption_ttl_s: float = 12.0
    manual_ttl_s: float = 30.0
    max_chars: int = 84
    line_breaks: bool = False
    line_chars: int = 42
    bilingual_labels: bool = True  # "TR: ... / EN: ..." prefixes in BILINGUAL mode
    output_mode: Literal["TR", "EN", "BILINGUAL"] = "TR"  # initial mode; the operator can switch
    min_interval_s: float = 1.0
    queue_max: int = 8
    auto_pause_after_failures: int = 3


class AudioCfg(BaseModel):
    sample_rate: int = 16000
    lost_after_s: float = 2.0
    flatline_after_s: float = 2.0
    flatline_dbfs: float = -90.0


class VadCfg(BaseModel):
    engine: Literal["silero", "energy"] = "energy"
    silero_model_path: str | None = None
    on_threshold: float = 0.5
    off_threshold: float = 0.35
    energy_margin_db: float = 12.0
    min_silence_ms: int = 650
    max_segment_s: float = 9.0
    min_segment_s: float = 0.4
    preroll_ms: int = 300
    postroll_ms: int = 200


class AsrCfg(BaseModel):
    # mock: no audio needed, the mock session timeline drives the pipeline
    # scripted: real audio + VAD, scripted transcripts (dev audio path test)
    # whisper: faster-whisper in a worker process (production)
    engine: Literal["mock", "scripted", "whisper"] = "mock"
    model: str = "large-v3"
    model_dir: str = "models"
    device: str = "cuda"
    compute_type: str = "float16"
    beam_size: int = 5
    fallback_model: str | None = "medium"
    initial_prompt_mn: str = ""
    initial_prompt_ja: str = ""
    queue_max: int = 4


class LangIdCfg(BaseModel):
    min_duration_s: float = 1.0
    short_duration_s: float = 2.0
    margin_ok: float = 0.3
    margin_short: float = 0.5
    second_pass_margin: float = 0.6
    logprob_conflict: float = 0.25
    min_avg_logprob_ok: float = -0.8
    nonspeech_prob: float = 0.6
    compression_max: float = 2.4
    overlap_script_ratio: float = 0.2


class PriceCfg(BaseModel):
    input: float
    output: float
    cache_read: float
    cache_write: float


class TranslateCfg(BaseModel):
    engine: Literal["mock", "claude"] = "mock"
    # Owner's choice: the middle tier (Sonnet) as primary, Haiku as the fast timeout fallback.
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    fallback_model: str | None = "claude-haiku-5-5"
    # Turkish + English: one request returning both (default) or one request per language.
    dual_mode: Literal["single_request", "parallel"] = "single_request"
    server_fallbacks: bool = True
    timeout_s: float = 5.0
    fallback_timeout_s: float = 4.0
    max_tokens: int = 2000
    concurrency: int = 3
    recent_context: int = 6
    # Per engine process (in memory). Set the same limit in the Anthropic Console as the real cap.
    budget_soft_usd: float = 15.0
    budget_hard_usd: float = 20.0
    prices: dict[str, PriceCfg] = Field(default_factory=lambda: {
        "claude-opus-5-5": PriceCfg(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
        "claude-sonnet-5-5": PriceCfg(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
        "claude-haiku-5-5": PriceCfg(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    })


class InterpCfg(BaseModel):
    ja_policy: JaPolicy = JaPolicy.AUTO
    auto_matching_enabled: bool = True
    match_window_s: float = 90.0
    hold_timeout_s: float = 20.0
    wait_pending_s: float = 3.0
    history_max: int = 40


class OrderingCfg(BaseModel):
    head_timeout_s: float = 8.0


class PathsCfg(BaseModel):
    glossary_dir: str = "resources/glossary"
    bible_dir: str = "resources/bible"
    event_dir: str = "event"
    state_dir: str = "state"
    log_dir: str = "logs"
    secrets_dir: str = "secrets"


class ViewerCfg(BaseModel):
    """Guest caption viewer (each guest picks Turkish or English)."""
    enabled: bool = False          # publish captions to viewers (local page + relay)
    local_page: bool = True        # serve /viewer + /ws/viewer on this engine (LAN / Tailscale)
    relay_url: str = ""            # public relay (Cloudflare Worker); token: SUBTALK_RELAY_TOKEN
    relay_timeout_s: float = 3.0
    relay_max_age_s: float = 20.0  # never deliver a caption to the relay later than this
    recent_max: int = 30


class MockCfg(BaseModel):
    session_file: str = "resources/mock/session.yaml"
    loop: bool = True
    audio_level: bool = True


class Config(BaseModel):
    mode: Literal["mock", "production"] = "mock"
    event_name: str = "Galkhuu & Akane"
    server: ServerCfg = ServerCfg()
    zoom: ZoomCfg = ZoomCfg()
    audio: AudioCfg = AudioCfg()
    vad: VadCfg = VadCfg()
    asr: AsrCfg = AsrCfg()
    langid: LangIdCfg = LangIdCfg()
    translate: TranslateCfg = TranslateCfg()
    interp: InterpCfg = InterpCfg()
    ordering: OrderingCfg = OrderingCfg()
    paths: PathsCfg = PathsCfg()
    mock: MockCfg = MockCfg()
    viewer: ViewerCfg = ViewerCfg()

    # resolved at load time, not from YAML
    root: Path = Path(".")

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(root: Path | None = None, extra: Path | None = None) -> Config:
    root = Path(root or os.environ.get("SUBTALK_HOME") or Path.cwd()).resolve()
    load_dotenv(root / ".env")
    data: dict = {}
    for p in (root / "config" / "default.yaml", root / "config" / "local.yaml",
              extra or (Path(os.environ["SUBTALK_CONFIG"]) if os.environ.get("SUBTALK_CONFIG") else None)):
        if p and Path(p).exists():
            data = _deep_merge(data, yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {})
    cfg = Config.model_validate(data)
    cfg.root = root
    return cfg


def anthropic_api_key() -> str | None:
    """ANTHROPIC_API_KEY, or CLAUDE_KEY (Windows env names are case-insensitive,
    so a variable saved as ``Claude_key`` is found too)."""
    return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_KEY") or None


def relay_token() -> str | None:
    return os.environ.get("SUBTALK_RELAY_TOKEN") or None


def audio_token() -> str | None:
    return os.environ.get("SUBTALK_AUDIO_TOKEN") or None
