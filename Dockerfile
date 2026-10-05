FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# "Hoje" é o de Brasília (vigência, semana dos to-dos); os horários gravados no banco seguem em UTC
ENV TZ=America/Sao_Paulo
COPY pyproject.toml README.md ./
COPY crosssell ./crosssell
COPY config ./config
RUN pip install --no-cache-dir .

# A plataforma não precisa de root: roda com um usuário sem privilégios
RUN useradd --system --no-create-home --uid 10001 crosssell
USER crosssell

# Web: crosssell serve. A rotina (Pipedrive, e-mails, LinkedIn, notícias, Receita, qualidade)
# roda à parte, de hora em hora: `crosssell rotina` (cron do provedor ou um segundo serviço).
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "crosssell initdb && uvicorn crosssell.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
