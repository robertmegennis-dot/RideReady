FROM python:3.12-slim
WORKDIR /app
COPY sync.py rideready.html ./
EXPOSE 8765
# Railway sets PORT + RR_DATA_DIR; config comes from environment variables, not files
CMD ["python3", "sync.py"]
