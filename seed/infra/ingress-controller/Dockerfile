# Reproduzierbarer Build aus einem festen Quellcommit (kein Registry-Zwang): Komodo baut diesen Stack
# beim Deploy aus dem gepinnten Commit. Basis-Image per Digest gepinnt (python 3.15, bookworm).
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

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
