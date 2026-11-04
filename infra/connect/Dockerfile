# Kafka Connect + Debezium PostgreSQL 커넥터
# Docker Hub의 apache/kafka 이미지에 Maven Central의 Debezium 플러그인을 넣습니다.
# 왜 quay.io의 debezium/connect 이미지를 쓰지 않는지는 docs/decisions/0001-local-runtime.md에 있습니다.
FROM apache/kafka:4.3.2

ARG DEBEZIUM_VERSION=3.6.3.Final

USER root
RUN set -eu; \
    url="https://repo1.maven.org/maven2/io/debezium/debezium-connector-postgres/${DEBEZIUM_VERSION}/debezium-connector-postgres-${DEBEZIUM_VERSION}-plugin.tar.gz"; \
    wget -qO /tmp/plugin.tar.gz "$url"; \
    echo "$(wget -qO- "$url.sha1")  /tmp/plugin.tar.gz" | sha1sum -c -; \
    mkdir -p /opt/connect-plugins; \
    tar -xzf /tmp/plugin.tar.gz -C /opt/connect-plugins; \
    rm /tmp/plugin.tar.gz
COPY connect-distributed.properties /opt/kafka/config/connect-cdclake.properties
USER appuser

EXPOSE 8083
CMD ["/opt/kafka/bin/connect-distributed.sh", "/opt/kafka/config/connect-cdclake.properties"]
