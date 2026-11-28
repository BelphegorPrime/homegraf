# Reproducible build from a fixed source commit (no registry required): Komodo builds this stack
# on deploy from the pinned commit. Base image pinned by digest (python 3.15, bookworm).
FROM python:3.15-slim-bookworm@sha256:498829c717b1e59fc42f0e7865eafe4e04ca0f70a3769d4ae70ba218df54507c

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory /repo \
 && useradd --system --uid 10001 --home /var/lib/ingressctl ingressctl \
 && install -d -o 10001 -m 0700 /var/lib/ingressctl

WORKDIR /opt/ingressctl
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && python -c "import ingressctl"

ENV INGRESSCTL_STATE_DIR=/var/lib/ingressctl \
    PYTHONUNBUFFERED=1
USER ingressctl
ENTRYPOINT ["ingressctl"]
CMD ["--help"]
