FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_DIR=/opt/models

# Tesseract OCR with Ukrainian language data (for PDFs without a text layer).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ukr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=true
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

# Download the embedding model at build time so startup does not depend on it.
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDING_MODEL}', cache_dir='/opt/models')"

COPY forest_bot ./forest_bot
COPY tests ./tests
COPY pyproject.toml ./

VOLUME ["/data"]
CMD ["python", "-m", "forest_bot"]
