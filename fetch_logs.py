import os
from google.cloud import logging as cloud_logging

client = cloud_logging.Client()
entries = client.list_entries(
    filter_='resource.type="cloud_run_revision" AND resource.labels.service_name="coachagent-service"',
    order_by=cloud_logging.DESCENDING,
    max_results=30
)
for e in entries:
    if "Failed to list events" in str(e.payload) or "Error listing events" in str(e.payload) or "Traceback" in str(e.payload):
        print(f"ERROR FOUND: {e.payload}")
    if isinstance(e.payload, str) and ("Error" in e.payload or "Exception" in e.payload):
        print(f"POTENTIAL ERROR: {e.payload}")
print("Done")
