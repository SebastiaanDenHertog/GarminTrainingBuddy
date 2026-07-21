FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY advisor.py planner.py feedback.py analytics.py coach.py ./
COPY test_planner.py test_feedback.py test_coach.py ./

RUN useradd --create-home --uid 10001 advisor \
    && mkdir -p /data /tokens /config \
    && chown -R advisor:advisor /app /data /tokens /config

USER advisor

CMD ["python", "advisor.py", "daemon"]
