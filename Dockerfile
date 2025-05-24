FROM python:3.11

# Set the working directory to /app
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy your source code into /app/app_src
COPY ./app_src /app/app_src

# Make the script executable
RUN chmod +x /app/app_src/wait-for-it.sh

# Set PYTHONPATH so imports inside app_src work
ENV PYTHONPATH="${PYTHONPATH}:/app/app_src"

# Start the app from app_src
CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--app-dir", "app_src" ]
