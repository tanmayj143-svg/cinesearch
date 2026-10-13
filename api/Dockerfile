FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt
COPY . .
ENV HF_HOME=/hf_cache
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
