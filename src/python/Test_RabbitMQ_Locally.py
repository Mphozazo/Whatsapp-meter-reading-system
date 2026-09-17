import pika
import json
import uuid
from datetime import datetime
import os
from dotenv import load_dotenv
import easyocr
import cv2
import re

load_dotenv()

# Configuration
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', '5672'))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'meter_user')
RABBITMQ_PASSWORD = os.getenv('RABBITMQ_PASSWORD', 'meter_pass')
RABBITMQ_VHOST = os.getenv('RABBITMQ_VHOST', '/meter_readings')
RABBITMQ_EXCHANGE = os.getenv('RABBITMQ_EXCHANGE', 'meter_readings_exchange')
RABBITMQ_QUEUE = os.getenv('RABBITMQ_QUEUE', 'meter_readings_queue')
RABBITMQ_ROUTING_KEY = os.getenv('RABBITMQ_ROUTING_KEY', 'meter.reading.submitted')

reader = easyocr.Reader(['en'], gpu=False)

def get_connection():
    """Create RabbitMQ connection"""
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300
    )
    return pika.BlockingConnection(parameters)

def setup_rabbitmq():
    """Setup exchanges and queues"""
    connection = get_connection()
    channel = connection.channel()
    
    # Declare exchange
    channel.exchange_declare(
        exchange=RABBITMQ_EXCHANGE,
        exchange_type='topic',
        durable=True
    )
    print(f"✓ Exchange declared: {RABBITMQ_EXCHANGE}")
    
    # Declare queue
    channel.queue_declare(
        queue=RABBITMQ_QUEUE,
        durable=True
    )
    print(f"✓ Queue declared: {RABBITMQ_QUEUE}")
    
    # Bind queue to exchange
    channel.queue_bind(
        exchange=RABBITMQ_EXCHANGE,
        queue=RABBITMQ_QUEUE,
        routing_key='meter.reading.*'
    )
    print(f"✓ Queue bound with routing key: meter.reading.*")
    
    connection.close()

def publish_meter_reading(sender, meter_reading, confidence):
    """Publish a meter reading message"""
    try:
        connection = get_connection()
        channel = connection.channel()
        
        # Create message
        message_id = str(uuid.uuid4())
        message = {
            'event_type': 'meter_reading_submitted',
            'timestamp': datetime.utcnow().isoformat(),
            'message_id': message_id,
            'data': {
                'Id': message_id,
                'sender': sender,
                'meterReading': meter_reading,
                'ocrConfidence': confidence,
                'meterType': 'electricity',
                'timestamp': datetime.utcnow().isoformat()
            }
        }
        
        # Publish message
        channel.basic_publish(
            exchange=RABBITMQ_EXCHANGE,
            routing_key=RABBITMQ_ROUTING_KEY,
            body=json.dumps(message, indent=2),
            properties=pika.BasicProperties(
                delivery_mode=2,  # Persistent
                content_type='application/json',
                message_id=message_id,
                timestamp=int(datetime.utcnow().timestamp()),
                headers={
                    'sender': sender,
                    'meter_reading': str(meter_reading)
                }
            )
        )
        
        print(f"\n✅ Message Published!")
        print(f"   Message ID: {message_id}")
        print(f"   Sender: {sender}")
        print(f"   Reading: {meter_reading} kWh")
        print(f"   Confidence: {confidence}%")
        print(f"   Exchange: {RABBITMQ_EXCHANGE}")
        print(f"   Routing Key: {RABBITMQ_ROUTING_KEY}")
        
        connection.close()
        return True
        
    except Exception as e:
        print(f"\n❌ Failed to publish: {e}")
        return False

# -------------------------------------------------
# Normalize common OCR mistakes
# -------------------------------------------------
def normalize_ocr_text(text):
    replacements = {
        'O': '0',
        'o': '0',
        'B': '8',
        'S': '5',
        'I': '1',
        'l': '1',
        ' ': '',
        '.': '',
        ',': ''
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    return text


def extract_water_meter_reading(image_path):
    processed = preprocess_meter_image(image_path)
    results = reader.readtext(processed)

    candidates = []

    for bbox, text, confidence in results:
        cleaned = normalize_ocr_text(text)

        if confidence < 0.35:
            continue

        if cleaned.isdigit() and 3 <= len(cleaned) <= 6:
            candidates.append({
                "reading": cleaned,
                "confidence": confidence
            })

    if not candidates:
        return None

    # Choose longest + highest confidence
    best = sorted(
        candidates,
        key=lambda x: (len(x["reading"]), x["confidence"]),
        reverse=True
    )[0]

    return {
        "reading": int(best["reading"]),
        "confidence": round(best["confidence"], 2)
    }

    """
    Enforce mechanical water meter rules
    """
    reading_str = str(raw_reading)

    # Rule: only first 4 digits for this meter type
    normalized = reading_str[:4]

    return int(normalized)

 #-------------------------------------------------
# Mechanical water meter rules
# -------------------------------------------------
def normalize_mechanical_meter_reading(raw_reading):
    """
    Mechanical meters rule:
    - Read LOWER number on rollover
    - Ignore red digits
    - Fixed digit count (older meters = 4 digits)
    """

    reading_str = str(raw_reading)

    # ✅ ONLY first 4 digits for THIS meter type
    return int(reading_str[:4])


# -------------------------------------------------
# Image Preprocessing (dirty / old meters)
# -------------------------------------------------
def preprocess_meter_image(image_path):
    image = cv2.imread(image_path)

    if image is None:
        raise ValueError("Image not found or unreadable")

    # Convert to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Increase contrast
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    contrast = clahe.apply(gray)

    # Reduce noise
    blurred = cv2.GaussianBlur(contrast, (5, 5), 0)

    # Adaptive threshold (better for uneven lighting)
    thresh = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        11,
        2
    )

    return thresh


# -------------------------------------------------
# Extract water meter reading
# -------------------------------------------------
def extract_water_meter_reading(image_path):
    processed = preprocess_meter_image(image_path)
    results = reader.readtext(processed)

    candidates = []

    # DEBUG: uncomment if you want to see raw OCR
    # print("\n--- RAW OCR ---")
    # for _, text, conf in results:
    #     print(f"{text} ({conf:.2f})")

    for bbox, text, confidence in results:
        if confidence < 0.30:
            continue

        cleaned = normalize_ocr_text(text)

        # Accept digit groups only
        if cleaned.isdigit() and 3 <= len(cleaned) <= 6:
            candidates.append({
                "reading": cleaned,
                "confidence": confidence,
                "x_pos": bbox[0][0]  # used to ignore red digits (right side)
            })

    if not candidates:
        return None

    # Sort:
    # 1️⃣ Longer digit group
    # 2️⃣ Higher confidence
    # 3️⃣ More to the left (black digits)
    best = sorted(
        candidates,
        key=lambda x: (len(x["reading"]), x["confidence"], -x["x_pos"]),
        reverse=True
    )[0]

    return {
        "raw_reading": best["reading"],
        "confidence": round(best["confidence"], 2)
    }


# -------------------------------------------------
# MAIN
# -------------------------------------------------
if __name__ == "__main__":
    image_path = "Zamdela-Meter-01.jpg"

    ocr_result = extract_water_meter_reading(image_path)

    if ocr_result is None:
        print("❌ OCR failed – please retake photo")
    else:
        final_reading = normalize_mechanical_meter_reading(
            ocr_result["raw_reading"]
        )

        print("✅ FINAL WATER METER READING")
        print("----------------------------")
        print(f"Reading   : {final_reading}")
        print(f"Confidence: {ocr_result['confidence']}")