# Running lab_auto_grader in Docker

This runs the live-lab platform (`server_student` + `server_admin`) in
containers so the lab PC needs nothing installed except Docker itself — no
Python, no gcc, no `isolate`, no host cgroup changes.

## Why there's no `isolate` sandbox in this image

`grader/sandbox.py` supports two backends: `isolate` (strong per-run
filesystem/network isolation) and a `subprocess` fallback (CPU/wall-time/
memory/process-count limits via rlimits, no filesystem/network isolation
between runs). `isolate` 2.x requires a **cgroup v2 host kernel** — that's a
property of the host machine, not something a container can override, since
containers share the host's kernel.

Oracle Linux 8 (and RHEL 8) boot into **cgroup v1** by default. Getting
`isolate` working would mean adding `systemd.unified_cgroup_hierarchy=1` to
the host's GRUB config and rebooting — a host change outside Docker. Since
the goal here is zero host changes, this image ships without `isolate` and
runs the grader with `--sandbox subprocess`. Student programs still get real
CPU/wall-time/memory/process-count limits; they just run in the same
container/filesystem as the grader rather than a hardened per-run sandbox —
an acceptable tradeoff for known lab students on an internal LAN.

If a future lab PC already runs cgroup v2 (check with
`stat -fc %T /sys/fs/cgroup` — `cgroup2fs` means v2), `isolate` can be added
back; ask for the Dockerfile changes to build it in and switch
`docker-compose.yml` to `--sandbox auto --privileged`.

Both `docker-compose.yml` services pass `--sandbox subprocess` explicitly
(the student server's own flag, and a `--sandbox` flag added to
`server_admin/app.py` so its "Finalize & Grade" action — which shells out to
`grader.grade`, whose own default is `--sandbox isolate` — doesn't try
isolate and hard-crash the grading run).

## Why both containers run as root

Both services keep the image's default root user, not a mapped host UID.
That's not for convenience — `SubprocessRlimitSandbox` (the fallback in use
here) sets `RLIMIT_NPROC` on every compile/run to bound student fork-bombs.
That's a **Linux kernel-wide, per-UID** limit, not a per-container one, and
the kernel only exempts UID 0 from it. A non-root container UID gets charged
against that UID's *entire host* process count — on a real machine with
other things running under the same UID, that count can already exceed
whatever budget the sandbox computes, causing spurious
`cc1: posix_spawn: Resource temporarily unavailable` compile failures that
have nothing to do with the student's code. Root sidesteps this entirely.

The tradeoff: files these containers write into the bind-mounted
`questions/`, `submissions/`, `runs/`, `live/`, and `.env` come out
**root-owned** on the host. If that gets in the way of editing them as
yourself later:

```bash
sudo chown -R $(id -u):$(id -g) questions submissions runs live .env
```

(or, without `sudo` on the host, run it via a throwaway container that has
root inside, same trick used to reach these files in the first place:
`docker run --rm -v "$PWD":/fix -w /fix lab-auto-grader:latest chown -R $(id -u):$(id -g) questions submissions runs live .env`)

## Concurrency and fork-bomb hardening

`SandboxPool` (`grader/sandbox_pool.py`) hands each concurrent Run/Finalize
its own `Sandbox` instance from a bounded pool (`--sandbox-slots`, 8 in
`docker-compose.yml` — see "Choosing --sandbox-slots" below) — verified by
hand: concurrent students overlap in wall-clock time (finish in ~1x a
single run's duration, not ~Nx), excess concurrent requests correctly queue
for a free slot rather than erroring or corrupting a result, and outputs
never cross-contaminate between students under load — including a genuine
35-way concurrent burst through the real HTTP API (35 separate simulated
students, each its own container/IP, all logging in and hitting Run at
once): all 35 completed successfully, no `PoolExhausted` 503s. This is
unchanged from `isolate` mode — `SandboxPool` doesn't know or care which
backend it's pooling.

That same 35-way test surfaced the actual bottleneck at real class-size
bursts: not the sandbox pool, but the cross-process file lock
(`grader/accounts.py`'s `_csv_lock`) that `live/accounts.csv` and each lab's
`students.csv` share — 35 simultaneous logins queue through it one at a
time, so login (not compile/run) is what a student waits several seconds
for if everyone logs in in the same instant. Not a failure mode (everyone
still gets in), just the realistic shape of a start-of-class rush.

The one real gap `subprocess` mode has that `isolate` doesn't: a
fork-bombing submission (`while(1) fork();`). `SubprocessRlimitSandbox` sets
`RLIMIT_NPROC` to bound exactly this, but that limit is a Linux kernel
per-UID one that **exempts root** — and both containers run as root (see
above). Confirmed by hand: run as root, a fork-bomb saturates the
container's entire process table and the container **never self-recovers**
(`docker exec` fails with `procReady not received` indefinitely; a manual
`docker compose restart student` is required), even with a Docker-level
`pids_limit` already in place — a plain fork loop can out-fork a single
`SIGKILL` sweep faster than it can be reaped.

Fixed at the source: `SubprocessRlimitSandbox.run()` (not `compile_c()` —
gcc still runs as the invoking user) now drops the *compiled student
binary* to the `nobody` account before exec, whenever running as root. Once
unprivileged, `RLIMIT_NPROC` is enforced by the kernel atomically at the
`fork()` syscall itself — no race, no sweep, nothing to out-fork. Re-tested
the identical fork-bomb scenario after this fix: 3 concurrent normal
students complete cleanly, the bomb is killed at its wall-time limit, and
the container's process count returns to baseline immediately, every time
across repeated runs. `pids_limit: 512` stays in `docker-compose.yml` as a
backstop, not the primary defense.

### Choosing `--sandbox-slots`

Each slot is one concurrently-running `gcc` compile or student binary.
Under `subprocess` sandboxing this is **CPU-bound, not memory-bound** — a
32GB lab PC has far more RAM than 35 students' compiles need even all at
once (each uses tens of MB, briefly); what actually runs out is CPU cores.

Benchmarked with `docker/slots_bench.py` (simulates N concurrent students'
compile+run through the real `SandboxPool`) on an 8-core/8GB dev machine:
throughput scaled close to linearly from 1→4 slots, then plateaued around
8 (≈ the core count) — pushing to 12 or 16 slots gave no real further
throughput gain and individual compiles started taking *longer* (more
processes competing for the same cores). `--sandbox-slots 8` in
`docker-compose.yml` is set from that result, expected to suit a typical
lab-PC i7 (8+ cores/threads) reasonably well — but it was measured on
different hardware than the actual lab PC, so treat it as a starting point
worth re-checking, not a proven-optimal number for that specific machine.

To re-run the sweep on any machine:

```bash
docker run -d --name bench_container --pids-limit 2048 lab-auto-grader:latest tail -f /dev/null
docker cp docker/slots_bench.py bench_container:/tmp/
docker exec -w /app bench_container bash -c \
    "PYTHONPATH=/app python3 /tmp/slots_bench.py --sweep 1,2,4,8,12,16 --students 35"
docker rm -f bench_container
```

Look at where `throughput` (students/s) stops climbing and
`avg_compile_run` starts rising — that's the plateau. Set `--sandbox-slots`
in `docker-compose.yml`'s `student` service to roughly that point, then
`docker compose up -d student` to apply it.

## One-time setup on the lab PC

1. Install Docker Engine + Compose plugin (Oracle Linux 8):

   ```bash
   sudo dnf install -y dnf-utils
   sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
   sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
   sudo systemctl enable --now docker
   sudo usermod -aG docker $USER   # log out/in after this
   ```

2. Copy this repo to the lab PC (or `git clone` it).

3. Bootstrap the `.env` file (admin username/password + session secrets) —
   skip the `touch` if `.env` already exists (e.g. copied from another
   machine):

   ```bash
   cd lab_auto_grader
   touch .env
   docker compose build
   docker compose run --rm admin python -m grader.manage_accounts init-admin
   ```

   The `touch` matters: `docker-compose.yml` bind-mounts `.env` into both
   containers, and if the file doesn't exist on the host yet, Docker creates
   the mount point as a **directory** instead — the container then fails
   with `IsADirectoryError: [Errno 21] Is a directory: '/app/.env'` the
   moment it tries to read it. `touch .env` first avoids that entirely.

   If you hit that error anyway (e.g. ran `docker compose up`/`run` before
   creating the file): `docker compose down`, then `rmdir .env` to remove
   the directory Docker created, then `touch .env` and retry.

   `init-admin` prompts for an admin username/password and writes the real
   `.env` contents into that file (bind-mounted into both containers, so no
   rebuild needed afterward).

4. Make sure `questions/lab_01/` (or whichever lab) has real question
   content — it's bind-mounted from the host, so edit it directly on the
   lab PC, no rebuild needed.

## Running

```bash
docker compose up -d
```

- Student server: `http://<lab-pc-ip>:5001` — this is what the other 35 PCs
  connect to. `docker-compose.yml` publishes it on `0.0.0.0` (all
  interfaces), reachable from the LAN.
- Admin server: `http://localhost:5002/admin` — session control,
  finalize/report, live dashboard. `docker-compose.yml` publishes this one
  as `127.0.0.1:5002:5002` — bound to the lab PC's own loopback only.
  Confirmed by hand: connecting to `http://<lab-pc-ip>:5002` from another
  machine on the LAN fails outright (nothing is listening on that
  interface), while the same request to port 5001 succeeds. This holds
  regardless of firewall state — there's no need to also block 5002 in
  `firewalld`, since Docker never binds it anywhere the firewall would need
  to catch. Manage the session from the lab PC itself, or over SSH port
  forwarding (`ssh -L 5002:localhost:5002 <lab-pc>`) from elsewhere.

Check logs:

```bash
docker compose logs -f student
docker compose logs -f admin
```

Stop:

```bash
docker compose down
```

Data in `questions/`, `submissions/`, `runs/`, `live/`, and `.env` lives on
the host (bind-mounted), so `docker compose down` (without `-v`, and there
are no named volumes here anyway) never deletes it.

## Firewall — you likely don't need to touch it

Confirmed by hand on the lab PC: with no `firewalld` rule for 5001 at all,
the student server was already reachable from the other lab PCs. This
matches Docker's documented behavior — it manages its own iptables rules to
implement `ports:` publishing, and on most setups (including this one)
those rules take effect ahead of `firewalld`'s own filtering, so a
published container port bypasses `firewalld`'s zone rules rather than
needing one opened for it. (5002/admin is a non-issue either way — it's
bound to loopback only, so it was never reachable from the LAN regardless
of firewall state; see above.)

## Day-to-day operations

Three different kinds of change, three different commands. The dividing
line: anything under `questions/`, `submissions/`, `runs/`, `live/`, or
`.env` is **bind-mounted** straight from the host disk (see the `volumes:`
list in `docker-compose.yml`) — containers see edits to those instantly,
live, no rebuild or restart. Everything else (`grader/`, `server_student/`,
`server_admin/`, `requirements.txt`, ...) is **baked into the image** at
build time via the Dockerfile's `COPY . .`, so it only updates on a rebuild.

### 1. Adding a new lab (new `questions/lab_03/` folder, etc.)

Nothing to run beyond creating the folder — no copy command needed:

```bash
mkdir -p questions/lab_03
# ...add question.yaml files, solution.c, etc...
```

Both containers read `questions/` live off the host. The admin server picks
it up immediately (it discovers labs by scanning the directory). The student
server needs telling *which* lab to serve — see next.

### 2. Switching which lab the student server serves

Edit the `--lab` value in `docker-compose.yml`'s `student` service
`command:` block (currently `--lab lab_01`), keeping `--port 5001` as-is,
then recreate just that container:

```bash
docker compose up -d student
```

This is a container recreate, not an image rebuild — a few seconds, and it
doesn't touch the `admin` container or drop any host-side data. The `admin`
service's command never needs to change between labs; it discovers labs
from `questions/` and takes a `--lab` as part of each API call/URL, not a
startup flag.

### 3. Pulling in code changes from the repo (grader/, server_student/, server_admin/, ...)

These are baked into the image, so they need a rebuild:

```bash
git pull
docker compose up -d --build
```

`--build` rebuilds the image from the current source and recreates both
containers from it. Host-mounted data (`questions/`, `submissions/`, `runs/`,
`live/`, `.env`) is untouched either way — only the code inside the image
changes.

If you only want to rebuild without restarting yet (e.g. to catch build
errors before committing to a restart during a live session):

```bash
docker compose build
```

then `docker compose up -d --build` (or just `docker compose up -d`, which
will already use the freshly built image) when ready to switch over.

## Notes

- Both services run Flask's built-in dev server (`app.run(...)`), same as
  running it directly on bare metal — this is what the existing codebase
  ships, unchanged by containerizing it. It's single-process; 35 students
  polling every `--heartbeat-interval-seconds` (default 60s) plus occasional
  Run/Submit requests is a light load for it, but it's not a
  production-grade WSGI server. If this becomes a bottleneck, ask about
  swapping to gunicorn — out of scope for this containerization pass.
- `ui/app.py` (the unauthenticated offline question/submission browser) is
  intentionally **not** included as a container service — it has no login
  and binds to `127.0.0.1` by design. The same browsing UI is already
  available, behind admin login, at `http://<lab-pc-ip>:5002/admin`.
