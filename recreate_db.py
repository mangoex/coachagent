from database.connection import engine, Base
from database.models import *

print("Dropping all tables...")
Base.metadata.drop_all(bind=engine)
print("Creating all tables...")
Base.metadata.create_all(bind=engine)
print("Database migrated successfully.")
