# Echo model — stdlib only, minimal image, no pip install (deterministic and
# fast to build/boot; see server.py for why that matters to LiteLLM's
# "simulated upgrade").
FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
ENV ECHO_PORT=9000
EXPOSE 9000
CMD ["python", "/app/server.py"]
