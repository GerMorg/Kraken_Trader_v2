FROM python:3.12-alpine

ARG BUILD_VERSION=0.1.12
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/kraken-trader
WORKDIR /opt/kraken-trader
COPY app/ /opt/kraken-trader/app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh && mkdir -p /data/logs
CMD ["/run.sh"]