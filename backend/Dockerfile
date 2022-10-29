FROM python:3.9-slim
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /code
COPY ./src .

# This command runs the backend in the Flask development server
CMD [ "python", "./index.py" ]
# This command runs the backend using GUnicorn
# CMD [ "gunicorn", "--config", "./config/gunicorn_config.py", "index:get_app()"]