import os
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.build_windows_fix import PATCH_FILES, ROOT, build_updater


def project_fixture(tmp_path):
    project = tmp_path / "installed-project"
    project.mkdir()
    for folder in ("tuman", "templates", "static"):
        shutil.copytree(ROOT / folder, project / folder, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.py", "requirements.lock"):
        shutil.copy2(ROOT / name, project / name)
    return project


def test_updater_restores_complete_code_without_changing_environment_or_database(tmp_path):
    project = project_fixture(tmp_path)
    (project / "main.py").write_text("# An incomplete local copy\n")
    config = project / ".env"
    config.write_bytes(b"# private test configuration must stay untouched\n")
    (project / "var").mkdir()
    database = project / "var" / "tuman.db"
    database.write_bytes(b"database placeholder: updater must never open it")
    updater = build_updater(project / "update_tuman.py")
    result = subprocess.run([sys.executable, str(updater)], cwd=project, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == b"# private test configuration must stay untouched\n"
    assert database.read_bytes() == b"database placeholder: updater must never open it"
    for name in PATCH_FILES:
        assert (project / name).read_bytes() == (ROOT / name).read_bytes()
    backups = list((project / "var" / "code-backups").glob("*/main.py"))
    assert len(backups) == 1
    assert backups[0].read_text() == "# An incomplete local copy\n"


def test_updater_rolls_back_changed_files_when_import_check_fails(tmp_path):
    project = project_fixture(tmp_path)
    # The current code should be preserved if local dependencies are incomplete.
    (project / "tuman" / "workers.py").unlink()
    originals = {name: (project / name).read_bytes() for name in PATCH_FILES}
    (project / "main.py").write_text("# local original\n")
    originals["main.py"] = (project / "main.py").read_bytes()
    updater = build_updater(project / "update_tuman.py")
    result = subprocess.run([sys.executable, str(updater)], cwd=project, capture_output=True, text=True)
    assert result.returncode == 1
    for name, content in originals.items():
        assert (project / name).read_bytes() == content
