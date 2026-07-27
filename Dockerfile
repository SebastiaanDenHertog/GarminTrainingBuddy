FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Embedded AI is optional at runtime but its dependencies are available in the
# image. CPU PyTorch is the safe default; override TORCH_INDEX_URL only when the
# Docker host has a matching accelerator setup.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" "torch>=2.6,<3" \
    && pip install --no-cache-dir -r requirements-ai.txt

COPY advisor.py planner.py targets.py feedback.py analytics.py coach.py local_ai.py dashboard.py webapp.py matching.py training_db.py benchmarks.py ./
COPY web ./web
COPY config ./config
COPY test_planner.py test_targets.py test_feedback.py test_coach.py test_local_ai.py test_analytics.py test_dashboard.py test_matching.py test_training_db.py test_benchmarks.py ./

RUN useradd --create-home --uid 10001 advisor \
    && mkdir -p /data /tokens /config /models/huggingface /models/offload \
    && chown -R advisor:advisor /app /data /tokens /config /models

USER advisor

CMD ["python", "advisor.py", "daemon"]
