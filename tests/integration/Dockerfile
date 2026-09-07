FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python3 -m venv /opt/test-venv \
    && /opt/test-venv/bin/python -m pip install --require-hashes -r requirements.lock \
    && /opt/test-venv/bin/python -m pip install --no-build-isolation --no-deps . \
    && /opt/test-venv/bin/python -c "import home_agent" \
    && /opt/test-venv/bin/agentctl --help
