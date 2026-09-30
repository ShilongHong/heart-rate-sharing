"""Forward one dispatch payload; never print payloads or credentials."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    target = os.environ["RELAY_TARGET_URL"]
    parsed = urlsplit(target)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("RELAY_TARGET_URL must be an HTTPS endpoint")
    token = os.environ["RELAY_TARGET_TOKEN"]
    if not token:
        raise ValueError("Missing relay token")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    payload = event["client_payload"]
    # The receiver performs schema, age and ordering validation.
    request = Request(target, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Relay rejected sample")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Relay failed: check secrets, endpoint, payload age and receiver availability.", file=sys.stderr)
        sys.exit(1)
