FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installer Chromium pour Playwright
RUN playwright install chromium

# Copier le code de l'application
COPY . .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
