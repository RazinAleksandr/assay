# Assay scanner + server image (like sonarsource/sonar-scanner-cli): browsers, Node 22 for the app under test, assay.
#   docker build -t assay:0.2 .
#   docker run --rm -v "$PWD:/src" -w /src -e ASSAY_HOST_URL -e ASSAY_TOKEN assay:0.2 ci
#   docker run -p 9000:9000 -v assay-data:/data assay:0.2 server --host 0.0.0.0 --data /data
FROM mcr.microsoft.com/playwright/python:v1.65.0-noble
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY . /opt/assay
RUN pip install --no-cache-dir /opt/assay
ENTRYPOINT ["assay"]
CMD ["--help"]
