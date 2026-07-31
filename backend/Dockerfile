# Use the official, lightweight Python 3.12 image
FROM python:3.12-slim

# Prevent Python from writing .pyc files to disk and from buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /code

# Copy only the requirements file first to leverage Docker's caching mechanism
COPY requirements.txt /code/

# Install the Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY ./app /code/app
COPY generate_secrets.py seed_account.py /code/

# Expose the port Uvicorn will run on
EXPOSE 8000

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
