import types

from nap_sentinel import config, install_hook


def test_config_defaults_and_clamp(tmp_path, monkeypatch):
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "c.json"))
  c = config.load()
  assert c["enabled"] is False and c["prerecord"] is False and c["prerecord_s"] == 10
  c = config.update({"prerecord_s": 99, "sensitivity": 0, "nope": 1, "enabled": 1})
  assert c["prerecord_s"] == 30 and c["sensitivity"] == 1 and c["enabled"] is True and "nope" not in c
  assert "web_password" not in config.public(c)


def test_hook_add_is_idempotent_and_removable(tmp_path):
  f = tmp_path / "process_config.py"
  original = "procs = []\nmanaged_processes = {p.name: p for p in procs}\n"
  f.write_text(original)
  assert install_hook.add_hook(f)
  assert not install_hook.add_hook(f)
  assert f.read_text().count(install_hook.BEGIN) == 1
  compile(f.read_text(), str(f), "exec")
  assert install_hook.remove_hook(f)
  assert f.read_text() == original


def test_hook_refuses_unknown_layout(tmp_path):
  f = tmp_path / "process_config.py"
  f.write_text("x = 1\n")
  assert not install_hook.add_hook(f)


def test_hook_extends_only_offroad(monkeypatch):
  from nap_sentinel import hook
  state = {"sensors": False, "cameras": True}
  monkeypatch.setattr(hook, "_procs", lambda: state)
  p = types.SimpleNamespace(name="camerad", should_run=lambda started, params, CP: started)
  hook._extend(p, "cameras")
  assert p.should_run(True, None, None) is True     # driving: untouched
  assert p.should_run(False, None, None) is True    # parked + sentinel wants cameras
  state["cameras"] = False
  assert p.should_run(False, None, None) is False
  hook._extend(p, "cameras")                        # no double wrapping
  assert p._sentinel_wrapped


def test_stale_procs_request_expires(tmp_path, monkeypatch):
  monkeypatch.setattr(config, "PROCS_FILE", tmp_path / "p.json")
  config.write_procs(True, True)
  assert config.read_procs()["cameras"] is True
  d = config.read_json(config.PROCS_FILE)
  d["t"] -= 60
  config.atomic_write_json(config.PROCS_FILE, d)
  assert config.read_procs() == {"sensors": False, "cameras": False}
