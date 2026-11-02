"""komodo_api.load_env: credentials come from komodo.env when there is one, otherwise from the environment (pytest infra/homelab)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "komodo"))
import komodo_api  # noqa: E402


def test_without_the_file_nothing_happens(tmp_path, monkeypatch):
    monkeypatch.delenv("KOMODO_URL", raising=False)
    komodo_api.load_env(tmp_path / "komodo.env")          # a new installation has no such file: no error
    import os
    assert "KOMODO_URL" not in os.environ


def test_the_file_fills_what_is_not_set_and_keeps_what_is(tmp_path, monkeypatch):
    import os
    f = tmp_path / "komodo.env"
    f.write_text("export KOMODO_URL='http://komodo:9120'\nKOMODO_API_KEY=file-key\nOTHER=x\n")
    monkeypatch.delenv("KOMODO_URL", raising=False)
    monkeypatch.setenv("KOMODO_API_KEY", "from-environment")
    komodo_api.load_env(f)
    assert os.environ["KOMODO_URL"] == "http://komodo:9120" and os.environ["KOMODO_API_KEY"] == "from-environment"
    assert "OTHER" not in os.environ
