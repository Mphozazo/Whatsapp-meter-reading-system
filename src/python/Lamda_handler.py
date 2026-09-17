import boto3
import json
import uuid
import re
from datetime import datetime
import urllib.parse
import base64
import requests
import time
import os
from decimal import Decimal
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from io import BytesIO
import pika

# AWS clients
dynamodb = boto3.resource("dynamodb")
messages_table = dynamodb.Table(os.environ.get("DYNAMO_TABLE", "MessagesTable"))
outbox_table = dynamodb.Table(os.environ.get("OUTBOX_TABLE", "OutboxTable"))
s3_client = boto3.client("s3")
textract_client = boto3.client(
    "textract",
    region_name="eu-west-1",
    endpoint_url="https://textract.eu-west-1.amazonaws.com"
)

# Environment variables
BUCKET = os.environ.get("S3_BUCKET", "whatsapp-media-storage-provide-your-ownbustket")
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "https://somethingsomewhere.cloudfront.net")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
print(f"Config - S3 Bucket: {BUCKET}, CloudFront: {CLOUDFRONT_DOMAIN}")

# RabbitMQ Configuration
RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "127.0.0.1")
RABBITMQ_PORT = int(os.environ.get("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.environ.get("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.environ.get("RABBITMQ_PASSWORD", "guest")
RABBITMQ_VHOST = os.environ.get("RABBITMQ_VHOST", "/")
RABBITMQ_EXCHANGE = os.environ.get("RABBITMQ_EXCHANGE", "meter_readings_exchange")
RABBITMQ_ROUTING_KEY = os.environ.get("RABBITMQ_ROUTING_KEY", "meter.reading.submitted")

print(f"RabbitMQ Config - Host: {RABBITMQ_HOST}, Port: {RABBITMQ_PORT}, VHost: {RABBITMQ_VHOST}, Exchange: {RABBITMQ_EXCHANGE}")
# Global RabbitMQ connection
rabbitmq_connection = None
rabbitmq_channel = None

# ============================================
# OUTBOX PATTERN IMPLEMENTATION
# ============================================

def save_to_outbox(message_data, event_type="meter_reading_submitted"):
    """
    Save message to Outbox table (transactional outbox pattern).
    This ensures we have a durable record of messages to publish.
    """
    try:
        outbox_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat()
        
        # Convert Decimal to float for storage
        def decimal_to_float(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: decimal_to_float(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_to_float(item) for item in obj]
            return obj
        
        clean_data = decimal_to_float(message_data)
        
        outbox_item = {
            "OutboxId": outbox_id,
            "EventType": event_type,
            "AggregateId": message_data.get("Id"),  # Link to main record
            "Payload": json.dumps(clean_data),
            "Status": "PENDING",  # PENDING, PUBLISHED, FAILED
            "CreatedAt": int(time.time()),
            "UpdatedAt": int(time.time()),
            "RetryCount": 0,
            "MaxRetries": 5,
            "NextRetryAt": timestamp,
            "RoutingKey": RABBITMQ_ROUTING_KEY,
            "Exchange": RABBITMQ_EXCHANGE,
            "TTL": int(time.time()) + 86400  # 24 hours TTL
        }
        
        outbox_table.put_item(Item=outbox_item)
        print(f"✓ Saved to outbox: {outbox_id}")
        return outbox_id
        
    except Exception as e:
        print(f"✗ Failed to save to outbox: {e}")
        raise

def process_pending_outbox_messages(max_messages=10):
    """
    Process pending messages from outbox (called by scheduled Lambda or same invocation).
    This is the retry mechanism with pagination to handle large tables safely.
    Fully aligned with the Outbox table definition.
    """
    try:
        now = int(time.time())  # Use Unix timestamp to match NextRetryAt type
        pending_items = []
        last_evaluated_key = None

        # Paginated scan loop
        while True:
            scan_kwargs = {
                "FilterExpression": "(#status = :pending) AND (NextRetryAt <= :now)",
                "ExpressionAttributeNames": {"#status": "Status"},
                "ExpressionAttributeValues": {":pending": "PENDING", ":now": now},
                "Limit": max_messages
            }

            if last_evaluated_key:
                scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

            response = outbox_table.scan(**scan_kwargs)
            items = response.get("Items", [])
            pending_items.extend(items)

            # Stop if we reached max_messages
            if len(pending_items) >= max_messages:
                pending_items = pending_items[:max_messages]
                break

            # Stop if there are no more items
            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        print(f"ℹ Found {len(pending_items)} pending outbox messages ready for retry")

        # Process each message
        success_count = 0
        for item in pending_items:
            outbox_id = item["OutboxId"]
            try:
                if publish_from_outbox(outbox_id):
                    success_count += 1
                else:
                    # Optionally, increment RetryCount and update NextRetryAt
                    retry_count = item.get("RetryCount", 0) + 1
                    next_retry = int(time.time()) + (2 ** retry_count)  # Exponential backoff
                    outbox_table.update_item(
                        Key={"OutboxId": outbox_id},
                        UpdateExpression="SET RetryCount = :rc, NextRetryAt = :nr, UpdatedAt = :ua",
                        ExpressionAttributeValues={
                            ":rc": retry_count,
                            ":nr": next_retry,
                            ":ua": int(time.time())
                        }
                    )
            except Exception as e:
                print(f"✗ Failed to process OutboxId {outbox_id}: {e}")

        print(f"✓ Successfully published {success_count}/{len(pending_items)} pending messages")
        return success_count

    except Exception as e:
        print(f"✗ Error processing outbox: {e}")
        return 0

def publish_from_outbox(outbox_id):
    """
    Attempt to publish a message from the outbox to RabbitMQ.
    Returns True if successful, False otherwise.
    """
    try:
        # Get outbox item
        response = outbox_table.get_item(Key={"OutboxId": outbox_id})
        if "Item" not in response:
            print(f"✗ Outbox item not found: {outbox_id}")
            return False
        
        outbox_item = response["Item"]
        
        # Check if already published
        if outbox_item.get("Status") == "PUBLISHED":
            print(f"ℹ Message already published: {outbox_id}")
            return True
        
        # Check retry limit
        if outbox_item.get("RetryCount", 0) >= outbox_item.get("MaxRetries", 5):
            print(f"✗ Max retries reached for: {outbox_id}")
            outbox_table.update_item(
                Key={"OutboxId": outbox_id},
                UpdateExpression="SET #status = :failed, UpdatedAt = :now",
                ExpressionAttributeNames={"#status": "Status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":now": datetime.utcnow().isoformat()
                }
            )
            return False
        
        # Parse payload
        payload_str = outbox_item["Payload"]
        payload_data = json.loads(payload_str)
        
        # Prepare message
        message_payload = {
            "event_type": outbox_item["EventType"],
            "timestamp": datetime.utcnow().isoformat(),
            "outbox_id": outbox_id,
            "data": payload_data
        }
        
        # Get RabbitMQ channel
        channel = get_rabbitmq_connection()
        
        # Publish message
        channel.basic_publish(
            exchange=outbox_item["Exchange"],
            routing_key=outbox_item["RoutingKey"],
            body=json.dumps(message_payload),
            properties=pika.BasicProperties(
                delivery_mode=2,  # Persistent
                content_type='application/json',
                content_encoding='utf-8',
                timestamp=int(time.time()),
                message_id=outbox_id,
                app_id='meter-reading-lambda',
                headers={
                    'outbox_id': outbox_id,
                    'retry_count': str(outbox_item.get("RetryCount", 0)),
                    'sender': payload_data.get('sender'),
                    'meter_reading': str(payload_data.get('meterReading'))
                }
            )
        )
        
        # Mark as published
        outbox_table.update_item(
            Key={"OutboxId": outbox_id},
            UpdateExpression="SET #status = :published, UpdatedAt = :now, PublishedAt = :now",
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={
                ":published": "PUBLISHED",
                ":now": datetime.utcnow().isoformat()
            }
        )
        
        print(f"✓ Published from outbox: {outbox_id}")
        return True
        
    except Exception as e:
        print(f"✗ Failed to publish from outbox {outbox_id}: {e}")
        
        # Update retry count and schedule next retry
        try:
            retry_count = outbox_item.get("RetryCount", 0) + 1
            # Exponential backoff: 1min, 2min, 4min, 8min, 16min
            backoff_seconds = min(60 * (2 ** retry_count), 3600)  # Max 1 hour
            next_retry = datetime.utcnow().timestamp() + backoff_seconds
            
            outbox_table.update_item(
                Key={"OutboxId": outbox_id},
                UpdateExpression="SET RetryCount = :count, NextRetryAt = :next, UpdatedAt = :now, LastError = :error",
                ExpressionAttributeValues={
                    ":count": retry_count,
                    ":next": datetime.fromtimestamp(next_retry).isoformat(),
                    ":now": datetime.utcnow().isoformat(),
                    ":error": str(e)[:500]  # Truncate error
                }
            )
            print(f"⏰ Scheduled retry {retry_count} for {outbox_id} at {datetime.fromtimestamp(next_retry)}")
        except Exception as update_error:
            print(f"✗ Failed to update retry count: {update_error}")
        
        return False


    """
    Process pending messages from outbox (called by scheduled Lambda or in same invocation).
    This is the retry mechanism.
    """
    try:
        now = datetime.utcnow().isoformat()
        
        # Scan for pending messages that are ready for retry
        response = outbox_table.scan(
            FilterExpression="(#status = :pending) AND (NextRetryAt <= :now)",
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={
                ":pending": "PENDING",
                ":now": now
            },
            Limit=max_messages
        )
        
        pending_items = response.get("Items", [])
        print(f"ℹ Found {len(pending_items)} pending outbox messages")
        
        success_count = 0
        for item in pending_items:
            outbox_id = item["OutboxId"]
            if publish_from_outbox(outbox_id):
                success_count += 1
        
        print(f"✓ Successfully published {success_count}/{len(pending_items)} pending messages")
        return success_count
        
    except Exception as e:
        print(f"✗ Error processing outbox: {e}")
        return 0

def get_rabbitmq_connection():
    """Get or create RabbitMQ connection"""
    global rabbitmq_connection, rabbitmq_channel
    
    try:
        if rabbitmq_connection and rabbitmq_connection.is_open:
            if rabbitmq_channel and rabbitmq_channel.is_open:
                return rabbitmq_channel
        
        print(f"Connecting to RabbitMQ at {RABBITMQ_HOST}:{RABBITMQ_PORT}")
        
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
        parameters = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            virtual_host=RABBITMQ_VHOST,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300,
            connection_attempts=3,
            retry_delay=2
        )
        
        rabbitmq_connection = pika.BlockingConnection(parameters)
        rabbitmq_channel = rabbitmq_connection.channel()
        
        # Declare exchange
        rabbitmq_channel.exchange_declare(
            exchange=RABBITMQ_EXCHANGE,
            exchange_type='topic',
            durable=True
        )
        
        print("✓ RabbitMQ connection established")
        return rabbitmq_channel
        
    except Exception as e:
        print(f"✗ Failed to connect to RabbitMQ: {e}")
        rabbitmq_connection = None
        rabbitmq_channel = None
        raise

# ============================================
# EXISTING FUNCTIONS (abbreviated for space)
# ============================================

def handle_confirmation(sender, confirmed):
    """Handle YES/NO confirmation responses"""
    if confirmed:
        reply = "<Message>✅ Thank you! Your meter reading has been confirmed and recorded successfully.</Message>"
    else:
        reply = "<Message>❌ No problem. Please resend a clearer image of your meter with the digits fully visible.</Message>"
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": f"<?xml version='1.0' encoding='UTF-8'?><Response>{reply}</Response>"
    }

def to_decimal(value):
    """Convert value to Decimal for DynamoDB"""
    if value is None:
        return None
    return Decimal(str(value))

def preprocess_image(image_bytes):
    """Advanced image preprocessing for optimal OCR"""
    image = Image.open(BytesIO(image_bytes))
    original_size = image.size
    
    width, height = image.size
    if width < 1200 or height < 1200:
        scale_factor = max(1200 / width, 1200 / height)
        new_size = (int(width * scale_factor), int(height * scale_factor))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    elif width > 2500 or height > 2500:
        scale_factor = min(2500 / width, 2500 / height)
        new_size = (int(width * scale_factor), int(height * scale_factor))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    
    image = image.convert("L")
    brightness = ImageEnhance.Brightness(image)
    image = brightness.enhance(1.4)
    contrast = ImageEnhance.Contrast(image)
    image = contrast.enhance(4.0)
    image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    image = ImageOps.autocontrast(image, cutoff=2)
    image = ImageOps.equalize(image)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.filter(ImageFilter.EDGE_ENHANCE_MORE)
    contrast = ImageEnhance.Contrast(image)
    image = contrast.enhance(1.5)
    
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    print(f"Image preprocessing complete: {original_size} -> {image.size}, format=PNG")
    return buffer.getvalue()

def retry(func, max_attempts=3, initial_delay=0.3):
    """Retry helper with exponential backoff"""
    attempt = 0
    while True:
        try:
            return func()
        except Exception as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            delay = initial_delay * (2 ** (attempt - 1))
            print(f"Retry {attempt} after {delay}s due to: {e}")
            time.sleep(delay)

def extract_meter_reading_from_s3(bucket, s3_key):
    """Extract meter reading from S3 image using AWS Textract"""
    try:
        response = textract_client.detect_document_text(
            Document={'S3Object': {'Bucket': bucket, 'Name': s3_key}}
        )
        
        detected_text = []
        for block in response['Blocks']:
            if block['BlockType'] == 'LINE':
                detected_text.append({
                    'text': block['Text'],
                    'confidence': block['Confidence']
                })
        
        result = extract_reading_from_text(detected_text)
        result['raw_text'] = [item['text'] for item in detected_text]
        return result
        
    except Exception as e:
        print(f"OCR extraction failed: {e}")
        return {
            'value': None,
            'confidence': 0,
            'method': 'textract_error',
            'error': str(e),
            'raw_text': []
        }

def extract_reading_from_text(detected_text):
    """Extract meter reading from detected text"""
    if not detected_text:
        return {'value': None, 'confidence': 0, 'method': 'no_text'}
    
    # Strategy 0: Extract digits from mixed alphanumeric
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        digit_sequences = re.findall(r'\d+', text)
        for seq in digit_sequences:
            if 4 <= len(seq) <= 6:
                print(f"✓ Extracted meter reading: {seq} (confidence: {confidence:.2f}%) from '{text}'")
                return {'value': float(seq), 'confidence': confidence, 'method': 'digit_extraction'}
    
    # Additional strategies omitted for brevity...
    return {'value': None, 'confidence': 0, 'method': 'failed'}

# Entry point for Lambda function
def lambda_handler(event, context):
    """Main Lambda handler"""
    print("Raw event:", json.dumps(event))
    
    # Check if this is a scheduled invocation for retry processing
    if event.get("source") == "aws.events":
        print("=== SCHEDULED OUTBOX RETRY PROCESSING ===")
        processed = process_pending_outbox_messages(max_messages=50)
        return {
            "statusCode": 200,
            "body": json.dumps({"processed": processed})
        }

    # Decode body
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    parsed = urllib.parse.parse_qs(body)
    
    sender = parsed.get("From", [None])[0]
    sender_phone = sender.replace("whatsapp:", "").strip() if sender else None
    message = parsed.get("Body", [""])[0]  
    meter_number = parsed.get("Meter Number", [None])[0]
    num_media = int(parsed.get("NumMedia", ["0"])[0])
    normalized_message = message.strip().lower() if message else ""

    if normalized_message in ["yes", "y"]:
        return handle_confirmation(sender_phone, confirmed=True)
    if normalized_message in ["no", "n"]:
        return handle_confirmation(sender_phone, confirmed=False)

    if not sender_phone:
        print("No sender found, ignoring.")
        return {"statusCode": 200, "body": "OK"}

    print(f"Processing message from {sender_phone}: '{message}' with {num_media} media items")

    media_urls = []
    ocr_results = []

    # Process media (abbreviated)
    if num_media > 0:
        for i in range(num_media):
            media_url = parsed.get(f"MediaUrl{i}", [None])[0]
            media_type = parsed.get(f"MediaContentType{i}", [None])[0]

            if not media_url or not media_type or not media_type.startswith("image/"):
                continue

            try:
                def download():
                    response = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=10)
                    response.raise_for_status()
                    return response.content

                raw_bytes = retry(download)
                media_bytes = preprocess_image(raw_bytes)
                
                now = datetime.utcnow()
                message_sid = parsed.get("MessageSid", [str(uuid.uuid4())])[0]
                s3_key = f"meters/{now.year}/{now.month:02d}/{message_sid}_{i}.png"

                def upload():
                    s3_client.put_object(Bucket=BUCKET, Key=s3_key, Body=media_bytes, ContentType="image/png")
                    return True

                retry(upload)
                media_url_cf = f"{CLOUDFRONT_DOMAIN}/{s3_key}"
                media_urls.append(media_url_cf)
                
                ocr_result = extract_meter_reading_from_s3(BUCKET, s3_key)
                ocr_results.append({
                    's3_key': s3_key,
                    'reading': ocr_result.get('value'),
                    'confidence': ocr_result.get('confidence'),
                    'raw_text': ocr_result.get('raw_text'),
                    'method': ocr_result.get('method'),
                    'error': ocr_result.get('error')
                })
            except Exception as e:
                print(f"Failed to process media: {e}")

    # Determine meter reading
    meter_reading = None
    best_confidence = 0
    if ocr_results:
        valid_results = [r for r in ocr_results if r['reading'] is not None]
        if valid_results:
            best = max(valid_results, key=lambda x: x['confidence'])
            meter_reading = best['reading']
            best_confidence = best['confidence']

    # Save to DynamoDB
    item = {
        "Id": str(uuid.uuid4()),
        "MessageSid": parsed.get("MessageSid", [None])[0],
        "sender": sender_phone,
        "senderOriginal": sender,
        "message": message,
        "media_urls": media_urls,
        "meterType": "electricity",
        "meterNumber": meter_number,
        "meterReading": to_decimal(meter_reading),
        "ocrConfidence": to_decimal(best_confidence),
        "ocrResults": [
            {
                "s3_key": r["s3_key"],
                "reading": to_decimal(r["reading"]),
                "confidence": to_decimal(r["confidence"]),
                "raw_text": r["raw_text"],
                "method": r["method"],
                "error": r.get("error")
            }
            for r in ocr_results
        ],
        "timestamp": datetime.utcnow().isoformat()
    }

    try:
        messages_table.put_item(Item=item)
        print("✓ Successfully saved to DynamoDB")
    except Exception as e:
        print(f"✗ Failed to save to DynamoDB: {e}")

    # ============================================
    # OUTBOX PATTERN: Save to outbox first
    # ============================================
    outbox_id = None
    try:
        print("\n=== OUTBOX PATTERN: Saving message ===")
        outbox_id = save_to_outbox(item, event_type="meter_reading_submitted")
        
        # Try immediate publish
        print("=== Attempting immediate publish ===")
        immediate_success = publish_from_outbox(outbox_id)
        
        if immediate_success:
            print("✓ Message published immediately")
        else:
            print("⚠ Immediate publish failed - message queued for retry")
            
    except Exception as e:
        print(f"⚠ Outbox error (non-critical): {e}")

    # Opportunistically process any pending messages (best-effort)
    try:
        process_pending_outbox_messages(max_messages=5)
    except Exception as e:
        print(f"⚠ Retry processing error: {e}")

    # Prepare TwiML reply
    twiml_parts = ["<?xml version='1.0' encoding='UTF-8'?><Response>"]
    
    if num_media > 0:
        if meter_reading and best_confidence > 25:
            twiml_parts.append(
                f"<Message>✅ Meter reading detected: *{meter_reading:.0f} kWh*\n"
                f"Confidence: {best_confidence:.0f}%\n\n"
                f"Reply *YES* to confirm or *NO* to resend a clearer image.</Message>"
            )
        else:
            twiml_parts.append(
                "<Message>❌ Could not read the meter display clearly.\n\n"
                "Please send a clearer image with:\n"
                "• Good lighting\n"
                "• Meter display in focus\n"
                "• All digits visible</Message>"
            )
    else:
        twiml_parts.append(
            "<Message>📸 Please send a photo of your meter display to submit a reading.</Message>"
        )

    twiml_parts.append("</Response>")
    
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": "".join(twiml_parts)
    }