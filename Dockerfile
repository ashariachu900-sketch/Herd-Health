FROM python:3.12-slim

WORKDIR /app

COPY server.py test_server.py backup.py restore.py ./
COPY index.html portal.html style.css portal.css script.js portal.js test_frontend.html ./
COPY vendor/qrcode.min.js vendor/LICENSE.qrcodejs.txt vendor/
COPY README.md PROJECT_README.md LICENSE .env.example ./

# Run as an unprivileged user; named volumes are chowned so SQLite and uploads work.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p uploads data logs \
    && chown -R app:app /app
USER app

ENV PORT=8000
EXPOSE 8000

# FARM_ADMIN_TOKEN and FARM_VET_TOKEN must be supplied at runtime, e.g.
# docker run -e FARM_ADMIN_TOKEN=... -e FARM_VET_TOKEN=... -p 8000:8000 cattlecare
CMD ["python", "server.py"]
