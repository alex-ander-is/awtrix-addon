ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.12-alpine3.20

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY run.sh /run.sh

RUN pip install --no-cache-dir . \
    && chmod a+x /run.sh

EXPOSE 8099
CMD ["/run.sh"]
