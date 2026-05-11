FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libglib2.0-0 libx11-6 \
    libxcb1 libxext6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

COPY . .

RUN echo 'import os, subprocess, sys\nport = os.environ.get("PORT", "8000")\nsubprocess.run(["uvicorn", "main:app", "--host", "0.0.0.0", "--port", port])' > run.py

CMD ["python", "run.py"]