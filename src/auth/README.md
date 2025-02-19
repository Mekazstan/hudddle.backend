### Model
- Fields:
     - `user_id` (UUID or ObjectId)
     - `username` (String)
     - `email` (String, unique)
     - `password_hash` (String, hashed)
     - `role` (String, e.g., "admin", "member")
     - `xp` (Integer, default: 0)
     - `level` (Integer, default: 1)
     - `badges` (List of Strings, e.g., ["First Task Completed"])
     - `avatar_url` (String, optional)
     - `created_at` (DateTime)
     - `updated_at` (DateTime)

### Services
   - Create/Register a new user.
   - Authenticate a user (login).
   - Update user profile (e.g., avatar, preferences).
   - Fetch user details (e.g., XP, level, badges).
   - Verify user account.
   - Get a new access token using a refresh token.
   - Logout user (revoke token).
   - Get current user details.
   - Request password reset.
   - Confirm password reset.
   - Send email (general purpose).

   

### Routes
   - `GET /users/{user_id}` - Fetch user details.
   - `PUT /users/{user_id}` - Update user profile.
   - `POST /auth/create` - Register a new user.
   - `POST /auth/login` - Authenticate a user.
   - `GET /auth/verify/{token}` - Verify user account.
   - `GET /auth/refresh_token` - Get a new access token using a refresh token.
   - `GET /auth/logout` - Logout user (revoke token).
   - `GET /auth/me` - Get current user details.
   - `POST /auth/password-reset-request` - Request password reset.
   - `POST /auth/password-reset-confirm/{token}` - Confirm password reset.
   - `POST /auth/send_mail` - Send email (general purpose).