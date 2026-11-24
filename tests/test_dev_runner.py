import importlib.util
import os
from pathlib import Path

DEV = Path(__file__).resolve().parents[1] / "scripts/dev.py"
spec = importlib.util.spec_from_file_location("dev_runner", DEV)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


def test_env_file_parsing_ignores_comments_and_strips_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "﻿# comment\n\nPOSTGRES_DB=finguard\nexport APP_ACCESS_PIN='1234'\n"
        'DASHSCOPE_BASE_URL="https://example.test/v1?a=b"\nEMPTY=\nnot a line\n1BAD=x\n',
        encoding="utf-8",
    )
    assert dev.parse_env_file(env_file) == {
        "POSTGRES_DB": "finguard",
        "APP_ACCESS_PIN": "1234",
        "DASHSCOPE_BASE_URL": "https://example.test/v1?a=b",
        "EMPTY": "",
    }


def test_shell_variables_override_env_file_and_postgres_maps_to_libpq():
    env = dev.build_env(
        {"POSTGRES_DB": "finguard", "POSTGRES_USER": "fg", "POSTGRES_PASSWORD": "pw", "PGPORT": "55432"},
        {"PGPORT": "6543"},
    )
    assert (env["PGDATABASE"], env["PGUSER"], env["PGPASSWORD"]) == ("finguard", "fg", "pw")
    assert env["PGPORT"] == "6543"
    assert env["PGHOST"] == "127.0.0.1"


def test_source_snapshot_sees_edits_additions_and_deletions_but_not_bytecode(tmp_path):
    package = tmp_path / "backend"
    (package / "__pycache__").mkdir(parents=True)
    module = package / "main.py"
    module.write_text("A = 1\n", encoding="utf-8")
    (package / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"x")
    (package / "notes.txt").write_text("ignored", encoding="utf-8")
    before = dev.source_snapshot(package)
    assert list(before) == [module]

    (package / "__pycache__" / "other.py").write_text("", encoding="utf-8")
    assert dev.source_snapshot(package) == before
    stat = module.stat()
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert dev.source_snapshot(package) != before
    added = package / "sub" / "new.py"
    added.parent.mkdir()
    added.write_text("", encoding="utf-8")
    assert added in dev.source_snapshot(package)
    added.unlink()
    assert added not in dev.source_snapshot(package)
