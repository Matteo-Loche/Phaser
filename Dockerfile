ARG PYTHON_VERSION=3.13-slim
ARG IPHREEQC_VERSION=3.8.6-17100

FROM python:${PYTHON_VERSION} AS iphreeqc-builder
ARG IPHREEQC_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN wget "https://water.usgs.gov/water-resources/software/PHREEQC/iphreeqc-${IPHREEQC_VERSION}.tar.gz" \
    && tar -xzf "iphreeqc-${IPHREEQC_VERSION}.tar.gz"

WORKDIR /tmp/iphreeqc-${IPHREEQC_VERSION}/build
RUN ../configure \
    && make -j"$(nproc)" \
    && make install


FROM python:${PYTHON_VERSION}
ARG IPHREEQC_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PHASER_HOST=0.0.0.0 \
    PHASER_PORT=8765 \
    PHASER_IPHREEQC_LIB=/usr/local/lib/libiphreeqc.so \
    PHASER_BUILTIN_DB_DIRS=/opt/phreeqc/database \
    PHASER_GENERATED_DB_DIR=/app/PHASER/data/databases/generated

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libstdc++6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=iphreeqc-builder /usr/local/lib/libiphreeqc.so* /usr/local/lib/
COPY --from=iphreeqc-builder /tmp/iphreeqc-${IPHREEQC_VERSION}/database /opt/phreeqc/database
RUN ldconfig

WORKDIR /app/PHASER
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/PHASER/data/databases/generated

EXPOSE 8765

CMD ["python", "run_server.py"]
