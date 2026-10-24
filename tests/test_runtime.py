import asyncio
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from main import instance_lock
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
