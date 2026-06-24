# Use the official Python base image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file to the container
COPY requirements.txt /app/requirements.txt

# Install the required Python packages
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the Streamlit app code to the container
COPY app.py /app/app.py

# Expose the default Streamlit port
EXPOSE 8501

# Set the command to run the Streamlit app
CMD ["streamlit", "run", "/app/app.py", "--server.port=8501", "--server.address=0.0.0.0"]

