FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

#copy all python files from root
COPY *.py ./

ENV PORT=8080

CMD exec gunicorn --bind :$PORT main:app