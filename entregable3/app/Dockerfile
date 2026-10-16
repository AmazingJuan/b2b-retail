FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY e3app ./e3app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "e3app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning", "--no-access-log"]
