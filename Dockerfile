FROM python:3.12-slim

WORKDIR /app

COPY server.py test_server.py ./
COPY index.html portal.html style.css portal.css script.js portal.js ./
COPY README.md LICENSE .env.example ./

RUN mkdir -p uploads

ENV PORT=8000
EXPOSE 8000

# FARM_ADMIN_TOKEN and FARM_VET_TOKEN must be supplied at runtime, e.g.
# docker run -e FARM_ADMIN_TOKEN=... -e FARM_VET_TOKEN=... -p 8000:8000 cattlecare
CMD ["python", "server.py"]
