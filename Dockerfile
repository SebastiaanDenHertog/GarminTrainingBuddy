FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The embedded model is included in the normal V3 image. This installs the
# CPU build of PyTorch by default. Change TORCH_INDEX_URL at build time only
# when the server has a matching, Docker-accessible accelerator setup.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" "torch>=2.6,<3" \
    && pip install --no-cache-dir -r requirements-ai.txt

COPY advisor.py planner.py feedback.py analytics.py coach.py local_ai.py ./
COPY test_planner.py test_feedback.py test_coach.py test_local_ai.py test_analytics.py ./

RUN useradd --create-home --uid 10001 advisor \
    && mkdir -p /data /tokens /config /models/huggingface /models/offload \
    && chown -R advisor:advisor /app /data /tokens /config /models

USER advisor

CMD ["python", "advisor.py", "daemon"]
