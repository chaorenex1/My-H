"""omp.appearance.*: the omp-ui Appearance panel is profile Hermes config.

Every Appearance-page setting is a config key under ``omp.appearance``; its default is omp's own
registry value (``hermes_cli/data/omp_appearance_defaults.json``) and config.yaml stores only the
profile's overrides. The gateway answers the complete map and writes one key per edit.
"""

from __future__ import annotations

from pathlib import Path

import hermes_yaml as yaml

from hermes_cli.config_defaults_omp import OMP_APPEARANCE_DEFAULTS
from tui_gateway import server


def _use_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    server._drop_cfg_cache()
    return home


def _write_cfg(home: Path, data: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    server._drop_cfg_cache()


def _read_cfg(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def _call(name: str, params: dict) -> dict:
    return server._methods[name]("rid", params)


def test_get_answers_every_setting_with_omp_defaults(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_cfg(home, {"display": {"skin": "default"}})

    values = _call("omp.appearance.get", {})["result"]["values"]

    assert len(values) == len(OMP_APPEARANCE_DEFAULTS)
    assert values["theme.dark"] == "titanium"
    assert values["theme.light"] == "light"
    assert values["symbolPreset"] == "unicode"
    assert values["composer.shape"] == "band"
    assert values["display.advisorCollapsedLines"] == 0


def test_get_overlays_the_profiles_stored_values(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_cfg(home, {"omp": {"appearance": {
        "theme": {"dark": "light"},
        "symbolPreset": "nerd",
    }}})

    values = _call("omp.appearance.get", {})["result"]["values"]

    assert values["theme.dark"] == "light"
    assert values["symbolPreset"] == "nerd"
    assert values["theme.light"] == "light"  # sibling default survives
    assert len(values) == len(OMP_APPEARANCE_DEFAULTS)


def test_set_nests_one_setting_and_keeps_defaults_for_the_rest(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)

    assert _call("omp.appearance.set", {"key": "theme.dark", "value": "titanium"})["result"] == {
        "key": "theme.dark", "value": "titanium"}
    assert _call("omp.appearance.set", {"key": "symbolPreset", "value": "nerd"})["result"] == {
        "key": "symbolPreset", "value": "nerd"}

    assert _read_cfg(home)["omp"]["appearance"] == {
        "theme": {"dark": "titanium"},
        "symbolPreset": "nerd",
    }
    values = _call("omp.appearance.get", {})["result"]["values"]
    assert values["theme.dark"] == "titanium"
    assert values["display.shimmer"] == "classic"  # untouched default, not written to the file


def test_null_removes_the_override_and_falls_back_to_the_default(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_cfg(home, {"omp": {"appearance": {"theme": {"dark": "light", "light": "dark"}}}})

    assert _call("omp.appearance.set", {"key": "theme.dark", "value": None})["result"] == {
        "key": "theme.dark", "value": None}

    assert _read_cfg(home)["omp"]["appearance"] == {"theme": {"light": "dark"}}
    assert _call("omp.appearance.get", {})["result"]["values"]["theme.dark"] == "titanium"


def test_invalid_setting_key_is_refused_before_any_write(tmp_path, monkeypatch):
    home = _use_home(tmp_path, monkeypatch)
    _write_cfg(home, {"omp": {"appearance": {"theme": {"dark": "titanium"}}}})

    for key in ("", "theme..dark", ".theme", "theme.", "a/b", "theme.dark\n"):
        response = _call("omp.appearance.set", {"key": key, "value": "x"})
        assert response["error"]["code"] == 4002, key

    assert _read_cfg(home)["omp"]["appearance"] == {"theme": {"dark": "titanium"}}


def test_section_is_profile_scoped(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    worker = tmp_path / "worker"
    launch.mkdir()
    worker.mkdir()
    _write_cfg(launch, {"omp": {"appearance": {"symbolPreset": "ascii"}}})
    _write_cfg(worker, {"omp": {"appearance": {"symbolPreset": "nerd"}}})
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_profile_home", lambda name: worker if (name or "").strip() == "worker" else None)
    server._drop_cfg_cache()

    assert _call("omp.appearance.get", {})["result"]["values"]["symbolPreset"] == "ascii"
    assert _call("omp.appearance.get", {"profile": "worker"})["result"]["values"]["symbolPreset"] == "nerd"

    _call("omp.appearance.set", {"profile": "worker", "key": "symbolPreset", "value": "unicode"})
    assert _read_cfg(worker)["omp"]["appearance"]["symbolPreset"] == "unicode"
    assert _read_cfg(launch)["omp"]["appearance"]["symbolPreset"] == "ascii"
