import json

from app.utils import strategy_runtime_logs as runtime_logs


def test_notify_bark_for_strategy_error_uses_user_bark_webhook(monkeypatch):
    runtime_logs._BARK_ERROR_DEDUP.clear()

    class FakeCursor:
        def execute(self, sql, params):
            self.row = {
                "strategy_name": "S6",
                "symbol": "DOGE/USDT",
                "notification_settings": json.dumps(
                    {"webhook_url": "https://api.day.app/test-key"}
                ),
            }

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class FakeDb:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    sent = {}

    class FakeNotifier:
        def _notify_webhook(self, *, url, payload, token_override=None, signing_secret_override=None):
            sent["url"] = url
            sent["payload"] = payload
            return True, ""

    monkeypatch.setattr(runtime_logs, "get_db_connection", lambda: FakeDb())
    monkeypatch.setattr("app.services.signal_notifier.SignalNotifier", FakeNotifier)

    runtime_logs._notify_bark_for_strategy_error(6, "Loop error: boom")

    assert sent["url"] == "https://api.day.app/test-key"
    assert sent["payload"]["event"] == "qd.strategy_error"
    assert sent["payload"]["strategy"]["id"] == 6
    assert "Loop error: boom" in sent["payload"]["message"]


def test_notify_bark_for_strategy_error_dedups_identical_message(monkeypatch):
    runtime_logs._BARK_ERROR_DEDUP.clear()

    class FakeCursor:
        def execute(self, sql, params):
            self.row = {
                "strategy_name": "S6",
                "symbol": "DOGE/USDT",
                "notification_settings": json.dumps(
                    {"webhook_url": "https://api.day.app/test-key"}
                ),
            }

        def fetchone(self):
            return self.row

        def close(self):
            pass

    class FakeDb:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = {"count": 0}

    class FakeNotifier:
        def _notify_webhook(self, *, url, payload, token_override=None, signing_secret_override=None):
            calls["count"] += 1
            return True, ""

    monkeypatch.setattr(runtime_logs, "get_db_connection", lambda: FakeDb())
    monkeypatch.setattr("app.services.signal_notifier.SignalNotifier", FakeNotifier)

    runtime_logs._notify_bark_for_strategy_error(6, "same error")
    runtime_logs._notify_bark_for_strategy_error(6, "same error")

    assert calls["count"] == 1
