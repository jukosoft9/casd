FROM python:3.13-slim

RUN useradd --system --uid 10001 --home-dir /nonexistent casd \
    && mkdir /data && chown casd /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER casd
VOLUME /data
EXPOSE 8080
# Credentials come from CASD_READ_CREDENTIALS / CASD_WRITE_CREDENTIALS ("user:password").
ENTRYPOINT ["casd", "serve", "--host", "0.0.0.0", "--dir", "/data"]
CMD ["--max-size", "10G"]
