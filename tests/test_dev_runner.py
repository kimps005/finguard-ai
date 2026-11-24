import importlib.util
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
