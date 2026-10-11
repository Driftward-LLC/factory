#!/usr/bin/env python3
"""Disposable hosting acceptance: real containers/browser, synthetic app, no model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import bootstrap
import ui_verification as ui


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-image", required=True)
    parser.add_argument("--preview-image", required=True)
    args = parser.parse_args()
    temporary = Path(tempfile.mkdtemp(prefix="factory-ui-acceptance-"))
    installation = temporary / "installation"
    bootstrap.init_directory(installation, "factory:test", "factory-ui-acceptance", apply=True)
    plugin = temporary / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills/verify-app").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "driftward-engineering", "version": "0.0.0-fixture"}))
    (plugin / "skills/verify-app/SKILL.md").write_text("Synthetic acceptance only. No model or plugin behavior is verified.\n")
    row = json.loads(ui.command(["docker", "image", "inspect", args.preview_image]))[0]
    runner = json.loads(ui.command(["docker", "image", "inspect", args.runner_image]))[0]["Id"]
    request = {"source_commit": row["Config"]["Labels"]["org.opencontainers.image.revision"],
               "preview_image": row["Id"], "path": "/", "objective": "Create a synthetic agent",
               "steps": [{"action": "fill", "label": "Agent name", "value": "Factory test"},
                         {"action": "press", "key": "Tab"}, {"action": "press", "key": "Enter"},
                         {"action": "expect", "text": "Agent created"}],
               "model": "fixture-not-invoked", "timeout_seconds": 30}
    recipe = temporary / "request.json"
    recipe.write_text(json.dumps(request))
    cli = [sys.executable, str(ROOT / "deploy/factory-ui-verify.py"), "--directory", str(installation),
           "--request", str(recipe), "--runner-image", runner, "--plugin-directory", str(plugin), "--fixture"]
    preview = subprocess.run(cli, capture_output=True, text=True, check=True)
    assert 'Preview only' in preview.stdout and not (installation / "ui-verification").exists()
    result = subprocess.run([*cli, "--apply"], capture_output=True, text=True, timeout=480)
    if not result.stdout:
        raise RuntimeError("acceptance launcher failed before creating a report")
    response = json.loads(result.stdout)
    report = json.loads(Path(response["evidence"]).read_text())
    assert result.returncode == 0 and report["status"] == "fixture_passed", response
    assert report["cleanup_complete"] and report["factory_verified"] is False
    assert not list(Path(response["evidence"]).parent.joinpath("secrets").iterdir())
    assert {'checks/viewport-1280.png', 'checks/viewport-390.png', 'checks/trace-1280.zip',
            'checks/trace-390.zip', 'checks/checks.json', 'agent/agent.json'} <= report['artifacts'].keys()
    # Failure must retain evidence, clean its containers, and never become a pass.
    request['steps'][-1]['text'] = 'This text must not exist'
    recipe.write_text(json.dumps(request))
    failed = subprocess.run([*cli, "--apply"], capture_output=True, text=True, timeout=480)
    failure = json.loads(failed.stdout)
    assert failed.returncode != 0 and failure['cleanup_complete'] and not failure['factory_verified'], failure
    # Cancel a live browser wait. The launcher, not the model, must stop all roles.
    running = subprocess.Popen([*cli, "--apply"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            active = ui.command(["docker", "ps", "-q", "--filter",
                                 "label=io.driftward.factory.installation=" + report["installation_id"],
                                 "--filter", "label=com.docker.compose.service=checks"])
            if active:
                break
            if running.poll() is not None:
                raise RuntimeError("cancellation probe ended before its browser started")
            time.sleep(0.2)
        else:
            raise RuntimeError("cancellation probe browser did not start")
        running.terminate()
        output, _ = running.communicate(timeout=90)
        cancelled = json.loads(output)
        assert running.returncode != 0 and cancelled["reason"] == "cancelled", cancelled
        assert cancelled["cleanup_complete"] and not cancelled["factory_verified"], cancelled
    finally:
        if running.poll() is None:
            running.terminate()
            running.communicate(timeout=90)
    print(json.dumps({"status": "passed", "mode": "synthetic_browser_acceptance", "evidence": str(temporary)}))


if __name__ == '__main__':
    main()
