#!/bin/bash
set -e

# This image doesn't build/install isolate: it needs a cgroup v2 host
# kernel, and Docker containers share the host's kernel/cgroup version --
# no container flag can give one to a host booted into cgroup v1 (e.g.
# Oracle Linux 8's default). grader/sandbox.py's `--sandbox auto` already
# handles this per-run: it self-tests isolate and, if unavailable, falls
# back to SubprocessRlimitSandbox (real CPU/wall-time/memory/process-count
# limits via rlimits, without filesystem/network namespace isolation).
echo "[entrypoint] Sandbox backend: subprocess (rlimits) -- isolate is not installed in this image."

if [[ ! -f /app/.env ]]; then
    echo "[entrypoint] No .env found -- this is a first run."
    echo "[entrypoint] Bootstrap the admin account with:"
    echo "  docker compose run --rm admin python -m grader.manage_accounts init-admin"
    echo "[entrypoint] Continuing to start the requested service; admin login will fail until .env exists."
fi

exec "$@"
