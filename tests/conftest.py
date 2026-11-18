"""Every test runs away from the real data: its own database (and so its own log folder, which sits next to the
database), and no real API keys, so nothing a test does can write to data/, spend Apify or Claude credit, or post
to Discord. Settings.from_env() reads .env but never overrides a variable that's already set, so these win."""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "env" / "concerts.db"))
    for key in ("APIFY_API_TOKEN", "ANTHROPIC_API_KEY", "DISCORD_WEBHOOK_URL"):
        monkeypatch.setenv(key, "")
    yield
