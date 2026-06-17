gcloud run jobs create delete-user-job ^
  --image us-central1-docker.pkg.dev/coach-agent-499614/coachagent-repo/coachagent-service:latest ^
  --command python ^
  --args "-c","from sqlalchemy import create_engine, text; import os; engine=create_engine(os.environ['DATABASE_URL'].replace('postgresql+psycopg2://', 'postgresql://')); conn=engine.connect(); conn.execute(text(\"DELETE FROM users WHERE email='renikenini1@gmail.com'\")); conn.commit()" ^
  --region us-central1 ^
  --set-secrets DATABASE_URL=COACHAGENT_DATABASE_URL:latest ^
  --set-cloudsql-instances coach-agent-499614:us-central1:coachagent-postgres
gcloud run jobs execute delete-user-job --region us-central1 --wait
