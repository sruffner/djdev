FROM python:3.8-slim
COPY requirements.txt .

RUN pip install -r requirements.txt

WORKDIR /code
COPY ./src .

CMD [ "python", "./index.py" ]