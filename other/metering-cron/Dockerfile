FROM python:3.12-slim

WORKDIR /app

# Copy source
COPY main.py requirements.txt ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]
