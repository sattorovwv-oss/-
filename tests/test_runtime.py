import asyncio
import os
from pathlib import Path
import socket
import shutil
import ssl
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from main import instance_lock, prepare_environment_file
from tuman.network import EnvironmentSession
from .conftest import TEST_PASSWORD


def test_network_adapter_keeps_tls_verification_and_respects_proxy():
    async def run():
        adapter=EnvironmentSession()
        assert adapter._connector_init["ssl"].verify_mode==ssl.CERT_REQUIRED
        assert adapter._connector_init["ssl"].check_hostname
        session=await adapter.create_session()
        assert session.trust_env is True
        await adapter.close()
    asyncio.run(run())


def test_instance_lock_rejects_second_process(tmp_path):
    path=tmp_path/"instance.lock"
    with instance_lock(path):
        with pytest.raises(RuntimeError):
            with instance_lock(path): pass


def test_real_main_starts_and_serves_database_health(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
    env={**os.environ,"DB_PATH":str(tmp_path/"runtime.db"),"WEB_ONLY":"1","APP_ENV":"development","BOT_TOKEN":"","OWNER_PASSWORD":TEST_PASSWORD,"CHINA_PASSWORD":"","TAJIK_PASSWORD":"","OWNER_TELEGRAM_IDS":"","CHINA_TELEGRAM_IDS":"","TAJIK_TELEGRAM_IDS":"","HOST":"127.0.0.1","PORT":str(port),"PUBLIC_URL":f"http://127.0.0.1:{port}"}
    root=Path(__file__).resolve().parent.parent
    process=subprocess.Popen([sys.executable,"main.py"],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    try:
        ready=False
        for _ in range(100):
            if process.poll() is not None: break
            try:
                with urlopen(f"http://127.0.0.1:{port}/health",timeout=1) as response:
                    ready=response.status==200
                if ready: break
            except OSError: time.sleep(.1)
        assert ready
        assert list((tmp_path/"backups").glob("*.db"))
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()


def test_environment_recovery_preserves_existing_project_file(tmp_path):
    project = tmp_path / "--main" / "--main"
    project.mkdir(parents=True)
    source = project.parent / ".env"
    target = project / ".env"
    source.write_text("WEB_ONLY=1\n")
    target.write_text("WEB_ONLY=0\n")
    assert prepare_environment_file(project) == target
    assert target.read_text() == "WEB_ONLY=0\n"
    assert source.read_text() == "WEB_ONLY=1\n"


def test_environment_recovery_does_not_read_unrelated_parent(tmp_path):
    project = tmp_path / "different-project"
    project.mkdir()
    (tmp_path / ".env").write_text("WEB_ONLY=1\n")
    assert prepare_environment_file(project) is None
    assert not (project / ".env").exists()


@pytest.mark.parametrize("location", ["project", "parent"])
def test_environment_directory_is_reported_without_removing_it(tmp_path, location):
    project = tmp_path / "--main" / "--main"
    project.mkdir(parents=True)
    folder = (project if location == "project" else project.parent) / ".env"
    folder.mkdir()
    (folder / "keep.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="текстовым файлом"):
        prepare_environment_file(project)
    assert (folder / "keep.txt").read_text() == "keep"


def test_nested_archive_main_recovers_env_and_serves_health(tmp_path):
    root = Path(__file__).resolve().parent.parent
    project = tmp_path / "--main" / "--main"
    project.mkdir(parents=True)
    shutil.copy2(root / "main.py", project / "main.py")
    for name in ("tuman", "templates", "static"):
        shutil.copytree(root / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    values = {
        "APP_ENV": "development", "WEB_ONLY": "1", "BOT_TOKEN": "",
        "OWNER_PASSWORD": TEST_PASSWORD, "CHINA_PASSWORD": "", "TAJIK_PASSWORD": "",
        "OWNER_TELEGRAM_IDS": "", "CHINA_TELEGRAM_IDS": "", "TAJIK_TELEGRAM_IDS": "",
        "DB_PATH": "var/tuman.db", "HOST": "127.0.0.1", "PORT": str(port),
        "PUBLIC_URL": f"http://127.0.0.1:{port}", "FORWARDED_ALLOW_IPS": "127.0.0.1",
    }
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    original = project.parent / ".env"
    original.write_text(content)
    env = {key: value for key, value in os.environ.items() if key not in values}
    process = subprocess.Popen([sys.executable, "main.py"], cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ready = False
        for _ in range(100):
            if process.poll() is not None:
                break
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except OSError:
                time.sleep(.1)
        assert ready, "Nested-archive startup did not become ready"
        assert (project / ".env").read_bytes() == original.read_bytes()
        assert original.read_text() == content
        if os.name != "nt":
            assert (project / ".env").stat().st_mode & 0o777 == 0o600
        assert (project / "var" / "tuman.db").is_file()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
