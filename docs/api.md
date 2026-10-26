# Local HTTP API

Base URL: `http://127.0.0.1:<port>` from `project.json`. Start the configured app before calling the API. JSON responses are used except for images. POST requests use JSON unless noted otherwise.

This interface is for a trusted local user and local agents. There is no authentication. The server binds loopback and rejects unexpected Host, Origin, and cross-site browser headers. Do not expose it through a public proxy or treat it as a multi-user service.

| Method | Path | Purpose / body |
| --- | --- | --- |
| GET | `/api/assets` | All non-missing assets, project path, masked settings, inbox path, available sources. |
| GET | `/api/asset/<guid>` | Slices, usages, and attempt history. |
| GET | `/api/manual/<guid>` | Prepare the input and return `prompt`; also selects the current manual asset. |
| GET | `/api/manual/batch?limit=10` | Combined prompt and count from the manual queue. |
| GET | `/api/scan` | Scan state and log. |
| GET | `/api/progress` | Counts, worker state, and inbox log. |
| GET | `/img/<guid>` | Original image bytes. |
| GET | `/work/<guid>/<filename>` | Prepared input or attempt image. |
| POST | `/api/update` | `{"guids":["..."],"fields":{"category":"UI","mode":"recolor"}}`. Editable fields: category, subcategory, mode, notes, custom_prompt. |
| POST | `/api/queue` | `{"guids":["..."],"source":"manual"}`. Sources: manual, test, api when configured. |
| POST | `/api/import/<guid>` | Raw image bytes, typically PNG; records an attempt and check results. |
| POST | `/api/review` | `{"guid":"...","verdict":"accept"}`; see verdicts below. |
| POST | `/api/apply` | `{"guids":["..."]}`; omitted guids applies all accepted assets. Returns applied count and skipped reasons. |
| POST | `/api/rollback` | `{"guids":["..."]}`; omitted guids restores all tracked applied assets. Returns restored count and skipped reasons. |
| POST | `/api/settings` | Partial settings object, such as `{"style_prompt":"Watercolor","design_rules":"Keep rarity colors"}`. Returns masked settings. |
| POST | `/api/settings/test-key` | `{"key":"..."}` or `{}` to check the stored provider key. Makes an external request. |
| POST | `/api/manual/close` | `{}` clears current manual selection. |
| POST | `/api/scan` | `{}` starts a background scan. Poll GET for completion. |
| POST | `/api/quit` | `{}` stops the app. |

Review verdicts: `approve` is an intermediate approval; `accept` is final sign-off and makes an attempt eligible for apply; `reject` records optional `reason` and requeues; `override` moves a failed attempt to generated for review; `reset` returns it to pending. Review does not enforce passing checks: clients must inspect check results and obtain deliberate user sign-off before acceptance. Applied assets must be rolled back before being reviewed again.

Typical statuses: pending → awaiting_manual / queued → generated / checks_failed → approved / accepted → applied. Rejection and rollback revisit earlier states. Poll the API for actual state instead of assuming a timing sequence.

Example, from a Python agent on the same machine:

```python
import json
import urllib.request

base = "http://127.0.0.1:8765"
with urllib.request.urlopen(base + "/api/assets") as response:
    catalog = json.load(response)
print(len(catalog["assets"]))

def post(path, body):
    request = urllib.request.Request(
        base + path, json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.load(response)

# Use GUIDs from the catalog; never derive identity from a filename alone.
# post("/api/queue", {"guids": [guid], "source": "manual"})
```

Do not log request bodies containing provider keys. The API is an early-release interface and may evolve; pin a tool version for integrations.
