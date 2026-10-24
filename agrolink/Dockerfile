FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agrolink.py .
# Everything the app saves (accounts, orders, photos) goes in /data - mount a persistent disk there.
ENV AGROLINK_DATA_DIR=/data AGROLINK_WEB=1
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "agrolink.py"]
