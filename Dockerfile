FROM python:3.12-slim

# Install system deps for nodriver (chromium headless) — optional at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY dosty.py .

# nodriver is optional; install it only if requirements.txt exists
COPY requirements*.txt ./
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi

ENV PORT=8787
EXPOSE 8787

# Run the HTTP server
CMD ["python", "dosty.py", "--serve", "--host", "0.0.0.0"]
