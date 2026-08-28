# Use a lightweight Python base image.
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy and install dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code — full tree, not top-level glob. Directories
# (engine/, filters/) must survive the COPY or the package import
# fails at container-start, not at build-time, which is what made
# this defect invisible until `docker logs` was actually read.
COPY . .

# Opening a port for FastAPI
EXPOSE 8000

# main:app -- main.py is the actual ASGI entrypoint (FastAPI() instance
# lives there per Directive I.1's Dumb Router pattern). engine/ is an
# import target for main.py, never an app target for Uvicorn itself.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--timeout-keep-alive", "30"]