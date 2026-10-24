# syntax=docker/dockerfile:1
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 tuman && useradd --uid 10001 --gid tuman --no-create-home tuman
COPY requirements.lock requirements.lock
# Optional trusted CA for enterprise/cloud HTTPS proxies. Verification stays on;
# the CA is mounted for this build step and is not included in the final image.
RUN --mount=type=secret,id=custom_ca,target=/tmp/build-ca.pem \
    if [ -f /tmp/build-ca.pem ]; then \
      PIP_CERT=/tmp/build-ca.pem pip install --no-cache-dir -r requirements.lock; \
    else pip install --no-cache-dir -r requirements.lock; fi
COPY --chown=tuman:tuman . .
RUN mkdir -p /app/var && chown -R tuman:tuman /app/var
USER tuman
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)"
CMD ["python", "main.py"]
