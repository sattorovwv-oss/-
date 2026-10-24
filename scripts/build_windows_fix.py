"""Build one self-contained updater for an already installed Windows checkout.

The generated file contains only the explicitly listed application files. No
download, .env access, credential handling, database update or pip invocation.
"""
import argparse
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PATCH_FILES = (
    "main.py", "tuman/bot.py", "tuman/config.py", "tuman/db.py",
    "tuman/network.py", "tuman/texts.py", "tuman/web.py",
)

UPDATER = '''# -*- coding: utf-8 -*-
"""Tuman Kargo: apply the bundled Windows fixes, preserving local data.

Stop the bot with Ctrl+C first. Put this file beside main.py and run it using
the same virtual environment as the application. The payload is bundled; no
external downloads or package installation are performed.
"""
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

MANIFEST = json.loads(__MANIFEST__)
PAYLOAD = __PAYLOAD__


def project_directory():
    roots = [Path(__file__).resolve().parent, Path.cwd().resolve()]
    roots += [path / "--main" for path in list(roots)]
    for root in roots:
        if (root / "main.py").is_file() and (root / "tuman").is_dir() and (root / "requirements.lock").is_file():
            return root.resolve()
    raise RuntimeError("Положите update_tuman.py рядом с main.py в папке проекта.")


def write_atomic(target, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".tuman-update-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    root = project_directory()
    contents = {}
    with zipfile.ZipFile(BytesIO(base64.b64decode(PAYLOAD))) as archive:
        if set(archive.namelist()) != set(MANIFEST):
            raise RuntimeError("Пакет обновления повреждён. Скачайте его заново.")
        for name, expected in MANIFEST.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or not name.endswith(".py"):
                raise RuntimeError("Недопустимый путь в пакете обновления.")
            target = root.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(root) or not target.is_file() or target.is_symlink():
                raise RuntimeError("Ожидаемый файл проекта не найден: " + name)
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != expected:
                raise RuntimeError("Не совпала контрольная сумма: " + name)
            compile(content, name, "exec")
            contents[name] = content

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    backup = root / "var" / "code-backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for name in contents:
        saved = backup.joinpath(*PurePosixPath(name).parts)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root.joinpath(*PurePosixPath(name).parts), saved)

    replaced = []
    try:
        for name, content in contents.items():
            write_atomic(root.joinpath(*PurePosixPath(name).parts), content)
            replaced.append(name)
        check = subprocess.run([
            sys.executable, "-c",
            "import main; from tuman.config import Settings; from tuman.web import DUSHANBE_TIMEZONE; "
            "from datetime import timedelta; assert not Settings().has_public_web_url(); "
            "assert DUSHANBE_TIMEZONE.utcoffset(None) == timedelta(hours=5); "
            "assert hasattr(main, 'asyncio')"
        ], cwd=root, capture_output=True, text=True)
        if check.returncode:
            raise RuntimeError("Проверка импорта не прошла. Используйте то же окружение Python, в котором установлены зависимости.")
    except BaseException:
        for name in reversed(replaced):
            target = root.joinpath(*PurePosixPath(name).parts)
            saved = backup.joinpath(*PurePosixPath(name).parts)
            write_atomic(target, saved.read_bytes())
        raise

    print("Обновление установлено. Проверено файлов:", len(contents))
    print("Старый код сохранён:", backup)
    print("Файл .env и база данных не изменялись.")
    print("Теперь запустите main.py тем же Python и проверьте /start.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Обновление не установлено:", str(error), file=sys.stderr)
        raise SystemExit(1)
'''


def build_updater(destination):
    manifest = {}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in PATCH_FILES:
            content = (ROOT / name).read_bytes()
            manifest[name] = hashlib.sha256(content).hexdigest()
            archive.writestr(name, content)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    source = UPDATER.replace("__MANIFEST__", repr(json.dumps(manifest))).replace("__PAYLOAD__", repr(payload))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source, encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build_updater(args.output))
