# Project Context

## Project
Platform for Protecting Water Chlorination Systems from Cyber Attacks

## Domain
OT / ICS Security

## Dataset
SWaT

## Selected MVP Model
TranAD

## ML Status
Multiple anomaly detection models were trained and evaluated.
TranAD was selected as the baseline model for the MVP.

## Completed
- Dataset experimentation
- Model training
- Model comparison
- TranAD selection
- Model artifacts

## Current Goal
Transform the existing TranAD research implementation into an
end-to-end security monitoring MVP.

## Target MVP Flow

SWaT telemetry
    ↓
Preprocessing
    ↓
TranAD
    ↓
Anomaly Score
    ↓
Alert Engine
    ↓
FastAPI
    ↓
Dashboard

## Future Scope
- Docker
- AWS
- CI/CD
- Security scanning
- Kubernetes
- OpenPLC simulation
