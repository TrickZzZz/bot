FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot script and config
COPY bot.py .
COPY cooldown_roles.json .

# Run the bot
CMD ["python", "bot.py"]