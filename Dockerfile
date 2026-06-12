FROM python:3.9
RUN apt-get update && apt-get install -y ffmpeg
WORKDIR /code
COPY . .
RUN pip install --no-cache-dir --upgrade -r requirements.txt
CMD ["python", "main.py"]
