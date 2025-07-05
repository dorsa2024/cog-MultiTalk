FROM python:3.10-slim

RUN apt-get update && apt-get install -y ffmpeg git

WORKDIR /src
COPY . /src

RUN pip install --upgrade pip
RUN pip install cog
RUN pip install -r requirements.txt

ENTRYPOINT ["cog", "serve"]
