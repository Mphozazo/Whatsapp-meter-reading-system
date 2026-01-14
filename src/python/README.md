

## Lambda Function –  Architecture

```mermaid
flowchart LR
    User[WhatsApp User]
    Twilio[Twilio WhatsApp API]
    APIGW[API Gateway]
    Lambda[Lambda Meter Processor]

    
    S3EU[S3 Bucket eu-west-1 OCR Media]
    CF[CloudFront CDN]
    Textract[Amazon Textract eu-west-1]
    DDB[(DynamoDB MeterReadings)]

    User -->|Image YES NO| Twilio
    Twilio -->|Webhook| APIGW
    APIGW --> Lambda

    
    Lambda -->|Upload image| S3EU
    S3EU --> Textract
    Textract -->|OCR text| Lambda

    Lambda -->|Save reading| DDB
    Lambda -->|Create URL| CF
    CF --> User

    Lambda -->|TwiML response| Twilio
    Twilio -->|WhatsApp reply| User

```

### Key Design Decisions

* **eu-west-1** required for Amazon Textract
* DynamoDB stores both *raw* and *confirmed* readings
* WhatsApp YES/NO confirmation ensures data quality

### Data Stored in DynamoDB

* sender (WhatsApp number)
* meterReading (Decimal)
* ocrConfidence
* confirmed (true/false)
* image S3 key
* timestamp

### Strengths of This Architecture

* Fully serverless
* Scales automatically
* Region-aware service usage
* Fault-tolerant (retries + confirmations)
* Optimized for real SA electricity meters

### Possible Extensions

* Add Rekognition for blur detection
* Add Step Functions for async OCR
* Support water meters & gas meters
* Admin dashboard (Athena + QuickSight)
