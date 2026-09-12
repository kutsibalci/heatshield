# HeatShield demo image — runs the API and the /demo console in fixture mode.
# No operator credentials are needed or read: every CAMARA call is answered from
# fixtures/profiles.json, so the public demo can never spend a Nokia query or leak a key.
#
#   docker build -t heatshield .
#   docker run -p 8000:8000 -e PORT=8000 heatshield     → http://127.0.0.1:8000/demo
#
# Render / Fly / Railway inject PORT; Hugging Face Spaces expects 7860 (the default below).
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NAC_MODE=fixture \
    HS_SCHEDULER=0 \
    PORT=7860

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','7860'))" || exit 1

CMD ["sh", "-c", "python -m uvicorn apps.api.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
