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
from PIL import Image, ImageEnhance
from io import BytesIO

# AWS clients
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ.get("DYNAMO_TABLE", "MessagesTable"))
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

def handle_confirmation(sender, confirmed):
    if confirmed:
        reply = (
            "<Message>✅ Thank you! Your meter reading has been confirmed "
            "and recorded successfully.</Message>"
        )
    else:
        reply = (
            "<Message>❌ No problem. Please resend a clearer image of your meter "
            "with the digits fully visible.</Message>"
        )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": f"<?xml version='1.0' encoding='UTF-8'?><Response>{reply}</Response>"
    }

def to_decimal(value):
    if value is None:
        return None
    return Decimal(str(value))

def preprocess_image(image_bytes):
    image = Image.open(BytesIO(image_bytes)).convert("L")  # grayscale

    # Increase contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)

    # Increase sharpness
    sharpness = ImageEnhance.Sharpness(image)
    image = sharpness.enhance(2.0)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()

def retry(func, max_attempts=3, initial_delay=0.3):
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
    """
    Extract meter reading from S3 image using AWS Textract.
    """
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
            'value': None,  # Changed from 'reading' to 'value'
            'confidence': 0,
            'method': 'textract_error',
            'error': str(e),
            'raw_text': []
        }

def extract_reading_from_text(detected_text):
    """
    Extract meter reading from detected text using multiple strategies.
    """
    if not detected_text:
        return {'value': None, 'confidence': 0, 'method': 'no_text'}
    
    # Strategy 1: Look for pure numbers (4-6 digits)
    number_pattern = r'\b(\d{4,6}(?:\.\d{1,2})?)\b'
    
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        
        matches = re.findall(number_pattern, text)
        if matches:
            reading = matches[0]
            print(f"✓ Extracted reading: {reading} (confidence: {confidence:.2f}%)")
            return {
                'value': float(reading),
                'confidence': confidence,
                'method': 'digit_pattern'
            }
    
    # Strategy 2: Look for numbers with units
    unit_pattern = r'(\d{3,6}(?:\.\d{1,2})?)\s*(?:kWh|kwh|KWH|m³|m3|cubic|units?)?'
    
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        
        matches = re.findall(unit_pattern, text, re.IGNORECASE)
        if matches:
            reading = matches[0]
            print(f"✓ Extracted reading with unit: {reading} (confidence: {confidence:.2f}%)")
            return {
                'value': float(reading),
                'confidence': confidence,
                'method': 'unit_pattern'
            }
    
    # Strategy 3: Take longest number (fallback)
    all_numbers = []
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        
        numbers = re.findall(r'\d+(?:\.\d+)?', text)
        for num in numbers:
            if len(num) >= 3:
                all_numbers.append({
                    'value': float(num),
                    'confidence': confidence,
                    'length': len(num)
                })
    
    if all_numbers:
        all_numbers.sort(key=lambda x: x['length'], reverse=True)
        best = all_numbers[0]
        print(f"⚠ Fallback: Using longest number: {best['value']} (confidence: {best['confidence']:.2f}%)")
        return {
            'value': best['value'],
            'confidence': best['confidence'],
            'method': 'longest_number'
        }
    
    print("✗ Could not extract meter reading from image")
    return {'value': None, 'confidence': 0, 'method': 'failed'}

def lambda_handler(event, context):
    print("Raw event:", json.dumps(event))

    # Decode body
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    # Parse form-urlencoded
    parsed = urllib.parse.parse_qs(body)
    
    sender = parsed.get("From", [None])[0]
    sender_phone = sender.replace("whatsapp:", "") if sender else None
    message = parsed.get("Body", [""])[0]  
    meter_number = parsed.get("Meter Number", [None])[0]
    num_media = int(parsed.get("NumMedia", ["0"])[0])

    normalized_message = message.strip().lower() if message else ""

    # FIXED INDENTATION - These if statements align properly now
    if normalized_message in ["yes", "y"]:
        return handle_confirmation(sender, confirmed=True)
    if normalized_message in ["no", "n"]:
        return handle_confirmation(sender, confirmed=False)

    if not sender:
        print("No sender found, ignoring.")
        return {"statusCode": 200, "body": "OK"}

    print(f"Processing message from {sender}: '{message}' with {num_media} media items")

    media_urls = []
    ocr_results = []

    # Process media if exists
    if num_media > 0:
        for i in range(num_media):
            media_url = parsed.get(f"MediaUrl{i}", [None])[0]
            media_type = parsed.get(f"MediaContentType{i}", [None])[0]

            if not media_url or not media_type:
                print(f"Media {i}: Missing URL or type")
                continue

            if not media_type.startswith("image/"):
                print(f"Skipping non-image media: {media_type}")
                continue

            print(f"Processing image {i}: {media_type}")

            def download():
                print(f"Downloading media: {media_url}")
                response = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=10)
                response.raise_for_status()
                return response.content

            try:
                raw_bytes = retry(download)
                media_bytes = preprocess_image(raw_bytes)
                print(f"Downloaded {len(media_bytes)} bytes")
            except Exception as e:
                print(f"Failed to download media: {e}")
                continue

            # Upload to S3
            now = datetime.utcnow()
            year = now.year
            month = f"{now.month:02d}"
            message_sid = parsed.get("MessageSid", [str(uuid.uuid4())])[0]
            ext = media_type.split("/")[-1]

            s3_key = f"meters/{year}/{month}/{message_sid}_{i}.{ext}"

            def upload():
                print(f"Uploading to S3: {s3_key}")
                s3_client.put_object(
                    Bucket=BUCKET,
                    Key=s3_key,
                    Body=media_bytes,
                    ContentType=media_type
                )
                return True

            try:
                retry(upload)
                print(f"Successfully uploaded to S3: {s3_key}")
            except Exception as e:
                print(f"Failed to upload to S3: {e}")
                continue

            # Generate CloudFront URL
            media_url_cf = f"{CLOUDFRONT_DOMAIN}/{s3_key}"
            media_urls.append(media_url_cf)
            print(f"Media available at: {media_url_cf}")

            # Extract meter reading using OCR
            print(f"Starting OCR extraction for {s3_key}")
            ocr_result = extract_meter_reading_from_s3(BUCKET, s3_key)
            
            # FIXED: Use 'value' key instead of 'reading'
            ocr_results.append({
                's3_key': s3_key,
                'reading': ocr_result.get('value'),  # Changed from 'reading' to 'value'
                'confidence': ocr_result.get('confidence'),
                'raw_text': ocr_result.get('raw_text'),
                'method': ocr_result.get('method'),
                'error': ocr_result.get('error')
            })
            print(f"OCR complete for {s3_key}: reading={ocr_result.get('value')}, confidence={ocr_result.get('confidence')}")
    else:
        print("No media attachments to process")

    # Determine final meter reading (use highest confidence)
    meter_reading = None
    best_confidence = 0
    
    if ocr_results:
        valid_results = [r for r in ocr_results if r['reading'] is not None]
        if valid_results:
            best = max(valid_results, key=lambda x: x['confidence'])
            meter_reading = best['reading']
            best_confidence = best['confidence']
            print(f"✓ Final meter reading: {meter_reading} (confidence: {best_confidence:.2f}%)")
        else:
            print("No valid meter readings extracted from images")

    # Save to DynamoDB
    item = {
        "Id": str(uuid.uuid4()),
        "MessageSid": parsed.get("MessageSid", [None])[0],
        "sender": sender_phone,
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

    print("Saving item to DynamoDB:", json.dumps(item, default=str))
    try:
        table.put_item(Item=item)
        print("Successfully saved to DynamoDB")
    except Exception as e:
        print(f"Failed to save to DynamoDB: {e}")

    # Prepare TwiML reply
    twiml_parts = ["<?xml version='1.0' encoding='UTF-8'?><Response>"]
    
    if num_media > 0:
        # LOWERED THRESHOLD: Accept readings with confidence > 25% instead of 70%
        if meter_reading and best_confidence > 25:
            twiml_parts.append(
                f"<Message>✅ Meter reading received: {meter_reading:.0f} kWh "
                f"(Confidence: {best_confidence:.0f}%). "
                f"Reply YES to confirm or NO to resend.</Message>"
            )
        else:
            twiml_parts.append(
                "<Message>❌ Could not read meter. Please send a clearer image with the meter display visible.</Message>"
            )

        # Echo processed image
        for url in media_urls:
            twiml_parts.append(f"<Message><Media>{url}</Media></Message>")
    else:
        if message:
            twiml_parts.append(
                f"<Message>Message received: {message}. To submit a meter reading, please send an image of your meter.</Message>"
            )
        else:
            twiml_parts.append(
                "<Message>Please send an image of your meter to submit a reading.</Message>"
            )

    twiml_parts.append("</Response>")
    twiml = "".join(twiml_parts)

    print("Returning TwiML response")
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": twiml
    }