FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g opencode-ai@1.18.25 \
    && pip install --no-cache-dir 'Pillow>=10,<13' 'playwright>=1.48,<2' \
    && python -m playwright install --with-deps chromium
WORKDIR /runner
COPY refcraft/__init__.py /runner/refcraft/__init__.py
COPY refcraft/inference /runner/refcraft/inference
COPY scripts/generate.py /runner/scripts/generate.py
ENTRYPOINT ["python", "/runner/scripts/generate.py"]
