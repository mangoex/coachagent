import json  
data=json.load(open('logs2.json', encoding='utf-8'))  
for d in data:  
    msg = d.get('textPayload', '')  
    if msg and ('list events' in msg.lower() or 'gemini' in msg.lower()):  
        print(msg)  
