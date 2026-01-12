# WhatsApp Meter Reading Processing System

This project is a cloud-native, event-driven system that allows users to submit meter readings via WhatsApp.
Images are received through Twilio webhooks, processed asynchronously, and stored for monthly billing generation.

## Key Features
- WhatsApp integration using Twilio webhooks
- Serverless ingestion with AWS Lambda and API Gateway
- Image storage in Amazon S3
- Raw message persistence in Amazon DynamoDB
- Asynchronous messaging using RabbitMQ
- .NET microservice for billing preparation
- Reliable, idempotent, and scalable event-driven architecture

## High-Level Architecture
WhatsApp → Twilio → API Gateway → Lambda → DynamoDB → RabbitMQ → .NET Billing Service → WhatsApp Webhook

This project is intended as a real-world case study showcasing modern microservices architecture,
cloud-native design, and asynchronous processing patterns.

## Technology Stack
- WhatsApp Business API (Twilio)
- AWS API Gateway
- AWS Lambda (Python)
- Amazon DynamoDB
- Amazon S3
- RabbitMQ
- .NET (Billing Microservice)
- Docker & Docker Compose


