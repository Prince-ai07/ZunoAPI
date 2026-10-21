# app/utils/errors.py
#
# Global error handlers for Zuno.
# Instead of Flask returning ugly HTML error pages,
# these handlers return clean JSON responses for every
# possible error code. This is critical for an API.

from flask import jsonify


def register_error_handlers(app):
    """
    Registers all error handlers with the Flask app.
    Called once in create_app() in app/__init__.py.
    """

    @app.errorhandler(400)
    def bad_request(e):
        """
        400 — Bad Request.
        Client sent invalid or malformed data.
        """
        return jsonify({
            'error': 'Bad request',
            'message': str(e),
            'status': 400
        }), 400

    @app.errorhandler(401)
    def unauthorized(e):
        """
        401 — Unauthorized.
        Request requires authentication but none provided.
        """
        return jsonify({
            'error': 'Unauthorized',
            'message': 'Authentication required. '
                       'Please log in and include your token.',
            'status': 401
        }), 401

    @app.errorhandler(403)
    def forbidden(e):
        """
        403 — Forbidden.
        Authenticated but not allowed to do this action.
        """
        return jsonify({
            'error': 'Forbidden',
            'message': 'You do not have permission '
                       'to perform this action.',
            'status': 403
        }), 403

    @app.errorhandler(404)
    def not_found(e):
        """
        404 — Not Found.
        The requested URL or resource does not exist.
        """
        return jsonify({
            'error': 'Not found',
            'message': 'The requested resource does not exist.',
            'status': 404
        }), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        """
        405 — Method Not Allowed.
        e.g. sending GET to a POST-only endpoint.
        """
        return jsonify({
            'error': 'Method not allowed',
            'message': 'This HTTP method is not allowed '
                       'for this endpoint.',
            'status': 405
        }), 405

    @app.errorhandler(409)
    def conflict(e):
        """
        409 — Conflict.
        Resource already exists or state conflict.
        """
        return jsonify({
            'error': 'Conflict',
            'message': str(e),
            'status': 409
        }), 409

    @app.errorhandler(422)
    def unprocessable(e):
        """
        422 — Unprocessable Entity.
        Data format is correct but values are invalid.
        """
        return jsonify({
            'error': 'Unprocessable entity',
            'message': str(e),
            'status': 422
        }), 422

    @app.errorhandler(429)
    def too_many_requests(e):
        """
        429 — Too Many Requests.
        Rate limit exceeded.
        """
        return jsonify({
            'error': 'Too many requests',
            'message': 'You are making too many requests. '
                       'Please slow down and try again.',
            'status': 429
        }), 429

    @app.errorhandler(500)
    def internal_error(e):
        """
        500 — Internal Server Error.
        Something went wrong on our side.
        We log it but return a safe generic message.
        Never expose internal error details to users.
        """
        print(f"[SERVER ERROR] {str(e)}")
        return jsonify({
            'error': 'Internal server error',
            'message': 'Something went wrong on our end. '
                       'Please try again or contact support.',
            'status': 500
        }), 500