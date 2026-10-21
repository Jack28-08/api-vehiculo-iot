"""Parser y renderizado Windows: sin servicios, ACL, firewall, descargas o RDS."""

import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PS = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
COMMON = ". './deploy/windows/Windows-ServiceCommon.ps1'; "


@unittest.skipUnless(os.name == "nt" and PS.is_file(), "Requiere parser Windows PowerShell 5.1")
class WindowsDeploymentTests(unittest.TestCase):
    def ps(self, source):
        result = subprocess.run(
            [str(PS), "-NoProfile", "-NonInteractive", "-Command", source],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-1200:])
        return result.stdout.strip()

    def test_native_parser_accepts_every_deployment_script(self):
        output = self.ps(
            "$count=0; Get-ChildItem './deploy/windows/*.ps1' | ForEach-Object {"
            "$t=$null; $e=$null; [void][Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$t,[ref]$e);"
            "if($e.Count){throw 'parser_error'}; $count++}; $count"
        )
        self.assertEqual(output, "3")

    def test_installers_refuse_without_destination_switch(self):
        for name in ["Install-ApiService.ps1", "Install-CaddyService.ps1"]:
            with self.subTest(script=name):
                # -File evaluates parameter defaults differently in Windows PS5.1.
                # This catches the real empty-PSScriptRoot regression before the guard.
                result = subprocess.run(
                    [str(PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(ROOT / "deploy/windows" / name)],
                    cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Sin cambios.", result.stderr)
                self.assertIn("InstallOnThisServer", result.stderr)

    def test_api_template_uses_exact_account_loopback_one_worker_and_safe_logs(self):
        source = COMMON + r"""
            [xml]$x=New-ApiServiceXml -Python 'C:\Python & IoT\python.exe' -Project 'C:\IoT\repo' -LogPath 'C:\IoT\logs';
            @{executable=$x.service.executable; arguments=$x.service.arguments;
              domain=$x.service.serviceaccount.domain; user=$x.service.serviceaccount.user;
              start=$x.service.startmode; stop=$x.service.stoptimeout} | ConvertTo-Json -Compress
        """
        rendered = json.loads(self.ps(source))
        self.assertEqual(rendered["executable"], r"C:\Python & IoT\python.exe")
        self.assertEqual((rendered["domain"], rendered["user"]), ("NT AUTHORITY", "LocalService"))
        self.assertIn("--host 127.0.0.1 --port 8000 --workers 1", rendered["arguments"])
        self.assertIn("--no-access-log", rendered["arguments"])
        self.assertNotIn("--reload", rendered["arguments"])
        self.assertEqual(rendered["start"], "Automatic")

    def test_caddy_template_persists_certificates_and_redacts_both_loggers(self):
        config = self.ps(COMMON + r"New-CaddyConfig -Hostname 'iot.example.com' -DataPath 'C:\IoT\caddy\data' -AccessLog 'C:\IoT\caddy\logs\access.json'")
        self.assertIn('storage file_system "C:/IoT/caddy/data"', config)
        self.assertIn("reverse_proxy 127.0.0.1:8000", config)
        self.assertIn("admin off", config)
        self.assertNotIn("tls internal", config)
        self.assertEqual(config.count("request>headers delete"), 2)
        self.assertEqual(config.count("request>uri delete"), 2)
        self.assertEqual(config.count("resp_headers delete"), 2)

    def test_caddy_template_rejects_hostname_injection(self):
        output = self.ps(COMMON + r"""
            foreach($h in @('https://evil.example.com','evil.example.com:443','evil.example.com/path',"evil.example.com`n{",'*.example.com')) {
                $caught=$false; try { New-CaddyConfig -Hostname $h -DataPath 'C:\IoT\data' -AccessLog 'C:\IoT\log' | Out-Null } catch {$caught=$true}
                if(-not $caught){throw 'hostname_injection_accepted'}
            }; 'rejected'
        """)
        self.assertEqual(output, "rejected")

    def test_service_directory_cannot_capture_repo_or_parent(self):
        output = self.ps(COMMON + r"""
            foreach($d in @('C:\','C:\IoT','C:\IoT\repo','C:\IoT\repo\service')) {
                $caught=$false; try { Get-SafeServicePath -Path $d -ProjectRoot 'C:\IoT\repo' | Out-Null } catch {$caught=$true}
                if(-not $caught){throw 'unsafe_acl_scope_accepted'}
            }; 'rejected'
        """)
        self.assertEqual(output, "rejected")


if __name__ == "__main__":
    unittest.main()
