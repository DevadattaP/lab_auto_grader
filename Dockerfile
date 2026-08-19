FROM python:3.12-slim

# gcc/libc6-dev: compiles student C submissions.
# Real per-run sandboxing via `isolate` needs a cgroup v2 host kernel, which
# this image intentionally does not assume (see docker/entrypoint.sh) --
# the grader's own --sandbox=auto falls back to SubprocessRlimitSandbox
# (CPU/wall-time/memory/process-count limits via rlimits) when isolate isn't
# usable, which is what this image runs on.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p questions submissions runs live

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
