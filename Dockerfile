FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY crosssell ./crosssell
COPY config ./config
RUN pip install --no-cache-dir .

# Web: crosssell serve. A rotina (Pipedrive, e-mails, LinkedIn, notícias, Receita, qualidade)
# roda à parte, de hora em hora: `crosssell rotina` (cron do provedor ou um segundo serviço).
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "crosssell initdb && uvicorn crosssell.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
