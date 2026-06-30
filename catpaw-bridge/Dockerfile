FROM python:3.11-slim

RUN pip install --no-cache-dir aiohttp pycryptodome pyyaml

COPY proxy/ /app/proxy/
COPY catpaw_reverse_proxy.py /app/
COPY bridge.conf.yaml /app/

WORKDIR /app
EXPOSE 9000

CMD ["python3", "catpaw_reverse_proxy.py"]
