#!/usr/bin/env python3
"""
Minimal Bambu Lab Cloud API client - used only to read print-task history.

The printer itself never reports how much filament a job used; the cloud does,
because the slicer computed it. `get_tasks()` returns per-job weight in grams,
usually with a per-AMS-slot breakdown for multi-material prints.

Only stdlib is used, so nothing extra has to be installed on the NAS.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "https://api.bambulab.com"
WEB = "https://bambulab.com"
UA = "bambu-monitor/1.0"


class CloudError(RuntimeError):
    pass


class BambuCloud:
    def __init__(self, token: str | None = None):
        self.token = token

    # ---- plumbing ----------------------------------------------------------
    def _request(self, method: str, url: str, body=None, auth=True) -> dict:
        headers = {"User-Agent": UA, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise CloudError(f"HTTP {e.code} on {url}: {detail}") from None
        except urllib.error.URLError as e:
            raise CloudError(f"cannot reach {url}: {e.reason}") from None

    # ---- authentication ----------------------------------------------------
    def login(self, email: str, password: str) -> dict:
        """Returns the raw login result. Three outcomes:
        - accessToken present            -> done
        - loginType == 'verifyCode'      -> call send_code() then login_code()
        - tfaKey present                 -> call login_tfa()
        """
        res = self._request("POST", f"{API}/v1/user-service/user/login",
                            {"account": email, "password": password}, auth=False)
        if res.get("accessToken"):
            self.token = res["accessToken"]
        return res

    def send_code(self, email: str) -> None:
        self._request("POST", f"{API}/v1/user-service/user/sendemail/code",
                      {"email": email, "type": "codeLogin"}, auth=False)

    def login_code(self, email: str, code: str) -> dict:
        res = self._request("POST", f"{API}/v1/user-service/user/login",
                            {"account": email, "code": code}, auth=False)
        if res.get("accessToken"):
            self.token = res["accessToken"]
        return res

    def login_tfa(self, tfa_key: str, tfa_code: str) -> dict:
        res = self._request("POST", f"{WEB}/api/sign-in/tfa",
                            {"tfaKey": tfa_key, "tfaCode": tfa_code}, auth=False)
        tok = res.get("accessToken") or res.get("token")
        if tok:
            self.token = tok
        return res

    # ---- data --------------------------------------------------------------
    def get_tasks(self, serial: str | None = None, limit: int = 20) -> list[dict]:
        url = f"{API}/v1/user-service/my/tasks?limit={int(limit)}"
        if serial:
            url += f"&deviceId={serial}"
        res = self._request("GET", url)
        return res.get("hits") or res.get("list") or []
