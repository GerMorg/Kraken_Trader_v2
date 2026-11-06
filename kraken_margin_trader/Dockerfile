FROM python:3.12-alpine

ARG BUILD_VERSION=0.1.61
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

COPY requirements.txt /requirements.txt
RUN apk add --no-cache su-exec \
  && pip install --no-cache-dir -r /requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/kraken-trader APP_VERSION=${BUILD_VERSION}
WORKDIR /opt/kraken-trader
COPY app/ /opt/kraken-trader/app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh \
  && chmod 0755 /opt/kraken-trader \
  && chmod -R a+rX /opt/kraken-trader/app \
  && mkdir -p /data/state /data/logs

# Fail the image build rather than shipping a container whose Python entrypoint
# cannot import the application package. This catches incorrect copy/context paths.
RUN test -f /opt/kraken-trader/app/main.py \
  && test -f /opt/kraken-trader/app/__init__.py \
  && python -c "import app.main; print('Kraken Trader application import: OK')" \
  && python -c "import sys; print('Python:', sys.version)"

ENTRYPOINT ["/run.sh"]
