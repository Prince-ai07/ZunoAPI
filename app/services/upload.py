# app/services/upload.py
#
# Handles all file uploads to Cloudinary for Zuno.
#
# FIX APPLIED:
# Cloudinary configuration is now done INSIDE each function
# instead of at module level. This ensures .env values are
# always loaded before Cloudinary tries to read them.
# Configuring at module level caused "Must supply api_key"
# because the file was imported before load_dotenv() ran.

import cloudinary
import cloudinary.uploader
import os


def _configure_cloudinary():
    """
    Configures Cloudinary with credentials from .env.
    Called at the start of every upload function to ensure
    credentials are always available when needed.
    
    We do this inside functions rather than at module level
    because module-level code runs at import time, which can
    be before load_dotenv() has populated os.environ.
    """
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        secure=True
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
    # Configure Cloudinary fresh before every upload
    _configure_cloudinary()

    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/kyc/seller_{seller_id}",
            public_id=document_type,
            overwrite=True,
            tags=[f"seller_{seller_id}", "kyc", document_type],
            resource_type='auto'
        )

        return {
            'success': True,
            'url': result['secure_url'],
            'public_id': result['public_id']
        }

    except Exception as e:
        import traceback
        print(f"[CLOUDINARY] Upload failed: {str(e)}")
        print(f"[CLOUDINARY] Full error: {traceback.format_exc()}")
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
    _configure_cloudinary()

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
    _configure_cloudinary()

    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/products/seller_{seller_id}",
            tags=[f"seller_{seller_id}", "product"],
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
    _configure_cloudinary()

    try:
        result = cloudinary.uploader.upload(
            file,
            folder=f"zuno/profiles",
            public_id=f"seller_{user_id}",
            overwrite=True,
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