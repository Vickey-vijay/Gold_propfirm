"""Crypto round-trip, MT5 connection storage, and the schema migration that
protects an already-deployed database from a later schema change."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from propdesk.storage import AccountStore
from propdesk.sync.crypto import SecretKeyMissing, decrypt, encrypt


@pytest.fixture
def secret_key(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PROPDESK_SECRET_KEY", key)
    return key


class TestCrypto:
    def test_round_trips(self, secret_key):
        assert decrypt(encrypt("my-investor-password")) == "my-investor-password"

    def test_different_plaintexts_produce_different_ciphertext(self, secret_key):
        assert encrypt("password-a") != encrypt("password-b")

    def test_missing_key_refuses_to_encrypt(self, monkeypatch):
        monkeypatch.delenv("PROPDESK_SECRET_KEY", raising=False)
        with pytest.raises(SecretKeyMissing):
            encrypt("anything")

    def test_wrong_key_cannot_decrypt(self, secret_key, monkeypatch):
        ciphertext = encrypt("secret")
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PROPDESK_SECRET_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError):
            decrypt(ciphertext)


class TestMt5ConnectionStorage:
    def test_no_connection_initially(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        assert store.get_mt5_connection() is None

    def test_save_and_retrieve(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        store.save_mt5_connection(
            login="12345", server="FTMO-Demo",
            encrypted_investor_password="enc-pw", encrypted_metaapi_token="enc-token",
        )
        conn = store.get_mt5_connection()
        assert conn["login"] == "12345"
        assert conn["server"] == "FTMO-Demo"
        assert conn["enabled"] == 1

    def test_saving_again_replaces_not_appends(self, tmp_path):
        """Only one MT5 connection can exist at a time — a reconnect replaces it."""
        store = AccountStore(tmp_path / "t.db")
        store.save_mt5_connection(login="a", server="s1", encrypted_investor_password="x", encrypted_metaapi_token="y")
        store.save_mt5_connection(login="b", server="s2", encrypted_investor_password="x", encrypted_metaapi_token="y")
        assert store.get_mt5_connection()["login"] == "b"

    def test_disconnect_removes_it(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        store.save_mt5_connection(login="a", server="s", encrypted_investor_password="x", encrypted_metaapi_token="y")
        store.disconnect_mt5()
        assert store.get_mt5_connection() is None

    def test_record_sync_result_updates_timestamp_and_error(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        cid = store.save_mt5_connection(login="a", server="s", encrypted_investor_password="x", encrypted_metaapi_token="y")
        store.record_sync_result(cid, error="connection refused")
        conn = store.get_mt5_connection()
        assert conn["last_sync_error"] == "connection refused"
        assert conn["last_synced_at"] is not None


class TestDailyRollover:
    def test_no_rollover_initially(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        assert store.last_rollover_date() is None

    def test_record_and_read_back(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        store.record_rollover("2026-09-23", Decimal("101250.50"))
        assert store.last_rollover_date() == "2026-09-23"

    def test_idempotent_for_same_date(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        store.record_rollover("2026-09-23", Decimal("100000"))
        store.record_rollover("2026-09-23", Decimal("100500"))  # same day, updated balance
        assert store.last_rollover_date() == "2026-09-23"

    def test_later_date_becomes_latest(self, tmp_path):
        store = AccountStore(tmp_path / "t.db")
        store.record_rollover("2026-09-22", Decimal("100000"))
        store.record_rollover("2026-09-23", Decimal("100500"))
        assert store.last_rollover_date() == "2026-09-23"


class TestSchemaMigration:
    def test_predates_source_column_does_not_crash(self, tmp_path, account):
        """Simulates the database this session already deployed to the server
        BEFORE the `source` column existed. Opening it with the new AccountStore
        must migrate it in place, not crash on the next save()."""
        db_path = tmp_path / "old-shape.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, phase TEXT NOT NULL, initial_balance TEXT NOT NULL,
                balance TEXT NOT NULL, equity TEXT NOT NULL,
                prev_day_closing_balance TEXT NOT NULL, trading_days_used INTEGER NOT NULL,
                trades_today INTEGER NOT NULL, consecutive_losses INTEGER NOT NULL,
                week_pnl TEXT NOT NULL, last_loss_at TEXT, open_positions_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO account_snapshots (ts, phase, initial_balance, balance, equity, "
            "prev_day_closing_balance, trading_days_used, trades_today, consecutive_losses, "
            "week_pnl, last_loss_at, open_positions_json) VALUES "
            "('2026-09-20T00:00:00+00:00','challenge','100000','100000','100000','100000',0,0,0,'0',NULL,'[]')"
        )
        conn.commit()
        conn.close()

        store = AccountStore(db_path)  # must not raise
        old_row = store.latest()
        assert old_row.equity == Decimal("100000")

        store.save(account)  # must not raise 'no such column: source'
        history = store.history(limit=1)
        assert history[0]["source"] == "manual"
