FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot script
COPY bot.py .

# Create volume mount point for persistent database
VOLUME ["/app/data"]

# Run the bot
CMD ["python", "bot.py"]