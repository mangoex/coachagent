import os
from google.cloud import logging as cloud_logging

client = cloud_logging.Client(project="coach-agent-499614")
entries = client.list_entries(
    filter_='resource.type="cloud_run_revision" AND severity>=WARNING',
    order_by=cloud_logging.DESCENDING,
    max_results=20
)
for e in entries:
    print(f"[{e.severity}] {e.timestamp}: {e.payload}")
print("Done")
