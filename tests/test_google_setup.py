import os
import shlex
import sys

from dotenv import dotenv_values
import pytest

from scripts.configure_google import configure
from scripts import configure_google

CLIENT_ID = "123456-test-client.apps.googleusercontent.com"
SECRET = "GOCSPX-only-for-tests-123456789"


def project(tmp_path):
    (tmp_path / "compose.yaml").touch()
    (tmp_path / "main.py").touch()
    return tmp_path


def test_google_setup_preserves_credentials_and_private_backup(tmp_path):
    root = project(tmp_path)
    previous = b"BOT_TOKEN='original-test-token'\nOWNER_PASSWORD='original-test-password'\nPUBLIC_URL='https://cargo.test'\nOWNER_TELEGRAM_IDS='202'\n"
    (root / ".env").write_bytes(previous)
    backup = configure(root, CLIENT_ID, SECRET)
    assert backup.read_bytes() == previous
    values = dotenv_values(root / ".env")
    assert values["BOT_TOKEN"] == "original-test-token"
    assert values["OWNER_PASSWORD"] == "original-test-password"
    assert values["OWNER_TELEGRAM_IDS"] == "202"
    assert values["GOOGLE_CLIENT_ID"] == CLIENT_ID
    assert values["GOOGLE_CLIENT_SECRET"] == SECRET
    if os.name != "nt":
        assert (root / ".env").stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o600
    assert not list(root.glob(".env.google-*"))


def test_google_setup_updates_duplicate_keys_and_keeps_other_lines(tmp_path):
    root = project(tmp_path)
    (root / ".env").write_text("# private settings\nGOOGLE_CLIENT_ID=old\n export GOOGLE_CLIENT_ID=duplicate\nGOOGLE_CLIENT_SECRET=old\nOTHER=kept")
    configure(root, CLIENT_ID, SECRET)
    content = (root / ".env").read_text()
    assert content.count("GOOGLE_CLIENT_ID=") == 1
    assert content.count("GOOGLE_CLIENT_SECRET=") == 1
    assert "OTHER=kept\n" in content
    assert content.startswith("# private settings\n")


@pytest.mark.parametrize("client_id,secret", [("wrong", SECRET), (CLIENT_ID, "short"), (CLIENT_ID, "secret\nINJECT=value"), (CLIENT_ID, "secret with spaces")])
def test_google_setup_rejects_invalid_input_without_replacing_environment(tmp_path, client_id, secret):
    root = project(tmp_path)
    (root / ".env").write_text("UNCHANGED=1\n")
    with pytest.raises(ValueError):
        configure(root, client_id, secret)
    assert (root / ".env").read_text() == "UNCHANGED=1\n"
    assert not list(root.glob(".env.before-google-*"))


def test_google_setup_requires_existing_environment(tmp_path):
    root = project(tmp_path)
    with pytest.raises(ValueError):
        configure(root, CLIENT_ID, SECRET)
    assert not (root / ".env").exists()


@pytest.mark.parametrize("wrapped", ["'{value}'", '"{value}"', "\x1b[200~{value}\x1b[201~", '\x1b[200~"{value}"\x1b[201~'])
def test_google_setup_accepts_copy_wrappers_without_storing_them(tmp_path, wrapped):
    root = project(tmp_path)
    (root / ".env").write_text("UNCHANGED=1\n")
    configure(root, wrapped.format(value=CLIENT_ID), wrapped.format(value=SECRET))
    values = dotenv_values(root / ".env")
    assert values["GOOGLE_CLIENT_ID"] == CLIENT_ID
    assert values["GOOGLE_CLIENT_SECRET"] == SECRET


@pytest.mark.parametrize("secret,message", [("", "не введён"), (CLIENT_ID, "вставлен Client ID"), ("short", "Формат Client Secret")])
def test_google_setup_reports_specific_secret_error_without_disclosing_input(tmp_path, secret, message):
    root = project(tmp_path)
    (root / ".env").write_text("UNCHANGED=1\n")
    with pytest.raises(configure_google.ConfigurationError, match=message) as error:
        configure(root, CLIENT_ID, secret)
    if secret:
        assert secret not in str(error.value)
    assert (root / ".env").read_text() == "UNCHANGED=1\n"


@pytest.mark.parametrize("error,expected", [
    (configure_google.ConfigurationError("Client Secret не введён."), "не введён"),
    (PermissionError(13, "Permission denied", SECRET), "нет доступа"),
    (FileNotFoundError(2, "No such file or directory", SECRET), "Файловая ошибка"),
    (UnicodeDecodeError("utf-8", SECRET.encode(), 0, 1, "invalid encoding"), "UTF-8"),
])
def test_google_setup_cli_errors_are_actionable_without_exposing_secret(monkeypatch, capsys, error, expected):
    monkeypatch.setattr(sys, "argv", ["configure_google.py", "--client-id", CLIENT_ID])
    monkeypatch.setattr(configure_google.getpass, "getpass", lambda prompt: SECRET)

    def fail(*args):
        raise error

    monkeypatch.setattr(configure_google, "configure", fail)
    with pytest.raises(SystemExit) as result:
        configure_google.main()
    assert result.value.code == 1
    output = capsys.readouterr().out
    assert expected in output
    assert SECRET not in output


def test_google_setup_cli_prints_absolute_compose_command_from_any_directory(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project with spaces"
    root.mkdir()
    project(root)
    (root / ".env").write_text("UNCHANGED=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_google, "__file__", str(root / "scripts" / "configure_google.py"))
    monkeypatch.setattr(sys, "argv", ["configure_google.py", "--client-id", CLIENT_ID])
    monkeypatch.setattr(configure_google.getpass, "getpass", lambda prompt: SECRET)
    configure_google.main()
    output = capsys.readouterr().out
    command = shlex.split(output.splitlines()[-1])
    assert command == ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "compose.yaml"), "up", "-d", "--build", "app"]
    assert SECRET not in output
