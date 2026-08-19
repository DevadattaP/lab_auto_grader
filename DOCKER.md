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
   skip this if `.env` already exists (e.g. copied from another machine):
   ```bash
   cd lab_auto_grader
   docker compose build
   docker compose run --rm admin python -m grader.manage_accounts init-admin
   ```
   This prompts for an admin username/password and writes `.env` into the
   project directory (bind-mounted into both containers).

4. Make sure `questions/lab_01/` (or whichever lab) has real question
   content — it's bind-mounted from the host, so edit it directly on the
   lab PC, no rebuild needed.

## Running

```bash
docker compose up -d
```

- Student server: `http://<lab-pc-ip>:5001` — this is what the other 35 PCs
  connect to.
- Admin server: `http://<lab-pc-ip>:5002/admin` — session control,
  finalize/report, live dashboard.

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

## Opening the firewall (firewalld, Oracle Linux 8 default)

Only the student port needs to reach the other 35 PCs; keep the admin port
restricted to the instructor's own use if possible.

```bash
sudo firewall-cmd --permanent --add-port=5001/tcp
sudo firewall-cmd --permanent --add-port=5002/tcp   # only if admin needs LAN access too
sudo firewall-cmd --reload
```

To restrict a port to the lab's subnet only (recommended for 5002), use a
rich rule instead, e.g. for a `192.168.1.0/24` lab LAN:
```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="5002" protocol="tcp" accept'
sudo firewall-cmd --reload
```

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
