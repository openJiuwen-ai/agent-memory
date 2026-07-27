# syntax=docker/dockerfile:1.6
# 平台后端：Spring Boot 3.3.5 / JDK 17
# 多阶段构建：build 阶段跑 mvn package；runtime 阶段只装 JRE + jar

# ---------- Stage 1: build ----------
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build

# 先单独 COPY pom.xml 预下载依赖（利用层缓存：源码变更不会重新下载依赖）
COPY pom.xml .
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q dependency:go-offline

# 再 COPY 源码与资源，打 fat-jar（跳过测试，容器内不依赖外部 DB / 记忆服务）
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 \
    mvn -B -q clean package -DskipTests \
    && cp target/platform-*.jar /build/app.jar

# ---------- Stage 2: runtime ----------
FROM eclipse-temurin:17-jre AS runtime
WORKDIR /app

# SQLite 文件库会落到 /app/data/platform.db；挂载此目录持久化
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

COPY --from=build /build/app.jar /app/app.jar

# 容器内 UTF-8（pom 里强制 source/target=17，运行期 locale 也补一下，避免 SQLite/日志中文乱码）
# profile / 记忆服务连接（base-url、api-key）由 docker-compose environment + .env 注入，
# 见 agent-memory-platform/.env.example；本镜像不再硬编码，避免构建期写入过期地址与密钥
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    JAVA_OPTS="-Dfile.encoding=UTF-8"

EXPOSE 9000

ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/app.jar"]
