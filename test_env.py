from dotenv import load_dotenv
import os

load_dotenv()

cloud = os.getenv('CLOUDINARY_CLOUD_NAME')
key = os.getenv('CLOUDINARY_API_KEY')
secret = os.getenv('CLOUDINARY_API_SECRET')

print('Cloud name:', cloud)
print('API key:', key)
print('Secret length:', len(secret) if secret else 0)
print('Secret first 5 chars:', secret[:5] if secret else None)