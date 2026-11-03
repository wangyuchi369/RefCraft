"""Configurable vision judge via a Chat Completions compatible HTTP endpoint."""

import base64
import hashlib
import json
import mimetypes
import os
import time
from urllib.parse import urlsplit

import requests
from .common import write


class Judge:
    def __init__(self, base_url, model, directory, timeout=180, max_tokens=16384):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) base URL without embedded credentials, query or fragment"
            )
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.directory = directory
        self.timeout = timeout
        self.max_tokens = max_tokens

    def ask(self, name, policy, data, images=()):
        content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
        for label, path in images:
            content.append({"type": "text", "text": label})
            mime = mimetypes.guess_type(path)[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": policy},
                {"role": "user", "content": content},
            ],
            "max_tokens": self.max_tokens,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        key = os.environ.get("REFCRAFT_JUDGE_API_KEY")
        if not key:
            raise ValueError("Set REFCRAFT_JUDGE_API_KEY before requesting judge calls")
        started = time.monotonic()
        # Do not log requests, headers, provider error bodies, or credentials.
        try:
            with requests.Session() as session:
                # Use only the explicit bearer key and URL. Never replace them with
                # ~/.netrc credentials or route through an implicit machine proxy.
                session.trust_env = False
                response = session.post(
                    self.url,
                    json=payload,
                    headers={"Authorization": "Bearer " + key},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
        except requests.RequestException as exc:
            raise RuntimeError("judge_transport_" + type(exc).__name__) from None
        if response.status_code != 200:
            raise RuntimeError(f"judge_http_{response.status_code}")
        try:
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise ValueError("judge response did not finish normally")
            text = choice["message"]["content"].strip()
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError("judge_invalid_or_truncated_json") from None
        write(
            self.directory / (name + ".json"),
            {
                "request_sha256": digest,
                "model": self.model,
                "elapsed_seconds": time.monotonic() - started,
                "usage": body.get("usage"),
                "result": result,
            },
        )
        return result
