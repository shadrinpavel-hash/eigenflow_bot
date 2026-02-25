import sys
import types

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


class _FakeImapConn:
    def __init__(self):
        self.login_args = None

    def login(self, email_addr, password):
        self.login_args = (email_addr, password)


def test_get_credentials_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_EMAIL", "user@yandex.ru")
    monkeypatch.setenv("YANDEX_APP_PASSWORD", "secret")

    email_addr, password = yandex_mail._get_credentials()

    assert email_addr == "user@yandex.ru"
    assert password == "secret"


def test_get_credentials_from_env_file(monkeypatch):
    monkeypatch.delenv("YANDEX_EMAIL", raising=False)
    monkeypatch.delenv("YANDEX_APP_PASSWORD", raising=False)
    monkeypatch.setattr(
        yandex_mail,
        "_load_env_file",
        lambda _path: {
            "YANDEX_EMAIL": "file@yandex.ru",
            "YANDEX_APP_PASSWORD": "file_secret",
        },
    )

    email_addr, password = yandex_mail._get_credentials()

    assert email_addr == "file@yandex.ru"
    assert password == "file_secret"


def test_get_credentials_from_colab_userdata(monkeypatch):
    monkeypatch.delenv("YANDEX_EMAIL", raising=False)
    monkeypatch.delenv("YANDEX_APP_PASSWORD", raising=False)
    monkeypatch.setattr(yandex_mail, "_load_env_file", lambda _path: {})

    fake_userdata = types.SimpleNamespace(
        get=lambda key: {
            "YANDEX_EMAIL": "colab@yandex.ru",
            "YANDEX_APP_PASSWORD": "colab_secret",
        }.get(key, "")
    )
    fake_colab = types.SimpleNamespace(userdata=fake_userdata)
    fake_google = types.SimpleNamespace(colab=fake_colab)

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.colab", fake_colab)

    email_addr, password, sources = yandex_mail._resolve_credentials()

    assert email_addr == "colab@yandex.ru"
    assert password == "colab_secret"
    assert "google.colab.userdata" in sources


def test_connect_passes_credentials_to_yandex_login(monkeypatch):
    fake_conn = _FakeImapConn()

    monkeypatch.setattr(
        yandex_mail,
        "_get_credentials",
        lambda: ("agent@yandex.ru", "app-password"),
    )
    monkeypatch.setattr(yandex_mail.imaplib, "IMAP4_SSL", lambda host, port: fake_conn)

    conn = yandex_mail._connect()

    assert conn is fake_conn
    assert fake_conn.login_args == ("agent@yandex.ru", "app-password")


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
