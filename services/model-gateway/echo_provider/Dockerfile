# Modelo eco — só stdlib, imagem mínima, sem pip install (determinístico e
# rápido de buildar/subir; ver server.py para o porquê disso importar para o
# "upgrade simulado" do LiteLLM).
FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
ENV ECHO_PORT=9000
EXPOSE 9000
CMD ["python", "/app/server.py"]
