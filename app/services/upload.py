# app/services/upload.py
#
# Handles all file uploads to Cloudinary for Zuno.
#
# Used for:
# - Seller KYC documents (National ID photos, selfies)
# - Dispute evidence (photos, screenshots)
# - Product catalog photos
# - Profile photos
#
# WHY CLOUDINARY:
# Files should never be stored on your server.
# Your server can restart, crash, or run out of space.
# Cloudinary stores files permanently in the cloud,
# gives you a permanent URL, and handles image
# optimization automatically.

import cloudinary
import cloudinary.uploader
import os

# Configure Cloudinary with credentials from .env
cloudinary.config(
    cloud_name=os.getenv('xosi0fwd'),
    api_key=os.getenv('218796364141752'),
    api_secret=os.getenv('AqqNdPJA04q5GxKhzWfz_olw7fk'),
    secure=True  # always use HTTPS
)


def upload_id_document(file, seller_id, document_type='id_front'):
    """
    Uploads a KYC identity document to Cloudinary.

    Documents are stored in a private folder organized
    by seller ID for easy admin review.

    Args:
        file          : the file object from the request
        seller_id     : seller's database ID
        document_type : 'id_front', 'id_back', or 'selfie'

    Returns dict with:
        success  : True/False
        url      : the Cloudinary URL if successful
        error    : error message if failed
    """
    try:
        # Upload to a private KYC folder
        # Files are organized as: kyc/seller_1/id_front
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/kyc/seller_{seller_id}",
            public_id=document_type,
            # Overwrite if seller resubmits
            overwrite=True,
            # Add metadata tags for easy searching in Cloudinary
            tags=[f"seller_{seller_id}", "kyc", document_type],
            # Resource type auto-detects image vs PDF
            resource_type='auto'
        )

        return {
            'success': True,
            'url': result['secure_url'],
            'public_id': result['public_id']
        }

    except Exception as e:
        print(f"[CLOUDINARY] Upload failed: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def upload_dispute_evidence(file, dispute_id, submitted_by):
    """
    Uploads dispute evidence to Cloudinary.

    Args:
        file         : the file object from the request
        dispute_id   : dispute database ID
        submitted_by : phone number of who is submitting
    """
    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/disputes/dispute_{dispute_id}",
            tags=[f"dispute_{dispute_id}", "evidence"],
            resource_type='auto'
        )

        return {
            'success': True,
            'url': result['secure_url']
        }

    except Exception as e:
        print(f"[CLOUDINARY] Evidence upload failed: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def upload_product_photo(file, seller_id, product_name):
    """
    Uploads a product catalog photo to Cloudinary.
    Automatically optimizes for web display.
    """
    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/products/seller_{seller_id}",
            tags=[f"seller_{seller_id}", "product"],
            # Automatically resize to max 1200px width
            # while maintaining aspect ratio
            transformation=[
                {'width': 1200, 'crop': 'limit'},
                {'quality': 'auto'}
            ],
            resource_type='image'
        )

        return {
            'success': True,
            'url': result['secure_url']
        }

    except Exception as e:
        print(f"[CLOUDINARY] Product photo upload failed: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def upload_profile_photo(file, user_id):
    """
    Uploads a seller profile photo to Cloudinary.
    Crops to square for consistent display.
    """
    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/profiles",
            public_id=f"seller_{user_id}",
            overwrite=True,
            # Crop to square like a profile photo
            transformation=[
                {'width': 400, 'height': 400,
                 'crop': 'fill', 'gravity': 'face'},
                {'quality': 'auto'}
            ],
            resource_type='image'
        )

        return {
            'success': True,
            'url': result['secure_url']
        }

    except Exception as e:
        print(f"[CLOUDINARY] Profile photo upload failed: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }