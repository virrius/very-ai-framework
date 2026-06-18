import importlib.util
from pathlib import Path


def _load_api_module():
    path = Path(__file__).resolve().parents[2] / "services" / "api" / "main.py"
    spec = importlib.util.spec_from_file_location("api_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_handler_is_defined():
    mod = _load_api_module()
    assert hasattr(mod, "Handler")
    assert hasattr(mod.Handler, "do_GET")
