FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY rowforge ./rowforge
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir -e . --no-deps \
    && useradd --create-home --uid 10001 rowforge && mkdir /app/data && chown rowforge:rowforge /app/data
USER rowforge
EXPOSE 8082
CMD ["python", "-m", "rowforge", "serve", "--host", "0.0.0.0"]
