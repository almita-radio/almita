# ALMITA DEMO - SIMULATED instrument + the real ALMITA web, isolated in its own network namespace.
# Built from a snapshot made by demo/docker/build.sh (never the operator's data/).
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 ALMITA_DEMO=1 TZ=America/Santiago \
    DEMO_CLOCK="@2026-07-15 22:00:00"
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv indi-bin tzdata ca-certificates faketime iproute2 procps \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-demo.txt /tmp/
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements-demo.txt
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
COPY systemctl-shim.py /usr/local/bin/systemctl
RUN chmod +x /entrypoint.sh /usr/local/bin/systemctl && mkdir -p /root/.config/almita \
    && echo '{"host": "127.0.0.1"}' > /root/.config/almita/instrument.json
EXPOSE 8088
ENTRYPOINT ["/entrypoint.sh"]
