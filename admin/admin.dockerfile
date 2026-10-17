# admin.dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /admin

# تثبيت الحزم
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "jinja2>=3.1" \
    "python-multipart>=0.0.9" \
    "pydantic>=2.6"

# نسخ المشروع
COPY . /admin

# مستخدم غير جذري
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# ADMIN_SECRET يُمرَّر عبر -e أو docker-compose
CMD ["uvicorn", "admin.main:app", "--host", "0.0.0.0", "--port", "8000"]
