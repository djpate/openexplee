from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    database_path: str = "data/openexplee.db"

    # LLM Settings (Defaults to Thor GPU Ollama + fallback to Claude CLI)
    default_llm_provider: str = Field(
        default="thor_ollama",
        description="thor_ollama | claude_cli | gemini | openai | anthropic | groq | ollama",
    )
    default_llm_model: str = "gemini-2.5-flash"
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "low"  # low | medium | high | xhigh | max
    groq_api_key: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"
    thor_ollama_url: str = "http://100.114.48.60:11434"
    thor_ollama_model: str = "llama3.2:3b"
    thor_cdp_url: str = "http://100.114.48.60:9222"

    # Booking link (e.g. your Cal.com page) used in outreach CTAs. Blank = no link.
    booking_url: str = ""

    # Sender identity
    sender_name: str = ""  # sign-off name on AI-written emails
    forward_to_email: str = ""  # your real inbox: Cloudflare catch-all forwarding + DMARC reports
    reply_cc_email: str = ""  # CC'd on replies sent from the Mailbox. Blank = no extra CC.

    # Compliance footer added under every cold email and follow-up
    opt_out_line: str = 'Not the right fit? Just reply "unsubscribe" and I won\'t email you again.'
    postal_address: str = ""

    # Search & Enrichment
    serper_api_key: Optional[str] = None
    serper_max_queries: int = 4

    # Overture Maps company discovery
    overture_release: str = ""  # blank = latest release
    overture_min_confidence: float = 0.5
    overture_max_places: int = 25000  # max places one search fetches (best-ranked kept)
    discovery_batch_size: int = 50
    discovery_concurrency: int = 8
    default_search_area: str = "United States"
    # Local parquet globs (tests/offline use). Blank = read from the Overture S3 bucket.
    overture_places_source: str = ""
    overture_divisions_source: str = ""

    # Sending & Deliverability Safeguards
    protected_primary_domains: str = ""  # your main domain(s), comma-separated; never send from these
    max_daily_sends_per_inbox: int = 35
    min_delay_seconds: int = 120
    max_delay_seconds: int = 300
    warmup_enabled: bool = True
    warmup_daily_target: int = 15


settings = Settings()

