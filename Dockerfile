FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache tzdata

ENV TZ=America/Sao_Paulo
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 5000

ENV WORKERS=4
ENV LOGLEVEL=info

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:5000 main:app --workers ${WORKERS} --log-level ${LOGLEVEL}"]
