import sys
import argparse
from app import create_app, db
from app.models import User

def create_user(username, password):
    app = create_app()
    with app.app_context():
        if User.query.filter_by(username=username).first():
            print(f"Error: User '{username}' already exists.")
            return False
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"User '{username}' created successfully.")
        return True

def list_users():
    app = create_app()
    with app.app_context():
        users = User.query.all()
        if not users:
            print("No users found.")
        for user in users:
            print(f"- {user.username} (created at: {user.created_at})")

def delete_user(username):
    app = create_app()
    with app.app_context():
        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"Error: User '{username}' not found.")
            return False
        
        db.session.delete(user)
        db.session.commit()
        print(f"User '{username}' deleted successfully.")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage RedTeam-Obsidian users.")
    subparsers = parser.add_subparsers(dest="command")

    # Create user
    create_parser = subparsers.add_parser("create", help="Create a new user")
    create_parser.add_argument("username", help="Username for the new account")
    create_parser.add_argument("password", help="Password for the new account")

    # List users
    subparsers.add_parser("list", help="List all users")

    # Delete user
    delete_parser = subparsers.add_parser("delete", help="Delete a user")
    delete_parser.add_argument("username", help="Username of the account to delete")

    args = parser.parse_args()

    if args.command == "create":
        create_user(args.username, args.password)
    elif args.command == "list":
        list_users()
    elif args.command == "delete":
        delete_user(args.username)
    else:
        parser.print_help()
