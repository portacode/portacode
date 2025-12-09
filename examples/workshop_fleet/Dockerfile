FROM python:3.11-slim

# Git is required so GitPython can interact with repositories from inside the container
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Pre-copy requirements so Django dependencies install during the image build
COPY initial_content/requirements.txt /tmp/workshop_requirements.txt

RUN pip install --no-cache-dir portacode \
    && pip install --no-cache-dir -r /tmp/workshop_requirements.txt \
    && rm -f /tmp/workshop_requirements.txt

# Bake the instructor-provided starter code into the image
COPY initial_content/ /opt/initial_content/
