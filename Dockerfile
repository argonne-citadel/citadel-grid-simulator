FROM ubuntu:24.04

# Install Python and dependencies
USER root
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    libzmq5-dev \
    libczmq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install OpenDSS binaries from SourceForge
WORKDIR /tmp
RUN curl -LO https://sourceforge.net/projects/electricdss/files/OpenDSSCmd/opendsscmd-1.7.7-linux-x64-installer.run/download && \
    chmod +x download && \
    ./download --mode unattended --unattendedmodeui none && \
    rm -f download

ENV PATH="/usr/local/bin:${PATH}"

# Switch to ubuntu user for remaining operations
USER ubuntu
WORKDIR /home/ubuntu

# Install micromamba
ENV MAMBA_ROOT_PREFIX=/home/ubuntu/.conda
RUN curl -L https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj bin/micromamba \
    && bin/micromamba shell init -s bash ${MAMBA_ROOT_PREFIX}

# Copy environment file and create conda environment
COPY environment.yml /tmp/environment.yml
RUN --mount=type=cache,target=/home/ubuntu/.conda/pkgs,id=ztcard-grid-simulator-conda-pkgs \
    --mount=type=cache,target=/home/ubuntu/.cache/pip,id=ztcard-grid-simulator-pip-cache \
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

# Default environment variables for grid simulator configuration
ENV GRID_ENGINE=pandapower
ENV GRID_MODEL=dickert-lv

# Run Grid Simulator directly from source with Grid-STIX export enabled
CMD ["/bin/bash", "-c", "/home/ubuntu/bin/micromamba run -n grid-simulator python -m src.main --mode scada --engine ${GRID_ENGINE} --model ${GRID_MODEL} --no-dnp3 --enable-grid-stix"]