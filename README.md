# Deployment Guide

## 1. Running the Docker Container

1. Ensure you have Docker and Docker Compose installed on your server.

2. Navigate to your project directory.

3. Run the following command to build and start the app:

   ```bash
   docker-compose up -d --build
Your app is now running at `http://localhost:8080`.```

## 2. NGINX Reverse Proxy Configuration
Add the following block to your NGINX site configuration (usually in `/etc/nginx/sites-available/default` or a specific file for your domain).

Crucial: You must forward the `X-Forwarded-Proto` header. Without this, Flask won't know the connection is secure (HTTPS), and Service Workers often fail to register on "insecure" origins.

```Nginx

server {
    listen 80;
    server_name pass.opencommons.org;

    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name pass.opencommons.org;

    # SSL Certificate paths (example using Let's Encrypt)
    ssl_certificate /etc/letsencrypt/live/pass.opencommons.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pass.opencommons.org/privkey.pem;

    location / {
        proxy_pass http://localhost:8080; # Points to the port defined in docker-compose.yml
        
        # Standard Headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        
        # CRITICAL for PWAs: Tells Flask we are using HTTPS
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}```
3. Updating Application Code (Optional but Recommended)
If your Python app generates links using url_for, you may need to tell Flask to trust the headers sent by NGINX. You can do this by wrapping your app with ProxyFix in app.py:

Python

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# x_proto=1 tells Flask to trust the X-Forwarded-Proto header
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
You should be able to copy the entire block above directly into your README.md file without any errors. I have also fixed the code fence identifiers (e.g., bash, nginx, python) to ensure perfect GitHub rendering.
