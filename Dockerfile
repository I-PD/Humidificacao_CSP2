FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home collector \
    && mkdir /data && chown collector:collector /data
COPY csp2_hum_machine.py schema.sql ./
USER collector
ENTRYPOINT ["python", "csp2_hum_machine.py"]
CMD ["--write-db"]
