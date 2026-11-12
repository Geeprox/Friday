"""合成账本覆盖并发、重启、跨日、未知消耗及配额收紧。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from homeplane.intelligence.budget import EnhancementUnavailable, UsageLedger
from homeplane.intelligence.settings import IntelligenceConfig, IntelligenceSettings


def test_budget_is_shared_atomic_and_survives_restart(tmp_path):
    config = IntelligenceConfig(daily_requests=3, daily_text_tokens=90)
    path = tmp_path / "usage.sqlite"
    ledger = UsageLedger(path)

    def reserve(i):
        try:
            UsageLedger(path).reserve(str(i), "responses", config, text_tokens=30)
            return True
        except EnhancementUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    assert ledger.status(config)["used"] == dict(requests=3, text_tokens=90, images=0, voice_seconds=0)
    assert UsageLedger(path).status(config)["uncertain_requests"] == 3


def test_settlement_is_once_and_unknown_keeps_reservation(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    config = IntelligenceConfig(daily_text_tokens=100)
    ledger.reserve("a", "responses", config, text_tokens=60)
    ledger.settle("a", text_tokens=20)
    ledger.settle("a", text_tokens=0)
    ledger.reserve("b", "responses", config, text_tokens=70)
    ledger.settle("b")
    assert ledger.status(config)["used"]["text_tokens"] == 90
    assert ledger.status(config)["uncertain_requests"] == 1
    with pytest.raises(EnhancementUnavailable, match="daily_text_tokens_reached"):
        ledger.reserve("c", "responses", config, text_tokens=11)
    with pytest.raises(EnhancementUnavailable, match="request_already_attempted"):
        ledger.reserve("a", "responses", config)


def test_day_rollover_and_timezone_change_do_not_free_existing_usage(tmp_path):
    now = [datetime(2026, 11, 10, 15, 55, tzinfo=UTC)]
    config = IntelligenceConfig(daily_requests=1)
    ledger = UsageLedger(tmp_path / "usage.sqlite", now=lambda: now[0])
    ledger.reserve("a", "images", config, images=1)
    reset = ledger.status(config)["resets_at"]
    changed = config.model_copy(update={"timezone": "America/Los_Angeles"})
    assert ledger.status(changed)["resets_at"] == reset
    with pytest.raises(EnhancementUnavailable, match="daily_requests_reached"):
        ledger.reserve("b", "images", changed)
    now[0] += timedelta(minutes=10)
    assert ledger.status(config)["used"]["requests"] == 0
    ledger.reserve("b", "images", config)
    now[0] -= timedelta(days=1)
    with pytest.raises(EnhancementUnavailable, match="usage_clock_regressed"):
        ledger.reserve("c", "images", config)


def test_zero_limit_and_lowered_limit_block_without_reset(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    config = IntelligenceConfig(daily_images=0)
    with pytest.raises(EnhancementUnavailable, match="daily_images_reached"):
        ledger.reserve("a", "images", config, images=1)
    ledger.reserve("b", "responses", config)
    config.daily_requests = 0
    with pytest.raises(EnhancementUnavailable, match="daily_requests_reached"):
        ledger.reserve("c", "responses", config)
    assert ledger.status(config)["used"]["requests"] == 1


def test_lowering_image_limit_preserves_independent_text_and_voice_budget(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    config = IntelligenceConfig(daily_images=1)
    ledger.reserve("picture", "images", config, images=1)
    config.daily_images = 0
    ledger.reserve("text", "responses", config, text_tokens=100)
    ledger.reserve("voice", "realtime", config, voice_seconds=25)
    with pytest.raises(EnhancementUnavailable, match="daily_images_reached"):
        ledger.reserve("picture-two", "images", config, images=1)
    assert ledger.status(config)["used"] == dict(requests=3, text_tokens=100, images=1, voice_seconds=25)


def test_private_settings_do_not_echo_credentials_or_accept_symlink(tmp_path):
    settings = IntelligenceSettings(tmp_path)
    settings.save_key("SIMULATED-private-key")
    settings.update({"provider": "openai", "cloud_text": True})
    assert settings.key() == "SIMULATED-private-key"
    assert "SIMULATED" not in settings.path.read_text()
    assert settings.credential.stat().st_mode & 0o777 == 0o600
    settings.remove_key()
    assert not settings.credential.exists()
    settings.path.unlink()
    settings.path.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(ValueError, match="canonical"):
        settings.read()
