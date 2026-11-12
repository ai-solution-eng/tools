# Python 3.14 — the fleet base (MultimodalRAG, Speech2Speech and the mcp_servers
# all run python:3.14-slim). The app code is 3.14-clean and the test suite runs
# on 3.14 locally; see README "Python version" for the dependency evidence.
FROM python:3.14-slim
WORKDIR /app
COPY docker/requirements.txt requirements.txt
# A newer pip backtracks far more efficiently than the one baked into the base
# image. With an open boto3 range the bundled resolver looped over
# boto3/botocore pairs without settling; upgrading first is cheap insurance.
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
COPY src/model_manager/app/ ./app/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
