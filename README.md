# Huddle Backend - API Documentation

## Table of Contents

-   [Introduction](#introduction)
-   [Project Structure](#project-structure)
-   [Features](#features)
-   [Getting Started](#getting-started)
    -   [Prerequisites](#prerequisites)
    -   [Installation](#installation)
    -   [Configuration](#configuration)
    -   [Running the Application](#running-the-application)
-   [API Endpoints](#api-endpoints)
    -   [Auth](#auth)
    -   [User](#user)
    -   [Friend](#friend)
    -   [Task](#task)
    -   [Workroom](#workroom)
    -    [Dashboard](#dashboard)
    -   [Achievement](#achievement)
-   [Database](#database)
-   [Authentication](#authentication)
-   [Middleware](#middleware)
-   [Docker](#docker)
-   [Contributing](#contributing)
-   [License](#license)
-   [Contact](#contact)

## Introduction

The Huddle Backend provides the API for the Huddle application, a platform designed to enhance productivity and collaboration.  This README serves as a guide for developers looking to understand, set up, and contribute to the backend. The backend is built using FastAPI.

## Project Structure

The project is organized as follows:

backend_venv/      # Virtual environment (not part of the repo)src/             # Source code root├── achievements/ # Achievement-related logic (levels, etc.)├── auth/        # Authentication and authorization├── dashboard/    #  Dashboard functionality├── db/          # Database models and connections├── friend/      # Friend management├── tasks/       # Task management├── workroom/    # Workroom functionality├── init.py  # Initializes the src directory├── config.py    # Project configuration├── mail.py      # Email sending utilities├── main.py      # Main application entry point├── manager.py   # Database migration management (Alembic)├── middleware.py #  Middleware components.env            # Environment configuration file.gitignore       # Specifies intentionally untracked files that Git should ignoredocker-compose.yml # Docker Compose configurationDockerfile       # Docker image definitionREADME.md        # This filerequirements.txt  # Project dependencies
## Features

The Huddle Backend provides the following key features:

* **User Authentication:** Secure user registration, login, and password management.
* **User Profiles:** Management of user data, including updates.
* **Friend Management:** Functionality for users to connect with friends.
* **Task Management:** Creation, assignment, and tracking of tasks.
* **Workrooms:** Collaborative spaces for users to work together.
* **Dashboard:** Provides a summary of user activity and progress.
* **Achievements:** Tracks and rewards user accomplishments.
* **Database Interaction:** Uses PostgreSQL for data storage.
* **Email Sending:** Support for sending emails (e.g., for verification).
* **Docker Support:** Easy setup and deployment with Docker and Docker Compose.
* **Async Support:** Built using FastAPI, leveraging asynchronous operations for performance.
* **Middleware:** Includes custom middleware.

## Getting Started

### Prerequisites

Before you begin, ensure you have the following installed:

* **Python 3.11+**
* **PostgreSQL**
* **Docker**
* **pip** (Python package installer)

### Installation

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/Hudddle-io/backend.hudddle.git
    cd backend.hudddle
    ```

2.  **Create a virtual environment (recommended):**

    ```bash
    python3 -m venv backend_venv
    source backend_venv/bin/activate  # On Linux/macOS
    backend_venv\Scripts\activate.bat # On Windows
    ```

3.  **Install the dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

### Configuration

1.  **Create a `.env` file:** Copy the `.env.example` file to `.env` and modify the variables to match your environment.  This file contains sensitive information such as database URLs, email settings, and secret keys.

    ```bash
    cp .env.example .env
    #  Edit .env
    ```

2.  **Database Configuration:** Ensure your PostgreSQL database is running and the connection details in `.env` are correct.

### Running the Application

1.  **Run database migrations:**

    ```bash
    alembic upgrade head
    ```

2.  **Start the application:**

    ```bash
    uvicorn src.main:app --reload
    ```

    The `--reload` flag enables hot reloading, so the server will automatically restart when you make changes to the code.

## API Endpoints

### Auth

* `POST /auth/register`: Register a new user.
* `POST /auth/login`: Log in and receive an access token.
* `POST /auth/logout`: Log out (invalidate token).
* `POST /auth/verify-email`: Verify user email.
* `POST /auth/reset-password-request`: Request a password reset.
* `POST /auth/reset-password`: Reset user password.
* `GET /auth/me`: Get the current user's details.
* `PUT /auth/update-profile`: Update the current user's profile.

### User

* `GET /users/{user_id}`: Get a specific user by ID.

### Friend

* `GET /friends/friends`: Get the current user's friends.
* `GET /friends/friends/search`: Search friend by email

### Task

* `GET /tasks`: Get all tasks created by the current user.
* `GET /tasks/{task_id}`: Get a specific task.

### Workroom

* `GET /workrooms`: Get all workrooms.
* `GET /workrooms/{workroom_id}`: Get a specific workroom.

### Dashboard

* `GET /`: Retrieves dashboard data for the current user.

### Achievement

* `GET /achievements/users/me/levels`: Get the current user's levels.
* `GET /achievements/levels`: Get all user levels.
* `GET /achievements/users/me/streak`: Gets the current user's streak.

## Database

The application uses PostgreSQL as its database.  The database schema is defined using SQLAlchemy.  Database migrations are managed using Alembic.

## Authentication

Authentication is handled using JWT (JSON Web Tokens).  The `auth` module provides the necessary functionality for user registration, login, and token management.

## Middleware

The application includes middleware for:

* Handling exceptions.
* Logging requests.

## Docker

The application can be easily deployed using Docker.  The provided `Dockerfile` and `docker-compose.yml` files define the application's environment and dependencies.

1.  **Build the Docker image:**

    ```bash
    docker build -t huddle-backend .
    ```

2.  **Run the application with Docker Compose:**

    ```bash
    docker-compose up -d
    ```
