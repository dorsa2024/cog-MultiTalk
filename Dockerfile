FROM r8.im/replicate/cog-python:3.10

# Install any system dependencies (if needed)
RUN apt-get update && apt-get install -y ffmpeg git

# Copy model code into the image
COPY . /src

# Set working directory
WORKDIR /src

# Install Python dependencies (defined in cog.yaml)
RUN pip install -r requirements.txt || true

# Let Cog handle the rest
ENTRYPOINT ["cog", "serve"]
