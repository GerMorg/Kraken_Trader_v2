FROM python:3.12-alpine

RUN apk add --no-cache su-exec

ARG BUILD_VERSION=0.1.17
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/kraken-trader APP_VERSION=${BUILD_VERSION}
WORKDIR /opt/kraken-trader
COPY app/ /opt/kraken-trader/app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh \
  && find /opt/kraken-trader/app -type d -exec chmod 0755 {} \\; \
  && find /opt/kraken-trader/app -type f -exec chmod 0644 {} \\; \
  && chmod 0755 /opt/kraken-trader \
  && mkdir -p /data/logs

# Fail the image build rather than shipping a container whose Python entrypoint
# cannot import the application package. This catches incorrect copy/context paths.
RUN test -f /opt/kraken-trader/app/main.py \
  && test -f /opt/kraken-trader/app/__init__.py \
  && python -c "import app.main; print('Kraken Trader application import: OK')" \
  && python -c "import sys; sys.path.insert(0, '/opt/kraken-trader'); import app.main; print('Kraken Trader explicit-path bootstrap import: OK')" \
  && /run.sh --healthcheck \
  && su-exec 10001:10001 /run.sh --healthcheck

ENTRYPOINT ["/run.sh"]