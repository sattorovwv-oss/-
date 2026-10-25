import os

from dotenv import dotenv_values
import pytest

from scripts.configure_google import configure

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
