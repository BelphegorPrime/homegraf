# The bootstrap of the homegraf stack: a one-shot container (standard library only) that configures the services against each other.
# Build from the repository root:  docker build -f bootstrap/Dockerfile -t homegraf-bootstrap .
# The seed (the initial content of the user's repository) is the folder seed/ when the repository has it, else the stub.
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS seed
COPY . /ctx
RUN mkdir /out-seed && if [ -n "$(ls -A /ctx/seed 2>/dev/null)" ]; then cp -r /ctx/seed/. /out-seed/; fi

FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Only the standard library runs here: pip, setuptools and wheel (and the libraries pip bundles, which scanners flag) are build tools.
RUN python3 -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1; \
    rm -rf /usr/local/lib/python3*/site-packages/pip* /usr/local/lib/python3*/site-packages/setuptools* /usr/local/lib/python3*/site-packages/wheel* \
           /usr/local/lib/python3*/site-packages/_distutils_hack /usr/local/lib/python3*/site-packages/pkg_resources* /usr/local/bin/pip*
# The state volume is owned by 1000, the user of the homegraf container, which reads what is written there.
RUN mkdir /state /credentials && chown 1000:1000 /state /credentials
WORKDIR /app
COPY bootstrap/hgboot /app/hgboot
COPY bootstrap/seed-stub /seed-stub
COPY --from=seed /out-seed /seed
USER 1000:1000
ENTRYPOINT ["python3", "-m", "hgboot"]
