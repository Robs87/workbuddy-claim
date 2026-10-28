# syntax=docker/dockerfile:1
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="workbuddy-claim" \
      org.opencontainers.image.description="WorkBuddy 每日签到（Buddy 加油站）无人值守容器" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data

RUN apk add --no-cache tzdata \
 && addgroup -g 1000 -S wb \
 && adduser -u 1000 -S -G wb -H -s /sbin/nologin wb \
 && mkdir -p /data \
 && chown -R 1000:1000 /data

WORKDIR /app
COPY app/claim.py /app/claim.py
RUN chmod 0555 /app/claim.py

USER 1000:1000
VOLUME ["/data"]

# 36 小时未成功领取即判定不健康：足够容忍一次失败重试，又能让静默失效浮出水面
HEALTHCHECK --interval=30m --timeout=20s --start-period=5m --retries=3 \
  CMD ["python3", "/app/claim.py", "--health"]

ENTRYPOINT ["python3", "/app/claim.py"]
