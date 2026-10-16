FROM python:3.12-alpine

ARG BUILD_VERSION=0.1.15
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/kraken-trader APP_VERSION=${BUILD_VERSION}
WORKDIR /opt/kraken-trader
COPY app/ /opt/kraken-trader/app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh && mkdir -p /data/logs

# Fail the image build rather than shipping a container whose Python entrypoint
# cannot import the application package. This catches incorrect copy/context paths.
RUN test -f /opt/kraken-trader/app/main.py \
  && test -f /opt/kraken-trader/app/__init__.py \
  && python -c "import app.main; print('Kraken Trader application import: OK')" \
  && python -c "import sys; sys.path.insert(0, '/opt/kraken-trader'); import app.main; print('Kraken Trader explicit-path bootstrap import: OK')"

ENTRYPOINT ["/run.sh"]