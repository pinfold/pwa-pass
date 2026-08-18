# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies for image processing (HEIF/JPEG)
RUN apt-get update && apt-get install -y \
    libheif-dev \
    libde265-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency file and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY --chown=1000:1000 . .

# Create uploads directory with proper permissions
RUN mkdir -p /app/logs uploads && \
chown -R 1000:1000 /app

# Set the correct user to avoid permission issues
# Use the default root user for gunicorn to avoid control server issues
# USER 1000:1000
ENV HOME=/app

RUN groupadd -g 1000 appgroup && useradd -r -u 1000 -g appgroup -m appuser
USER appuser

# Expose port 5000 (internal to the container)
EXPOSE 5000

# Run the application using Gunicorn (Production Server)
# -w 4: uses 4 worker processes
# -b 0.0.0.0:5000: binds to all interfaces on port 5000
CMD ["gunicorn", "--worker-tmp-dir", "/dev/shm", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
