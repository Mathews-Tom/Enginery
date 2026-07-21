# enginery-stage3-local-service

Disposable Stage 3 controlled deployment target for [Enginery](https://github.com/Mathews-Tom/Enginery).

This is a real, minimal, stdlib-only HTTP service (`app.py`) whose sole
purpose is to be deployed and rolled back by Enginery's local-service
deployment broker (`enginery.adapters.local_service`). It reports its own
revision, a health status, and an intentionally injectable defect so the
Stage 3 incident-to-hotfix workflow can be proven against a real running
process rather than a simulation: a real deploy starts a real subprocess
bound to a real port, and a real rollback restarts it against a prior
configuration and is observed to restore the prior revision.

It is not a usable library, ships no dependency beyond the Python
standard library, and never shares a distribution name or version
namespace with the `enginery` product package itself.

## Usage

```sh
python3 app.py --config <path-to-config.json> --port <port> [--host 127.0.0.1]
```

`config.json` is `{"revision": "<label>", "defect_mode": "none" | "increment_off_by_one" | "health_degraded"}`.

## Endpoints

- `GET /version` -> `{"revision": "<label>", "pid": <int>}`
- `GET /health` -> `{"status": "healthy"}` or `{"status": "unhealthy"}` (when `defect_mode` is `health_degraded`)
- `POST /increment` body `{"value": <int>}` -> `{"result": <int>}` (`value + 1`, or `value + 2` when `defect_mode` is `increment_off_by_one`)
