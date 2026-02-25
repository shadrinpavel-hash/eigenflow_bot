from ouroboros.tools import yandex_mail


class _DummyConn:
    def __init__(self, search_payload: bytes):
        self.search_payload = search_payload
        self.logged_out = False

    def select(self, _mailbox):
        return "OK", [b""]

    def uid(self, command, *_args):
        if command.lower() == "search":
            return "OK", [self.search_payload]
        raise AssertionError("unexpected uid command")

    def logout(self):
        self.logged_out = True


def test_get_credentials_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "user@yandex.ru")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")

    email_addr, password = yandex_mail._get_credentials()

    assert email_addr == "user@yandex.ru"
    assert password == "secret"


def test_get_credentials_from_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("YANDEX_EMAIL", raising=False)
    monkeypatch.delenv("YANDEX_APP_PASSWORD", raising=False)

    env_file = tmp_path / "ouroboros.env"
    env_file.write_text("YANDEX_EMAIL=file@yandex.ru\nYANDEX_APP_PASSWORD=file_secret\n")

    monkeypatch.setattr(yandex_mail, "_load_env_file", lambda _path: {"YANDEX_EMAIL": "file@yandex.ru", "YANDEX_APP_PASSWORD": "file_secret"})

    email_addr, password = yandex_mail._get_credentials()

    assert email_addr == "file@yandex.ru"
    assert password == "file_secret"


def test_monitor_inbox_formats_important(monkeypatch):
    conn = _DummyConn(search_payload=b"1 2")
    monkeypatch.setattr(yandex_mail, "_connect", lambda: conn)
    monkeypatch.setattr(
        yandex_mail,
        "_fetch_messages",
        lambda _conn, _uids: [
            {
                "from": "boss@example.com",
                "subject": "URGENT: payment deadline",
                "date": "2026-01-01 12:00",
                "preview": "Please pay invoice ASAP",
            }
        ],
    )

    result = yandex_mail._yandex_monitor_inbox(ctx=None, limit=5, unseen_only=True)

    assert "🔴 ВАЖНО" in result
    assert "Итого важных писем: 1 из 1" in result
    assert conn.logged_out
