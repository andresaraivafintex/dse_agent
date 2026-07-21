# Plano 06 — console-projector: SoR -> console_rm (read model do painel).
FROM python:3.12-slim

WORKDIR /app
COPY packages/contracts /app/packages/contracts
COPY services/console-projector /app/services/console-projector

RUN pip install --no-cache-dir -e /app/packages/contracts -e /app/services/console-projector

CMD ["python", "-m", "console_projector.projector"]
