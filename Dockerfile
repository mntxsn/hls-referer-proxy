# ffmpeg is only needed for the /stream.mp3 and /stream.aac endpoints. If you
# only ever use /stream.m3u8, you can drop the apt-get line and the image
# shrinks from roughly 250 MB to 45 MB.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hls_proxy.py .

# Nothing here needs to write anything, so it does not run as root.
USER nobody

EXPOSE 8765

# 0.0.0.0 binds inside the container only; what the NAS exposes is decided by
# the port mapping in docker-compose.yml.
ENTRYPOINT ["python", "hls_proxy.py", "--host", "0.0.0.0", "--port", "8765"]
