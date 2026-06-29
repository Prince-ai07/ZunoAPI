# run.py
#
# This is the entry point of the entire Zuno backend.
# When you type "python run.py" in the terminal, this file runs first.
#
# It creates the Flask app using our factory function and starts
# the development server so we can test our API endpoints.
#
# TO START THE SERVER: python run.py
# TO STOP THE SERVER:  Ctrl + C

from app import create_app

# Create the Zuno application
app = create_app()

if __name__ == '__main__':
    print("=" * 50)
    print("  ZUNO BACKEND SERVER STARTING")
    print("  Environment: Development")
    print("  URL: http://localhost:5000")
    print("=" * 50)

    # debug=True means:
    # 1. Server auto-restarts when you change any code file
    # 2. Shows detailed error messages in the browser
    # 3. NEVER use debug=True in production — it exposes internals
    #
    # host='0.0.0.0' means the server is accessible from any
    # device on your local network (useful for testing on phone)
    app.run(debug=True, host='0.0.0.0', port=5000)