FROM ubuntu:24.04

# Install Python and dependencies (including iproute2 for tc/netem network simulation)
USER root
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    libzmq5-dev \
    libczmq-dev \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Install OpenDSS binaries from SourceForge
WORKDIR /tmp
RUN curl -LO https://sourceforge.net/projects/electricdss/files/OpenDSSCmd/opendsscmd-1.7.7-linux-x64-installer.run/download && \
    chmod +x download && \
    ./download --mode unattended --unattendedmodeui none && \
    rm -f download

ENV PATH="/usr/local/bin:${PATH}"

# Fetch IEEE 8500-Node + IEEE 123 test feeders from Siemens PowerGym via
# sparse checkout. Fetch the exact validated commit, independently of the
# upstream default branch, and verify it before copying feeder inputs. Only
# the needed system directories are checked out (~3 MB vs ~200 MB full clone).
#
# IEEE 123 (B3 / #1140): PowerGym's 123Bus ships with 4 batteries wired
# in IEEE123Master.dss (kW=0 disconnected by default) and standalone
# Solar/Wind ramp scripts useful as references. Per-zone PV portfolio is
# authored in our Run_IEEE123_ZTCARD.dss wrapper.
ARG POWERGYM_COMMIT=c9fc6d8357308a1f2bd2389474b56089c2a6bd78
RUN test "${#POWERGYM_COMMIT}" -eq 40 && \
    case "${POWERGYM_COMMIT}" in *[!0-9a-f]*) exit 1 ;; esac && \
    git init /tmp/powergym && \
    cd /tmp/powergym && \
    git remote add origin https://github.com/siemens/powergym.git && \
    git sparse-checkout init --cone && \
    git sparse-checkout set systems/8500-Node systems/123Bus && \
    git fetch --depth 1 --filter=blob:none origin "${POWERGYM_COMMIT}" && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${POWERGYM_COMMIT}" && \
    mkdir -p /usr/app/examples/ieee8500 /usr/app/examples/ieee123 && \
    cp -r systems/8500-Node/* /usr/app/examples/ieee8500/ && \
    cp -r systems/123Bus/* /usr/app/examples/ieee123/ && \
    rm -rf /tmp/powergym

# Switch to ubuntu user for remaining operations
USER ubuntu
WORKDIR /home/ubuntu

# Install micromamba
ENV MAMBA_ROOT_PREFIX=/home/ubuntu/.conda
RUN mkdir -p bin && curl -L https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64 -o bin/micromamba \
    && chmod +x bin/micromamba \
    && bin/micromamba shell init -s bash ${MAMBA_ROOT_PREFIX} \
    && mkdir -p /home/ubuntu/.cache/conda/pkgs/cache/shards

# Copy environment file and create conda environment
COPY environment.yml /tmp/environment.yml
RUN --mount=type=cache,target=/home/ubuntu/.conda/pkgs,id=ztcard-grid-simulator-conda-pkgs,uid=1000,gid=1000 \
    --mount=type=cache,target=/home/ubuntu/.cache/conda,id=ztcard-grid-simulator-conda-shard-cache,uid=1000,gid=1000 \
    --mount=type=cache,target=/home/ubuntu/.cache/pip,id=ztcard-grid-simulator-pip-cache,uid=1000,gid=1000 \
    bin/micromamba create -f /tmp/environment.yml \
    && bin/micromamba clean --all --yes

# Copy source code and examples
COPY --chown=ubuntu:ubuntu src/ /usr/app/src
COPY --chown=ubuntu:ubuntu examples/*.dss /usr/app/examples/

# Set working directory and Python path
WORKDIR /usr/app
ENV PYTHONPATH=/usr/app

# Expose ports
# DNP3 outstation
EXPOSE 20000
# Modbus server
EXPOSE 502

# Entrypoint fixes /shared volume permissions (Docker named volumes mount as
# root by default) then drops to ubuntu via runuser before exec-ing CMD.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
USER root
ENTRYPOINT ["/entrypoint.sh"]

# GRID_MODEL must be set via .env or docker compose environment — no default.
# Engine is inferred from the value: *.dss → opendss, named string → pandapower.
# Run Grid Simulator. Engine is inferred from GRID_MODEL; no separate GRID_ENGINE var needed.
CMD ["/bin/bash", "-c", "/home/ubuntu/bin/micromamba run -n grid-simulator python -m src.main --mode scada --model ${GRID_MODEL} --no-dnp3 --enable-grid-stix"]
