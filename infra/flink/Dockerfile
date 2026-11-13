# Flink + Iceberg (Bronze 적재)
# Docker Hub의 flink 이미지에 Maven Central의 jar 다섯 개를 넣습니다.
# jar마다 sha256을 고정해 두고 받은 뒤 대조합니다. 버전을 고른 이유는 docs/decisions/0002에 있습니다.
FROM flink:1.20.4-scala_2.12-java17

USER root
RUN set -eu; \
    m=https://repo1.maven.org/maven2; \
    fetch() { \
      wget -qO "/opt/flink/lib/$(basename "$1")" "$m/$1"; \
      echo "$2  /opt/flink/lib/$(basename "$1")" | sha256sum -c -; \
    }; \
    fetch org/apache/flink/flink-sql-connector-kafka/3.4.0-1.20/flink-sql-connector-kafka-3.4.0-1.20.jar \
      b860724fd97d7da0901bfc3859680442f4c1aa2b35ab944407558b393a1cf93e; \
    fetch org/apache/iceberg/iceberg-flink-runtime-1.20/1.10.0/iceberg-flink-runtime-1.20-1.10.0.jar \
      c950ad6cedde3fe30794e551ffc41f4a72d0733348b5d865c8a9cab0968021ce; \
    fetch org/apache/iceberg/iceberg-aws-bundle/1.10.0/iceberg-aws-bundle-1.10.0.jar \
      172370cfc32a5f1ae5399b210e86fc04a366dc29a112d97ac8ffc74df7525596; \
    fetch org/postgresql/postgresql/42.7.7/postgresql-42.7.7.jar \
      157963d60ae66d607e09466e8c0cdf8087e9cb20d0159899ffca96bca2528460; \
    fetch org/apache/flink/flink-shaded-hadoop-2-uber/2.8.3-10.0/flink-shaded-hadoop-2-uber-2.8.3-10.0.jar \
      492b2a559f2a1dad3808b51d9a26a575dbb1202004c9f85f5059c520e0632127; \
    mkdir -p /opt/flink/state/checkpoints /opt/flink/state/savepoints; \
    chown -R flink:flink /opt/flink/state
USER flink
