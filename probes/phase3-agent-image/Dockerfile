# AgentCore Runtime is ARM64 only, so this must be built for linux/arm64.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8080
CMD ["python", "/app/server.py"]
