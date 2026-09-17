

## Whatzapp Process flow (Lambda Function) –  Architecture

```mermaid
flowchart TD
    %% Real-time processing
    A1[1. WhatsApp User]
    A2[2. Twilio WhatsApp API]
    A3[3. API Gateway]
    A4[4. Lambda Meter Processor]

    A5[(5. DynamoDB MessagesTable)]
    A6[(6. DynamoDB OutboxTable)]

    A7[7. RabbitMQ]
    A8[8. C# Billing Consumer]

    %% Retry mechanism
    B1[9. EventBridge Schedule<br/>Every 2 Minutes]
    B2[10. Lambda Outbox Retry Processor]

    %% Real-time flow
    A1 --> A2
    A2 --> A3
    A3 --> A4

    A4 -->|Save message| A5
    A4 -->|Create outbox record| A6
    A4 -->|Immediate publish| A7

    A7 --> A8

    %% Retry flow (clearly separated)
    B1 -->|Trigger| B2
    B2 -->|Scan PENDING| A6
    B2 -->|Retry publish| A7

```
---
### 1. Description
- A **serverless message processing architecture** for WhatsApp meter readings.
- Uses **AWS Lambda** to handle incoming WhatsApp messages, OCR processing via **Amazon Textract**, and storage in **DynamoDB**.
- Publishes messages to **RabbitMQ**, ensuring reliable delivery with a **retry mechanism** using **OutboxTable** + **EventBridge**.
- **C# Billing Consumer** processes messages asynchronously from RabbitMQ.
### 2. Benefits
- **Serverless & scalable** – Lambda handles spikes in messages without provisioning servers.
- **Reliable message delivery** – RabbitMQ + Outbox retry ensures messages are not lost.
- **Decoupled architecture** – WhatsApp processing, OCR, storage, and billing are independent.
- **Real-time feedback** – Users receive a WhatsApp response quickly.
- **Audit & persistence** – All messages are saved in DynamoDB, retryable if failures occur.
- **Extensible** – Easy to add new consumers or integrations.
### 3. Process (Step-by-Step)
1. **User sends WhatsApp message/image** → Twilio WhatsApp API receives it.
2. **API Gateway** triggers **Lambda Meter Processor**.
3. **Lambda** processes the message and:
    - Uploads image to **S3**
    - Runs **OCR via Amazon Textract**
    - Saves reading to **DynamoDB MeterReadings**
    - Creates URL via **CloudFront** for user access
    - Sends **TwiML response** back via Twilio.

4. Lambda attempts to publish message to RabbitMQ.
    - ✅ Success → Message delivered to **C# Billing Consumer**.
    - ❌ Failure → Message saved in **OutboxTable**.
5. **EventBridge triggers a retry Lambda every 2 minutes**.
    - Scans **OutboxTable** for pending messages
    - Retries publishing to RabbitMQ with **exponential backoff**
    - Stops after **maximum 5 retries**.
6. **C# Billing Consumer** reads messages from RabbitMQ and processes billing.

### 4. Strengths of This Architecture
* Fully serverless
* Scales automatically
* Region-aware service usage
* Fault-tolerant (retries + confirmations)

### 5. Possible Upgrade (Optional)
* Add Rekognition for blur detection
* Add Step Functions for async OCR
* Support water meters & gas meters
* Admin dashboard (Athena + QuickSight)
