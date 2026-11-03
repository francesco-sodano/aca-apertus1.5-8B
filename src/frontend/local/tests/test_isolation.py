import json
import os
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

FRONTEND_ROOT = Path(__file__).resolve().parents[2]


def test_cloud_distribution_does_not_package_local_modules():
    manifest = tomllib.loads((FRONTEND_ROOT / "pyproject.toml").read_text())
    packaging = manifest["tool"]["setuptools"]
    assert "apertus_local" not in packaging["packages"]
    assert "app_local" not in packaging["py-modules"]
    assert not list((FRONTEND_ROOT / "apertus_frontend").glob("local_*.py"))
    assert (FRONTEND_ROOT / "local" / "uv.lock").is_file()
    assert (FRONTEND_ROOT / "uv.lock").is_file()


@pytest.mark.parametrize("required", [
    "MODEL_ENDPOINT", "VLLM_API_KEY", "CONTENT_SAFETY_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT", "MODEL_HEALTH_TOKEN",
])
def test_cloud_settings_still_require_their_services(monkeypatch, required):
    settings = runpy.run_path(str(FRONTEND_ROOT / "apertus_frontend" / "settings.py"))["Settings"]
    for name in (
        "MODEL_ENDPOINT", "VLLM_API_KEY", "CONTENT_SAFETY_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT", "MODEL_HEALTH_TOKEN",
    ):
        monkeypatch.setenv(name, "required-cloud-value")
    monkeypatch.delenv(required)
    with pytest.raises(RuntimeError, match=required):
        settings.from_env()


def test_local_bootstrap_cannot_import_cloud_clients(tmp_path):
    script = """
import asyncio
import importlib.abc
import json
import sys

class RejectCloud(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "azure" or fullname.startswith("azure."):
            raise AssertionError("The local application attempted an Azure import.")
        return None

sys.meta_path.insert(0, RejectCloud())
import app_local
service = app_local.get_runtime()
assert service.settings.endpoint == "http://inference:8000"
assert "app" not in sys.modules
assert "apertus_frontend.azure_services" not in sys.modules
asyncio.run(app_local.shutdown_runtime())
print(json.dumps({"local_only": True, "cloud_entrypoint_imported": False}))
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(FRONTEND_ROOT),
        "CHAINLIT_APP_ROOT": str(tmp_path),
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "CLOUD_SENTINEL",
        "CONTENT_SAFETY_ENDPOINT": "CLOUD_SENTINEL",
        "FOUNDRY_PROJECT_ENDPOINT": "CLOUD_SENTINEL",
        "AZURE_CLIENT_SECRET": "CLOUD_SENTINEL",
    }
    environment.pop("LOCAL_MODEL_ENDPOINT", None)
    environment.pop("LOCAL_MAX_OUTPUT_TOKENS", None)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment,
        text=True, capture_output=True, check=True, timeout=30,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "local_only": True, "cloud_entrypoint_imported": False,
    }
