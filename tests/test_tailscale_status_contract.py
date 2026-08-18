import json
import subprocess
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_contract_check(tmp_path, status_payload, command):
    status_json = json.dumps(status_payload)
    script = tmp_path / "check_tailscale_contract.ps1"
    script.write_text(
        textwrap.dedent(
            f"""
            . "{PROJECT_ROOT / 'tools' / 'app_contract.ps1'}"
            $Status = @'
            {status_json}
            '@ | ConvertFrom-Json
            {command} | ConvertTo-Json -Compress
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_tailscale_contract_accepts_public_443_and_private_8443(tmp_path):
    status = {
        "Web": {
            "desktop.tail27cee7.ts.net:443": {
                "Handlers": {
                    "/": {"Proxy": "http://127.0.0.1:8099"},
                    "/shocks_art": {"Proxy": "http://127.0.0.1:8000/shocks_art"},
                }
            },
            "desktop.tail27cee7.ts.net:8443": {
                "Handlers": {
                    "/shocks_art": {"Proxy": "http://127.0.0.1:8000/shocks_art"},
                }
            },
        },
        "AllowFunnel": {"desktop.tail27cee7.ts.net:443": True},
    }

    public = run_contract_check(
        tmp_path,
        status,
        (
            'Test-TailscaleRouteContract -Status $Status -HttpsPort 443 '
            '-Path "/shocks_art" -Target "http://127.0.0.1:8000/shocks_art" -RequireFunnel'
        ),
    )
    private = run_contract_check(
        tmp_path,
        status,
        (
            'Test-TailscaleRouteContract -Status $Status -HttpsPort 8443 '
            '-Path "/shocks_art" -Target "http://127.0.0.1:8000/shocks_art" -RequirePrivate'
        ),
    )

    assert public["Ok"] is True
    assert public["Mode"] == "Funnel/public"
    assert private["Ok"] is True
    assert private["Mode"] == "Serve/tailnet-only"


def test_tailscale_contract_rejects_tailnet_only_443(tmp_path):
    status = {
        "Web": {
            "desktop.tail27cee7.ts.net:443": {
                "Handlers": {
                    "/shocks_art": {"Proxy": "http://127.0.0.1:8000/shocks_art"},
                }
            }
        },
        "AllowFunnel": {},
    }

    result = run_contract_check(
        tmp_path,
        status,
        (
            'Test-TailscaleRouteContract -Status $Status -HttpsPort 443 '
            '-Path "/shocks_art" -Target "http://127.0.0.1:8000/shocks_art" -RequireFunnel'
        ),
    )

    assert result["Ok"] is False
    assert result["Mode"] == "Serve/tailnet-only"
    assert "switched back to Serve/tailnet-only" in result["Message"]
