# Use the official, lightweight Python 3.12 image
FROM python:3.12-slim

# Prevent Python from writing .pyc files to disk and from buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

# Install Cloudflare WARP client. This ONLY installs the binary at build time —
# whether the daemon actually connects successfully at runtime is handled
# defensively in entrypoint.sh, and never blocks the app from starting.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${VERSION_CODENAME} main" \
        > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker's caching mechanism
COPY requirements.txt /code/

# Install the Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY ./app /code/app
COPY generate_secrets.py seed_account.py /code/

# Startup script — launches the WARP daemon (best-effort) then the web server
COPY entrypoint.sh /code/entrypoint.sh
RUN chmod +x /code/entrypoint.sh

# Expose the port Uvicorn will run on
EXPOSE 8000

# Command to run the application
ENTRYPOINT ["/code/entrypoint.sh"]
