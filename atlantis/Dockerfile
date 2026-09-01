FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg is the inactivity checker: one pass per compiled Lookout video, so
# neither check_timelapse_activity nor the check a new journal kicks off can
# run at all without it on PATH. --no-install-recommends keeps that to the
# codecs and leaves out the desktop stack apt would otherwise drag in.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD sh -c "python manage.py migrate && python manage.py collectstatic --noinput && gunicorn atlantis.wsgi:application --bind 0.0.0.0:8000"