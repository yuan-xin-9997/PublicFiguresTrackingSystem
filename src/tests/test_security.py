from pathlib import Path

import pytest

from app.backend.security import hash_password, parse_password_file, verify_password


def test_password_hash_round_trip():
    encoded = hash_password("秘密-123")
    assert encoded.startswith("scrypt$")
    assert verify_password("秘密-123", encoded)
    assert not verify_password("wrong", encoded)


def test_password_file_validation(tmp_path: Path):
    path = tmp_path / "password.txt"
    path.write_text("# comment\nadmin:pw:admin\nreader:pw:user\n", encoding="utf-8")
    assert parse_password_file(path) == [("admin", "pw", "admin"), ("reader", "pw", "user")]
    path.write_text("broken-line", encoding="utf-8")
    with pytest.raises(ValueError, match="格式无效"):
        parse_password_file(path)

