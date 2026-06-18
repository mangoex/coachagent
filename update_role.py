from database.connection import SessionLocal  
from database.models import User  
db = SessionLocal()  
user = db.query(User).filter(User.email == 'mangoex@gmail.com').first()  
if user:  
    user.role = 'admin'  
    db.commit()  
    print('Role updated to admin for', user.email)  
else:  
    print('User not found')  
db.close()  
