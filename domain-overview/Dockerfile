FROM python:3.12-slim
WORKDIR /srv
# git: the journal repo reader (#30) reads each site repo's history
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/data/overview.db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
