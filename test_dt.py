import datetime
target_date = datetime.datetime.utcnow().date()
print(datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z')
