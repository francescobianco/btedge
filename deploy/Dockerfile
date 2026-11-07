# btedge control node: no Bluetooth, mesh hub over IP and admin panel.
# Build context is the repo root (see deploy/btedge.yafb.net.yml).
FROM python:3.13-alpine
RUN apk add --no-cache openssh-keygen
WORKDIR /opt/btedge
COPY btedge ./btedge
ENV PYTHONUNBUFFERED=1 \
    BTEDGE_ROLE=control \
    BTEDGE_STATE_DIR=/var/lib/btedge \
    BTEDGE_LAN=false \
    BTEDGE_PORT=4646
VOLUME /var/lib/btedge
EXPOSE 4646
CMD ["python", "-m", "btedge"]
