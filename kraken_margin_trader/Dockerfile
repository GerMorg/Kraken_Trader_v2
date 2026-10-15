FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/kraken-trader
WORKDIR /opt/kraken-trader
COPY app/ /opt/kraken-trader/app/
COPY run.sh /run.sh
RUN chmod 0755 /run.sh && mkdir -p /data/logs /data/reports/tax/AT
CMD ["/run.sh"]