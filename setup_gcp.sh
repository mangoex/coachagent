#!/bin/bash

# ==============================================================================
# Google AI Sales Coach Agent - GCP Infrastructure Setup Script
# Author: Principal Google Cloud Platform Architect
# Description: Automates API enablement, sets up Artifact Registry, provisions
#              Cloud SQL (PostgreSQL), provisions Memorystore (Redis), and configures
#              initial Secret Manager placeholders.
# ==============================================================================

set -e # Exit immediately if a command exits with a non-zero status

# Configuration
REGION="us-central1"
DB_INSTANCE_NAME="coachagent-postgres"
DB_NAME="coachagent"
REDIS_INSTANCE_NAME="coachagent-redis"
REPO_NAME="coachagent-repo"

echo "======================================================================"
echo "Initializing GCP Setup for Google AI Sales Coach Agent..."
echo "======================================================================"

# Get active project ID
PROJECT_ID=$(gcloud config get-value project)
if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: No active Google Cloud project configured. Run 'gcloud config set project <PROJECT_ID>' first."
  exit 1
fi
echo "Active Project: $PROJECT_ID"
echo "Region Target: $REGION"

# 1. Enable Required Google Cloud APIs
echo "----------------------------------------------------------------------"
echo "Step 1: Enabling Required GCP & Google Workspace APIs..."
echo "----------------------------------------------------------------------"
gcloud services enable \
  aiplatform.googleapis.com \
  calendar-json.googleapis.com \
  sheets.googleapis.com \
  docs.googleapis.com \
  drive.googleapis.com \
  run.googleapis.com \
  sqladmin.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  redis.googleapis.com \
  vpcaccess.googleapis.com

echo "APIs enabled successfully."

# 2. Create Artifact Registry Docker Repository
echo "----------------------------------------------------------------------"
echo "Step 2: Provisioning Artifact Registry Repository..."
echo "----------------------------------------------------------------------"
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" &>/dev/null; then
  echo "Artifact Repository '$REPO_NAME' already exists. Skipping."
else
  gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Repository for Google AI Sales Coach Agent microservices"
  echo "Artifact Repository created."
fi

# 3. Create Cloud SQL PostgreSQL Instance
echo "----------------------------------------------------------------------"
echo "Step 3: Provisioning Cloud SQL (PostgreSQL 15)..."
echo "----------------------------------------------------------------------"
if gcloud sql instances describe "$DB_INSTANCE_NAME" &>/dev/null; then
  echo "Cloud SQL Instance '$DB_INSTANCE_NAME' already exists. Skipping."
else
  echo "Creating micro-instance database. This may take a few minutes..."
  # db-f1-micro is selected for development/budget-friendly scaling. For production use db-g1-small or custom vCPUs.
  gcloud sql instances create "$DB_INSTANCE_NAME" \
    --database-version=POSTGRES_15 \
    --tier=db-f1-micro \
    --region="$REGION" \
    --storage-type=SSD \
    --storage-auto-increase

  # Create the database inside the instance
  gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE_NAME"
  echo "Cloud SQL PostgreSQL instance and database '$DB_NAME' created."
fi

# 4. Create Memorystore for Redis
echo "----------------------------------------------------------------------"
echo "Step 4: Provisioning Memorystore (Redis)..."
echo "----------------------------------------------------------------------"
if gcloud redis instances describe "$REDIS_INSTANCE_NAME" --region="$REGION" &>/dev/null; then
  echo "Memorystore Redis '$REDIS_INSTANCE_NAME' already exists. Skipping."
else
  echo "Creating Redis cache instance (1GB)..."
  gcloud redis instances create "$REDIS_INSTANCE_NAME" \
    --size=1 \
    --region="$REGION" \
    --redis-version=redis_7_0
  echo "Memorystore Redis provisioned."
fi

# 5. Secret Manager Setup Instructions
echo "----------------------------------------------------------------------"
echo "Step 5: Setting up Secrets in Secret Manager..."
echo "----------------------------------------------------------------------"

create_secret_placeholder() {
  SECRET_NAME=$1
  if gcloud secrets describe "$SECRET_NAME" &>/dev/null; then
    echo "Secret '$SECRET_NAME' already exists."
  else
    gcloud secrets create "$SECRET_NAME" --replication-policy="automatic"
    echo "Placeholder secret '$SECRET_NAME' created. Please add the secret value in the GCP Console."
  fi
}

create_secret_placeholder "COACHAGENT_DATABASE_URL"
create_secret_placeholder "COACHAGENT_ENCRYPTION_KEY"
create_secret_placeholder "COACHAGENT_REDIS_URL"
create_secret_placeholder "COACHAGENT_WHATSAPP_TOKEN"
create_secret_placeholder "COACHAGENT_WHATSAPP_PHONE_NUMBER_ID"
create_secret_placeholder "COACHAGENT_WHATSAPP_VERIFY_TOKEN"
create_secret_placeholder "COACHAGENT_GCS_BUCKET_NAME"
create_secret_placeholder "COACHAGENT_GOOGLE_CLIENT_ID"
create_secret_placeholder "COACHAGENT_GOOGLE_CLIENT_SECRET"

echo "======================================================================"
echo "GCP PROVISIONING SETUP COMPLETE"
echo "======================================================================"
echo "Next Steps:"
echo "1. Visit Secret Manager in the Google Cloud Console and populate values"
echo "   for the 'COACHAGENT_*' secrets created."
echo "2. Generate your AES-256 base64 Fernet key and save it in COACHAGENT_ENCRYPTION_KEY."
echo "3. Run your Cloud Build pipeline using:"
echo "   gcloud builds submit --config=cloudbuild.yaml ."
echo "======================================================================"
