# Small image for the vision-sidecar. It only needs paho-mqtt + protobuf —
# it talks to OpenMoxie's mosquitto broker over MQTT; it does not import Django.
FROM python:3.10-slim
WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt protobuf
COPY . /app
# Broker host/port come from env (the compose overlay points them at the `mqtt`
# service). Flags still work too: `docker run ... python vision_sidecar.py --help`.
CMD ["python", "vision_sidecar.py"]
