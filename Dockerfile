FROM r8.im/replicate/cog-python:3.10

RUN apt-get update && apt-get install -y ffmpeg git

COPY . /src
WORKDIR /src

RUN pip install --upgrade pip && pip install -r requirements.txt

ENTRYPOINT ["cog", "serve"]
