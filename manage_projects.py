import sys
import json
import argparse
import os
import shutil
import pyzipper
from app import create_app, db
from app.main import parse_iso_datetime
from app.models import Project, TimelineEvent, GraphNode, GraphEdge, User

def export_project(project_name, output_file, password):
    app = create_app()
    with app.app_context():
        project = Project.query.filter_by(name=project_name).first()
        if not project:
            print(f"Error: Project '{project_name}' not found.")
            return False
        
        data = {
            "project": project.to_dict(),
            "timeline_events": [e.to_dict() for e in TimelineEvent.query.filter_by(project_id=project.id).all()],
            "graph_nodes": [n.to_dict() for n in GraphNode.query.filter_by(project_id=project.id).all()],
            "graph_edges": [e.to_dict() for e in GraphEdge.query.filter_by(project_id=project.id).all()],
        }
        
        # We'll use a ZIP file to bundle JSON and uploads
        try:
            with pyzipper.AESZipFile(output_file, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(password.encode('utf-8'))
                
                # Write project metadata
                zf.writestr('project.json', json.dumps(data, indent=4))
                
                # Write uploaded files
                upload_dir = os.path.join(app.instance_path, "uploads", str(project.id))
                if os.path.exists(upload_dir):
                    for root, dirs, files in os.walk(upload_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.join('uploads', os.path.relpath(file_path, upload_dir))
                            zf.write(file_path, arcname)
            
            print(f"Project '{project_name}' exported securely to {output_file}")
            return True
        except Exception as e:
            print(f"Error during export: {str(e)}")
            return False

def import_project(input_file, password, new_name=None):
    app = create_app()
    with app.app_context():
        if not os.path.exists(input_file):
            print(f"Error: File '{input_file}' not found.")
            return False
        
        try:
            with pyzipper.AESZipFile(input_file, 'r') as zf:
                zf.setpassword(password.encode('utf-8'))
                
                # Test the password by reading project.json
                try:
                    with zf.open('project.json') as f:
                        data = json.load(f)
                except (RuntimeError, pyzipper.zipfile.BadZipFile):
                    print("Error: Invalid password or corrupted file.")
                    return False
                
                proj_data = data["project"]
                name = new_name if new_name else proj_data["name"]
                
                if Project.query.filter_by(name=name).first():
                    print(f"Error: Project '{name}' already exists.")
                    return False
                
                owner = User.query.first()
                if not owner:
                    print("Error: No users found. Create a user first using manage_users.py.")
                    return False
                
                project = Project(name=name, description=proj_data.get("description"), created_by_id=owner.id)
                db.session.add(project)
                db.session.commit()
                
                # Map old IDs to new IDs for nodes
                node_id_map = {}
                
                # Import Nodes (first pass)
                for n_data in data["graph_nodes"]:
                    old_id = n_data["id"]
                    node = GraphNode(
                        project_id=project.id,
                        title=n_data["title"],
                        caption=n_data["caption"],
                        notes=n_data["notes"],
                        files_json=json.dumps(n_data["files"]),
                        order_index=n_data["order_index"],
                        color=n_data["color"],
                        in_graph=n_data["in_graph"],
                        x=n_data["x"],
                        y=n_data["y"],
                        user_id=owner.id
                    )
                    db.session.add(node)
                    db.session.flush()
                    node_id_map[old_id] = node.id
                    
                # Import Nodes (second pass for parent_id)
                for n_data in data["graph_nodes"]:
                    if n_data.get("parent_id"):
                        new_id = node_id_map[n_data["id"]]
                        new_parent_id = node_id_map.get(n_data["parent_id"])
                        if new_parent_id:
                            node = db.session.get(GraphNode, new_id)
                            node.parent_id = new_parent_id

                # Import Timeline Events
                for e_data in data["timeline_events"]:
                    event = TimelineEvent(
                        project_id=project.id,
                        title=e_data["title"],
                        body=e_data["body"],
                        files_json=json.dumps(e_data["files"]),
                        occurred_at=parse_iso_datetime(e_data["occurred_at"]),
                        order_index=e_data["order_index"],
                        manual_order=e_data["manual_order"],
                        user_id=owner.id
                    )
                    db.session.add(event)
                    
                # Import Edges
                for e_data in data["graph_edges"]:
                    new_source_id = node_id_map.get(e_data["source_id"])
                    new_target_id = node_id_map.get(e_data["target_id"])
                    if new_source_id and new_target_id:
                        edge = GraphEdge(
                            project_id=project.id,
                            source_id=new_source_id,
                            target_id=new_target_id,
                            user_id=owner.id
                        )
                        db.session.add(edge)

                # Extract uploads
                new_upload_dir = os.path.join(app.instance_path, "uploads", str(project.id))
                os.makedirs(new_upload_dir, exist_ok=True)
                
                for member in zf.namelist():
                    if member.startswith('uploads/'):
                        # Strip 'uploads/' prefix
                        filename = os.path.relpath(member, 'uploads/')
                        if filename == '.': continue # skip the folder itself
                        
                        source = zf.open(member)
                        target_path = os.path.join(new_upload_dir, filename)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        
                        with source, open(target_path, "wb") as target:
                            shutil.copyfileobj(source, target)

                db.session.commit()
                print(f"Project '{name}' imported successfully from secure archive.")
                return True
        except Exception as e:
            print(f"Error during import: {str(e)}")
            return False

def list_projects():
    app = create_app()
    with app.app_context():
        projects = Project.query.all()
        for p in projects:
            print(f"- {p.name} (ID: {p.id}, created by: {p.created_by.username})")

def delete_project(project_name):
    app = create_app()
    with app.app_context():
        project = Project.query.filter_by(name=project_name).first()
        if not project:
            print(f"Error: Project '{project_name}' not found.")
            return False
        
        confirm = input(f"Are you sure you want to delete project '{project_name}'? This will delete ALL associated data and files. \nType the project name to confirm: ")
        if confirm != project_name:
            print("Confirmation failed. Deletion cancelled.")
            return False
            
        # Delete associated data
        TimelineEvent.query.filter_by(project_id=project.id).delete()
        
        nodes = GraphNode.query.filter_by(project_id=project.id).all()
        for node in nodes:
            for child in node.children:
                child.parent_id = None
        
        GraphEdge.query.filter_by(project_id=project.id).delete()
        GraphNode.query.filter_by(project_id=project.id).delete()
        
        db.session.delete(project)
        db.session.commit()
        
        project_upload_dir = os.path.join(app.instance_path, "uploads", str(project.id))
        if os.path.exists(project_upload_dir):
            shutil.rmtree(project_upload_dir)
            
        print(f"Project '{project_name}' deleted successfully.")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage RedTeam-Obsidian projects.")
    subparsers = parser.add_subparsers(dest="command")

    # Export
    export_parser = subparsers.add_parser("export", help="Export a project to a password-protected ZIP")
    export_parser.add_argument("name", help="Name of the project to export")
    export_parser.add_argument("file", help="Output ZIP file path")
    export_parser.add_argument("--password", required=True, help="Password for encryption")

    # Import
    import_parser = subparsers.add_parser("import", help="Import a project from a password-protected ZIP")
    import_parser.add_argument("file", help="Input ZIP file path")
    import_parser.add_argument("--password", required=True, help="Password for decryption")
    import_parser.add_argument("--name", help="New name for the imported project")

    # List
    subparsers.add_parser("list", help="List all projects")

    # Delete
    delete_parser = subparsers.add_parser("delete", help="Delete a project")
    delete_parser.add_argument("name", help="Name of the project to delete")

    args = parser.parse_args()

    if args.command == "export":
        export_project(args.name, args.file, args.password)
    elif args.command == "import":
        import_project(args.file, args.password, args.name)
    elif args.command == "list":
        list_projects()
    elif args.command == "delete":
        delete_project(args.name)
    else:
        parser.print_help()
