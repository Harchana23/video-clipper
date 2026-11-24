import pytest

from clipper.config import load_settings


def test_load_settings_applies_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'data_dir = "{(tmp_path / "clips").as_posix()}"\npassword_hash = "x"\nsession_secret = "s"\n')
    s = load_settings(p)
    assert s.ollama_model == "qwen3:4b" and s.caption_delay == 0.1
    assert s.source_retention_days == 14 and s.default_captions_on is False
    assert s.data_dir.is_dir()


def test_load_settings_rejects_missing_password_hash(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('data_dir = "D:/clips"\nsession_secret = "s"\n')
    with pytest.raises(ValueError, match="password_hash"):
        load_settings(p)


def test_load_settings_rejects_unknown_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'data_dir = "{tmp_path.as_posix()}"\npassword_hash = "x"\nsession_secret = "s"\nbogus = 1\n')
    with pytest.raises(ValueError, match="bogus"):
        load_settings(p)


@pytest.mark.parametrize("key", ["password_hash", "session_secret"])
@pytest.mark.parametrize("val", ["", "   "])
def test_load_settings_rejects_empty_secrets(tmp_path, key, val):
    vals = {"password_hash": "x", "session_secret": "s", key: val}
    p = tmp_path / "config.toml"
    p.write_text(f'data_dir = "{tmp_path.as_posix()}"\npassword_hash = "{vals["password_hash"]}"\nsession_secret = "{vals["session_secret"]}"\n')
    with pytest.raises(ValueError, match=key):
        load_settings(p)
