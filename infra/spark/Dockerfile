# Spark Thrift server + Iceberg (Silver MERGE와 dbt Gold)
# Docker Hub의 apache/spark 이미지에 Maven Central의 jar 세 개를 넣고 sha256을 대조합니다.
# 버전을 고른 이유는 docs/decisions/0002에 있습니다.
FROM apache/spark:3.5.6-java17

USER root
RUN set -eu; \
    m=https://repo1.maven.org/maven2; \
    fetch() { \
      curl -fsSL -o "/opt/spark/jars/$(basename "$1")" "$m/$1"; \
      echo "$2  /opt/spark/jars/$(basename "$1")" | sha256sum -c -; \
    }; \
    fetch org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.10.0/iceberg-spark-runtime-3.5_2.12-1.10.0.jar \
      70f9471870b2d456167d703c829f098a446b86bb1379eee338c0efa03a341026; \
    fetch org/apache/iceberg/iceberg-aws-bundle/1.10.0/iceberg-aws-bundle-1.10.0.jar \
      172370cfc32a5f1ae5399b210e86fc04a366dc29a112d97ac8ffc74df7525596; \
    fetch org/postgresql/postgresql/42.7.7/postgresql-42.7.7.jar \
      157963d60ae66d607e09466e8c0cdf8087e9cb20d0159899ffca96bca2528460; \
    usermod -d /opt/spark/work-dir spark
# beeline이 홈 폴더에 설정 폴더를 만드는데, 이미지의 spark 사용자에게는 홈 폴더가 없어 경고가 납니다.
# 쓸 수 있는 작업 폴더를 홈으로 둡니다(위 usermod).
COPY start-thrift.sh /opt/cdclake/start-thrift.sh
USER spark

EXPOSE 10000 4040
ENTRYPOINT ["bash", "/opt/cdclake/start-thrift.sh"]
