# dv2mv web tier — review the pipeline in a browser with zero host setup.
#
#   docker build -t dv2mv-web .
#   docker run --rm -p 8000:8000 -v /path/to/media:/media dv2mv-web
#   # open http://localhost:8000   (mounted folder = your media library)
#
# Web tier only: the Tk desktop app and the DaVinci Resolve hand-off need a
# display / a Resolve install, so those aren't containerized — use the conda
# env (environment.yml) for the full desktop workflow.
FROM python:3.11-slim

# system binaries: ffmpeg (required) + rubberband (optional Retempo R3);
# libgl1/libglib2.0-0 are the runtime libs OpenCV (cv2) needs on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg rubberband-cli libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# the media library is mounted here at run time (outside the image)
ENV DV2MV_MEDIA=/media
EXPOSE 8000
CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
