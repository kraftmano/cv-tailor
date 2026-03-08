FROM python:3.11-slim

# Install LibreOffice for DOCX→PDF conversion on Linux
RUN apt-get update && \
    apt-get install -y --no-install-recommends libreoffice-writer && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn public_app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
