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

# AWS clients
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ.get("DYNAMO_TABLE", "MessagesTable"))
s3_client = boto3.client("s3")
textract_client = boto3.client("textract")



# Environment variables
BUCKET = os.environ.get("S3_BUCKET", "whatsapp-media-storage-provide-your-ownbustket")
CLOUDFRONT_DOMAIN = os.environ.get("CLOUDFRONT_DOMAIN", "https://somethingsomewhere.cloudfront.net")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Retry helper (exponential backoff)
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

def extract_meter_reading_from_s3(bucket, key):
    """
    Extract text from image stored in S3 using AWS Textract.
    Returns the detected meter reading and confidence score.
    """
    try:
        print(f"Starting Textract OCR on s3://{bucket}/{key}")
        
        # Call Textract to detect text
        response = textract_client.detect_document_text(
            Document={
                'S3Object': {
                    'Bucket': bucket,
                    'Name': key
                }
            }
        )
        
        print("Textract SUCCESS")
        print(json.dumps(response, default=str))
        
        # Extract all detected text
        detected_text = []
        for block in response.get('Blocks', []):
            if block['BlockType'] == 'LINE':
                text = block.get('Text', '')
                confidence = block.get('Confidence', 0)
                detected_text.append({
                    'text': text,
                    'confidence': confidence
                })
                print(f"Detected: '{text}' (confidence: {confidence:.2f}%)")
        
        # Extract meter reading using pattern matching
        meter_reading = extract_reading_from_text(detected_text)
        
        return {
            'reading': meter_reading.get('value'),
            'confidence': meter_reading.get('confidence'),
            'raw_text': [item['text'] for item in detected_text],
            'method': meter_reading.get('method', 'pattern_match')
        }
        
    except Exception as e:
        print(f"Textract OCR failed: {e}")
        return {
            'reading': None,
            'confidence': 0,
            'raw_text': [],
            'error': str(e)
        }

def extract_reading_from_text(detected_text):
    """
    Extract meter reading from detected text using multiple strategies.
    Returns dict with value, confidence, and method used.
    """
    if not detected_text:
        return {'value': None, 'confidence': 0, 'method': 'no_text'}
    
    # Strategy 1: Look for pure numbers (most common for digital meters)
    # Pattern: 4-6 digits, possibly with decimal point
    number_pattern = r'\b(\d{4,6}(?:\.\d{1,2})?)\b'
    
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        
        # Try to find meter reading pattern
        matches = re.findall(number_pattern, text)
        if matches:
            # Take the first substantial number found
            reading = matches[0]
            print(f"✓ Extracted reading: {reading} (confidence: {confidence:.2f}%)")
            return {
                'value': float(reading),
                'confidence': confidence,
                'method': 'digit_pattern'
            }
    
    # Strategy 2: Look for numbers with units (kWh, m³, etc.)
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
    
    # Strategy 3: Take the longest number found (fallback)
    all_numbers = []
    for item in detected_text:
        text = item['text']
        confidence = item['confidence']
        
        # Find all numbers in the text
        numbers = re.findall(r'\d+(?:\.\d+)?', text)
        for num in numbers:
            if len(num) >= 3:  # At least 3 digits
                all_numbers.append({
                    'value': float(num),
                    'confidence': confidence,
                    'length': len(num)
                })
    
    if all_numbers:
        # Sort by length (longer numbers are more likely to be meter readings)
        all_numbers.sort(key=lambda x: x['length'], reverse=True)
        best = all_numbers[0]
        print(f"⚠ Fallback: Using longest number: {best['value']} (confidence: {best['confidence']:.2f}%)")
        return {
            'value': best['value'],
            'confidence': best['confidence'],
            'method': 'longest_number'
        }
    
    # No reading found
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
    message = parsed.get("Body", [""])[0]  
    meter_number = parsed.get("MeterNumber", [None])[0]
    num_media = int(parsed.get("NumMedia", ["0"])[0])

    if not sender:
        print("No sender found, ignoring.")
        return {"statusCode": 200, "body": "OK"}

    media_urls = []
    ocr_results = []

    # Process media if exists
    for i in range(num_media):
        media_url = parsed.get(f"MediaUrl{i}", [None])[0]
        media_type = parsed.get(f"MediaContentType{i}", [None])[0]

        if not media_url or not media_type:
            continue

        # Only process images
        if not media_type.startswith("image/"):
            print(f"Skipping non-image media: {media_type}")
            continue

        # Download media with retry
        def download():
            print(f"Downloading media: {media_url}")
            response = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=10)
            response.raise_for_status()
            return response.content

        try:
            media_bytes = retry(download)
        except Exception as e:
            print(f"Failed to download media: {e}")
            continue

        # Upload to S3 with structured key
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
        except Exception as e:
            print(f"Failed to upload to S3: {e}")
            continue

        # Generate CloudFront URL
        media_url_cf = f"{CLOUDFRONT_DOMAIN}/{s3_key}"
        media_urls.append(media_url_cf)
        print(f"Media available at: {media_url_cf}")

        # ✨ NEW: Extract meter reading using OCR
        ocr_result = extract_meter_reading_from_s3(BUCKET, s3_key)
        ocr_results.append({
            's3_key': s3_key,
            'reading': ocr_result.get('reading'),
            'confidence': ocr_result.get('confidence'),
            'raw_text': ocr_result.get('raw_text'),
            'method': ocr_result.get('method'),
            'error': ocr_result.get('error')
        })

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

    # Save message + media URLs + OCR results in DynamoDB
    item = {
        "Id": str(uuid.uuid4()),
        "MessageSid": parsed.get("MessageSid", [None])[0],
        "sender": sender,
        "message": message,
        "media_urls": media_urls,
        "meterType": "electricity",
        "meterNumber": meter_number,
        "meterReading": meter_reading,
        "ocrConfidence": best_confidence,
        "ocrResults": ocr_results,
        "timestamp": datetime.utcnow().isoformat()
    }

    print("Saving item:", json.dumps(item, default=str))
    table.put_item(Item=item)

    # Prepare TwiML reply with meter reading
    twiml_parts = ["<?xml version='1.0' encoding='UTF-8'?><Response>"]
    
    if meter_reading and best_confidence > 70:
        # High confidence reading
        twiml_parts.append(
            f"<Message>✅ Meter reading received: {meter_reading:.2f} kWh "
            f"(Confidence: {best_confidence:.0f}%)</Message>"
        )
    elif meter_reading and best_confidence > 50:
        # Medium confidence - ask for confirmation
        twiml_parts.append(
            f"<Message>⚠️ Detected reading: {meter_reading:.2f} kWh "
            f"(Low confidence: {best_confidence:.0f}%). Please confirm or resend clearer image.</Message>"
        )
    else:
        # Failed to extract reading
        twiml_parts.append(
            "<Message>❌ Could not read meter. Please send a clearer image with the meter display visible.</Message>"
        )

    # Optionally echo the processed image
    for url in media_urls:
        twiml_parts.append(f"<Message><Media>{url}</Media></Message>")

    twiml_parts.append("</Response>")
    twiml = "".join(twiml_parts)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": twiml
    }