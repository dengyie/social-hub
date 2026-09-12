FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY server ./server
COPY cli ./cli
RUN pip install --no-cache-dir .

ENV SOCIAL_HUB_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8767

CMD ["shub", "serve", "--host", "0.0.0.0"]
