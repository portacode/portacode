FROM python:3.11-slim

# Install git so GitPython can find the git executable
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir portacode===1.3.39
