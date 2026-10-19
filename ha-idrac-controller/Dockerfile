# HA iDRAC Controller/Dockerfile
# Use the current Home Assistant Python on Debian Trixie
FROM python:3.14-slim-trixie

ENV PYTHONUNBUFFERED=1

# Set the default shell for RUN commands to bash for consistency
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies: ipmitool and bash (if not already primary), and jq
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ipmitool \
    bash \
    jq \
 && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements.txt first to leverage Docker's build cache
COPY app/requirements.txt ./
# Ensure pip is up-to-date, then install requirements
RUN python3 -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code from the 'app' subdirectory
COPY app/ /app/

# Copy your run.sh script, make it executable, and place it where it's easy to call
COPY run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh

# Set the command to run when the container starts
CMD ["/usr/local/bin/run.sh"]