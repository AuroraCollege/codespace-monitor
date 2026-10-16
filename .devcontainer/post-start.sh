#!/usr/bin/env bash
set -u

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if [[ -n "${CODESPACE_NAME:-}" && -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
  python3 -c 'import json, os; url = "https://%s-5000.%s" % (os.environ["CODESPACE_NAME"], os.environ["GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"]); open("server.json", "w", encoding="utf-8").write(json.dumps({"url": url}) + "\n")'
else
  echo "Codespaces URL variables are unavailable; leaving server.json unchanged."
fi

if [[ -n "${CODESPACE_NAME:-}" ]] && command -v gh >/dev/null 2>&1; then
  gh codespace ports visibility 5000:public -c "$CODESPACE_NAME" >/dev/null 2>&1 || \
    echo "Could not set port 5000 public; check Codespaces or organisation policy."
fi

if ! pgrep -f '[p]ython3 server.py' >/dev/null 2>&1; then
  nohup python3 server.py >>/tmp/student-monitor-server.log 2>&1 </dev/null &
fi