from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool  # Added for pooling
import os
import json
import time
import boto3
import logging
import logging.handlers # Added for Rotation
import sys
import io
from PIL import Image, ImageOps
import pillow_heif

# --- 1. LOGGING (Production Mute Strategy) ---
pillow_heif.register_heif_opener()

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Rotating logs: 5MB max per file, keeps 3 backups
log_handler = logging.handlers.RotatingFileHandler(
    "/app/logs/app.log", maxBytes=5*1024*1024, backupCount=3
)

logging.basicConfig(
    level=logging.WARNING, # System-wide silence
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[log_handler, logging.StreamHandler(sys.stdout)]
)

# Silence noisy third-party libs
for logger_name in ['boto3', 'botocore', 's3transfer', 'urllib3', 'werkzeug']:
    logging.getLogger(logger_name).setLevel(logging.ERROR)

# Your App Logger (set to INFO for prod, DEBUG for dev)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- 2. CONFIGURATION & APP INIT ---
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

S3_ENDPOINT_IP = os.getenv('S3_ENDPOINT_IP')
S3_ENDPOINT_URL = os.getenv('S3_ENDPOINT_URL')
S3_ACCESS_KEY = os.getenv('S3_ACCESS_KEY')
S3_SECRET_KEY = os.getenv('S3_SECRET_KEY')
S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME')
S3_REGION = os.getenv('S3_REGION')
DB_TABLE = str(os.getenv('DB_TABLE', 'incident_reports_test')).strip()

logger.info(f"[INIT] DB_TABLE raw value: {repr(DB_TABLE)} | Type: {type(DB_TABLE)}")

s3_client = boto3.client(
    's3',
    endpoint_url=S3_ENDPOINT_IP,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name=S3_REGION,
    config=boto3.session.Config(signature_version='s3v4')
)

# --- 3. DATABASE CONNECTION POOLING ---
# Create the pool once when the server starts
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20, # Min 1, Max 20 connections
        host=os.getenv('DB_HOST'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD')
    )
    logger.info("Database connection pool created successfully")
except Exception as e:
    logger.error(f"Failed to create connection pool: {e}")
    sys.exit(1)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    if conn:
        db_pool.putconn(conn)

# --- 4. IMAGE PROCESSING ---
def process_image(photo_file):
    raw_image = Image.open(photo_file)
    oriented_image = ImageOps.exif_transpose(raw_image)

    width, height = oriented_image.size
    if width > height:
        new_width, new_height = 800, int(height * (800 / width))
    else:
        new_height, new_width = 800, int(width * (800 / height))
    
    resized_image = oriented_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    if resized_image.mode != 'RGB':
        resized_image = resized_image.convert('RGB')

    output = io.BytesIO()
    resized_image.save(output, format='JPEG', quality=70, optimize=True)
    output.seek(0)
    return output

# --- 5. ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check_nearby', methods=['GET'])
def check_nearby():
    lat = request.args.get('latitude')
    lon = request.args.get('longitude')
    if not lat or not lon:
        return jsonify({"error": "Missing coordinates"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # SQL handles the UTC -> Pacific conversion including DST
        query = f"""
            SELECT 
                id, 
                image_path, 
                metadata->>'Type' as incident_type, 
                metadata->>'Description' as description, 
                (last_report AT TIME ZONE 'UTC' AT TIME ZONE 'America/Los_Angeles') as last_report
            FROM {DB_TABLE}
            WHERE ST_DWithin(location::geography, ST_MakePoint(%s, %s)::geography, 76.2) AND last_report >= NOW() - INTERVAL '3 days'
            ORDER BY last_report DESC;
        """

        logger.info(f"[INIT] DB_TABLE raw value: {repr(DB_TABLE)} | Type: {type(DB_TABLE)}")
        
        cur.execute(query, (float(lon), float(lat)))
        results = cur.fetchall()
        
        for result in results:
            if result['last_report']:
                result['last_report'] = result['last_report'].isoformat()
            if result['image_path']:
                result['image_url'] = f"{S3_ENDPOINT_URL}/{S3_BUCKET_NAME}/{result['image_path']}"
        
        return jsonify(results), 200
    except Exception as e:
        logger.error(f"Nearby check error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        release_db_connection(conn)

@app.route('/upload', methods=['POST'])
def upload_photo():
    photo_file = request.files.get('photo')
    lat, lon = request.form.get('latitude'), request.form.get('longitude')
    description = request.form.get('description', '')
    incident_type = request.form.get('incident_types', '')
    reporter = request.form.get('email', 'Unknown')
    heading = request.form.get('heading', '0')
    
    # Extract new boolean flags (handling string-to-boolean conversion)
    is_obstruction = request.form.get('is_obstruction') == 'true'
    is_hazard = request.form.get('is_hazard') == 'true'
    
    timestamp = int(time.time())
    safe_filename = f"capture_{timestamp}.jpg"
    
    # Include new fields in the metadata dictionary
    metadata = {
        "Reporter": reporter, 
        "Type": incident_type, 
        "Description": description, 
        "CameraHeading": heading,
        "IsObstruction": is_obstruction,
        "IsHazard": is_hazard
    }

    # S3 Upload
    try:
        # Check if photo_file exists before processing
        if not photo_file:
            return jsonify({"error": "No image provided"}), 400
            
        processed_image = process_image(photo_file)
        s3_client.upload_fileobj(
            processed_image, S3_BUCKET_NAME, safe_filename,
            ExtraArgs={'ContentType': 'image/jpeg'}
        )
    except Exception as e:
        logger.error(f"S3 Upload failed: {e}")
        return jsonify({"error": "Storage server unreachable"}), 500

    # DB Write
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # We store the metadata dict as a JSON string for the Postgres jsonb column
        insert_query = f"""
        INSERT INTO  {DB_TABLE} (location, image_path, first_reported, last_report, metadata, status)
        VALUES (ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s, NOW(), NOW(), %s, 'active')
        """
        logger.info(f"[INIT] DB_TABLE raw value: {repr(DB_TABLE)} | Type: {type(DB_TABLE)}")

        cur.execute(insert_query, (lon, lat, safe_filename, json.dumps(metadata)))
        conn.commit()
        return jsonify({"message": "Success"}), 200
    except Exception as e:
        logger.error(f"Database error: {e}. Cleaning up S3...")
        # Cleanup: delete the image from S3 if the DB entry fails
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=safe_filename)
        except:
            pass
        if conn: conn.rollback()
        return jsonify({"error": "Database sync failed"}), 500
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/still_there/<report_id>', methods=['POST'])
def still_there(report_id):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"UPDATE {DB_TABLE} SET last_report = NOW() WHERE id = %s", (report_id,))
        conn.commit()
        return jsonify({"message": "Updated"}), 200
    except Exception as e:
        logger.error(f"Update error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        release_db_connection(conn)

# --- 6. STATIC FILES ---
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

if __name__ == '__main__':
    # For local dev only. Production uses Gunicorn.
    app.run(host='0.0.0.0', port=5000)