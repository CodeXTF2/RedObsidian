"""Project management from the command line. Export/import is also available in each project's Settings.

Export passwords are prompted for (or read from REDOBSIDIAN_EXPORT_PASSWORD), never taken as arguments.
"""
import argparse
import getpass
import os
import sys

from app import audit, create_app, db
from app.maintenance import delete_project_data
from app.models import PROJECT_ROLES, Project, ProjectMember, User


def export_password(confirm):
    password = os.environ.get("REDOBSIDIAN_EXPORT_PASSWORD")
    if password:
        return password
    password = getpass.getpass("Export password: ")
    if confirm and password != getpass.getpass("Repeat password: "):
        sys.exit("Error: passwords don't match.")
    return password


def find_project(name):
    matches = Project.query.filter_by(name=name).all()
    if not matches and name.isdigit():
        project = db.session.get(Project, int(name))
        matches = [project] if project else []
    if not matches:
        sys.exit(f"Error: project '{name}' not found.")
    if len(matches) > 1:
        sys.exit(f"Error: several projects are named '{name}'; use the project ID instead: "
                 + ", ".join(str(p.id) for p in matches))
    return matches[0]


def find_user(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        sys.exit(f"Error: user '{username}' not found.")
    return user


def cmd_list(args):
    for project in Project.query.order_by(Project.created_at).all():
        owners = ", ".join(m.user.username for m in project.members if m.role == "owner")
        status = " [archived]" if project.archived_at else ""
        print(f"- {project.name}{status} (ID: {project.id}, owners: {owners or '-'}, members: {len(project.members)})")


def cmd_export(args):
    from app.transfer import TransferError, export_project

    project = find_project(args.name)
    try:
        export_project(project, args.file, export_password(confirm=True))
    except TransferError as exc:
        sys.exit(f"Error: {exc}")
    audit.record("project.exported", user=None, username="cli", project_id=project.id, target_type="project",
                 target_id=project.id)
    print(f"Project '{project.name}' exported to {args.file}")


def cmd_import(args):
    from app.transfer import TransferError, import_project

    owner = find_user(args.owner) if args.owner else User.query.filter_by(is_admin=True).order_by(User.id).first()
    if not owner:
        sys.exit("Error: no owner. Pass --owner <username> or create an administrator first.")
    if not os.path.isfile(args.file):
        sys.exit(f"Error: file '{args.file}' not found.")
    try:
        project = import_project(args.file, export_password(confirm=False), owner, args.name)
    except TransferError as exc:
        sys.exit(f"Error: {exc}")
    audit.record("project.imported", user=None, username="cli", project_id=project.id, target_type="project",
                 target_id=project.id, name=project.name, owner=owner.username)
    print(f"Imported '{project.name}' (ID {project.id}), owned by {owner.username}.")


def cmd_delete(args):
    project = find_project(args.name)
    confirm = input(f"Delete project '{project.name}' and ALL its data and files? Type the project name to confirm: ")
    if confirm != project.name:
        sys.exit("Confirmation failed. Nothing was deleted.")
    name, project_id = project.name, project.id
    delete_project_data(project)
    audit.record("project.deleted", user=None, username="cli", project_id=project_id, target_type="project",
                 target_id=project_id, name=name)
    print(f"Project '{name}' deleted.")


def cmd_member(args):
    project = find_project(args.name)
    user = find_user(args.username)
    member = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if args.role == "remove":
        if member:
            db.session.delete(member)
        audit.record("member.removed", user=None, username="cli", project_id=project.id, target_type="user",
                     target_id=user.id, member=user.username)
        print(f"Removed {user.username} from '{project.name}'.")
        return
    if member:
        member.role = args.role
    else:
        db.session.add(ProjectMember(project_id=project.id, user_id=user.id, role=args.role))
    audit.record("member.added", user=None, username="cli", project_id=project.id, target_type="user",
                 target_id=user.id, member=user.username, role=args.role)
    print(f"{user.username} is now {args.role} of '{project.name}'.")


def main():
    parser = argparse.ArgumentParser(description="Manage RedObsidian projects.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all projects").set_defaults(func=cmd_list)

    export = subparsers.add_parser("export", help="Export a project to an AES-encrypted ZIP (prompts for a password)")
    export.add_argument("name", help="Project name or ID")
    export.add_argument("file", help="Output ZIP path")
    export.set_defaults(func=cmd_export)

    imp = subparsers.add_parser("import", help="Import a project export (prompts for the password)")
    imp.add_argument("file", help="Input ZIP path")
    imp.add_argument("--name", help="Name for the imported project")
    imp.add_argument("--owner", help="Username that will own it (default: first administrator)")
    imp.set_defaults(func=cmd_import)

    delete = subparsers.add_parser("delete", help="Delete a project and its files")
    delete.add_argument("name", help="Project name or ID")
    delete.set_defaults(func=cmd_delete)

    member = subparsers.add_parser("member", help="Add, change or remove a project member")
    member.add_argument("name", help="Project name or ID")
    member.add_argument("username")
    member.add_argument("role", choices=[*PROJECT_ROLES, "remove"])
    member.set_defaults(func=cmd_member)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    app = create_app()
    with app.app_context():
        args.func(args)
        db.session.commit()


if __name__ == "__main__":
    main()
