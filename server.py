import os
import select
import subprocess
import threading
import psutil
from flask import Flask, send_from_directory, jsonify, request, send_file
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import time
import signal
import zipfile
import shutil

# Flask app initialization
app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)  # Allow cross-origin requests
START_TIME = time.time()

# Set working directory to /home
HOME_DIR = os.path.join(os.getcwd(), 'home')

# Ensure the home directory exists
os.makedirs(HOME_DIR, exist_ok=True)
def safe_path(path):
    abs_path = os.path.abspath(os.path.join(HOME_DIR, path))
    return abs_path if abs_path.startswith(HOME_DIR) else None

# Flask & Socket.IO setup
socketio = SocketIO(app, cors_allowed_origins="*")

# Hardcoded server stats (Modify based on actual system stats)

# Define allowed commands for security
ALLOWED_COMMANDS = {"ls", "cat", "echo", "python", "pip", "python3", "node", "npm", "yarn", "gitclone", "pwd", "zip", "unzip", "ping"}  # Add only safe commands

@app.route('/')
def serve_index():
    """Serve index.html from the static folder."""
    return send_from_directory(app.static_folder, "index.html")

def get_cpu_and_uptime():
    """Fetch real-time CPU usage and properly format uptime."""

    # Get accurate CPU usage inside Docker
    cpu_usage = psutil.cpu_percent(interval=0.5)

    # Calculate uptime
    total_minutes = int((time.time() - START_TIME) // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60

    uptime_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    return {
        "cpu": f"{cpu_usage}%",
        "ram": "16GB",  # Hardcoded
        "disk": "24GB",  # Hardcoded
        "uptime": uptime_str
    }

@app.route('/stats', methods=['GET'])
def stats():
    """Return CPU and uptime stats."""
    return jsonify(get_cpu_and_uptime())

@app.route('/myfiles')
def serve_file():
    """Serve the file.html as the base page."""
    return send_from_directory("static", "file.html")

processes = {}  # Store running processes per session

@socketio.on('command')
def handle_command(data):
    """Execute only whitelisted commands in /home directory with input handling."""
    full_command = data.get("command", "").strip()
    base_command = full_command.split(" ")[0]

    if base_command not in ALLOWED_COMMANDS:
        socketio.emit("output", {"response": f"ERROR: Command '{base_command}' is not allowed."}, room=request.sid)
        return

    if request.sid in processes:
        socketio.emit("output", {"response": "ERROR: Another command is already running. Wait until it finishes."}, room=request.sid)
        return

    socketio.emit("command_started", room=request.sid)  # Notify frontend

    def run_command(sid):
        try:
            process = subprocess.Popen(
                full_command,
                shell=True,
                cwd=HOME_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,  # Enable input handling
                universal_newlines=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True
            )
            processes[sid] = process

            # Read stdout and stderr without blocking
            while process.poll() is None:
                read_fds, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)

                for stream in read_fds:
                    line = stream.readline().strip()
                    if line:
                        if stream == process.stdout:
                            socketio.emit("output", {"response": line}, room=sid)
                        else:
                            socketio.emit("output", {"response": f"ERROR: {line}"}, room=sid)

                socketio.sleep(0.1)

            # Read remaining output after process ends
            for stream in (process.stdout, process.stderr):
                for line in stream:
                    line = line.strip()
                    if line:
                        if stream == process.stdout:
                            socketio.emit("output", {"response": line}, room=sid)
                        else:
                            socketio.emit("output", {"response": f"ERROR: {line}"}, room=sid)

            process.stdout.close()
            process.stderr.close()
            process.stdin.close()

        except Exception as e:
            socketio.emit("output", {"response": f"Error executing command: {str(e)}"}, room=sid)

        finally:
            processes.pop(sid, None)
            socketio.emit("command_ended", room=sid)  # Notify frontend

    # ✅ Use `start_background_task()` so Flask doesn't freeze
    socketio.start_background_task(run_command, request.sid)

@socketio.on('input')
def handle_input(data):
    """Send user input to the running process."""
    input_text = data.get("text", "").strip()

    if not input_text:
        return  # Ignore empty input

    if not processes:
        socketio.emit("output", {"response": "ERROR: No running process to send input to."})
        return

    for process in processes.values():  # Send input to all running processes
        if process.poll() is None:  # If still running
            try:
                process.stdin.write(input_text + "\n")
                process.stdin.flush()
            except Exception as e:
                socketio.emit("output", {"response": f"ERROR: Failed to send input: {str(e)}"})

@socketio.on('stop')
def stop_all_commands(_):
    """Stop all running commands and their child processes."""
    if not processes:
        socketio.emit("output", {"response": "No running commands to stop."})
        return

    stopped_count = 0

    for session_id, process in list(processes.items()):  # Use list() to avoid modification issues
        try:
            # Terminate all child processes first
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)

            for child in children:
                child.terminate()

            _, still_alive = psutil.wait_procs(children, timeout=3)
            for child in still_alive:
                child.kill()  # Force kill if needed

            # Now stop the main process
            if process.poll() is None:  # If still running
                process.terminate()
                process.wait(timeout=3)

                if process.poll() is None:
                    process.kill()

            del processes[session_id]  # Remove from tracking
            stopped_count += 1
        except Exception as e:
            socketio.emit("output", {"response": f"Error stopping process {session_id}: {str(e)}"})

    socketio.emit("output", {"response": f"Stopped {stopped_count} running command(s)."})

@app.route('/files', methods=['GET'])
def list_files():
    """Recursively list all files and directories inside /home."""
    def scan_directory(path):
        result = []
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            result.append({
                "name": item,
                "is_directory": os.path.isdir(item_path),
                "size": os.path.getsize(item_path),
                "last_modified": os.path.getmtime(item_path),
            })
        return result

    return jsonify(scan_directory(HOME_DIR))

@app.route('/files/<path:filepath>', methods=['GET'])
def get_file(filepath):
    """Retrieve the content of a file inside /home."""
    file_path = safe_path(filepath)
    if not file_path or not os.path.exists(file_path) or os.path.isdir(file_path):
        return jsonify({"error": "File not found"}), 404

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    return jsonify({"content": content})

@app.route('/edit', methods=['POST'])
def edit_file():
    """Edit an existing file or create a new one inside /home."""
    filename = request.json.get("filename")
    content = request.json.get("content")
    file_path = safe_path(filename)

    allowed_extensions = {".py", ".js", ".env", ".mjs", ".json", ".db"}

    if not file_path or not any(filename.endswith(ext) for ext in allowed_extensions):
        return jsonify({"error": "Invalid filename"}), 400

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return jsonify({"message": f"{filename} saved successfully"}), 200

@app.route('/delete', methods=['POST'])
def delete_file_or_dir():
    """Deletes a file, empty directory, or non-empty directory."""
    filename = request.json.get("filename")
    if not filename:
        return jsonify({"error": "Missing filename"}), 400

    file_path = safe_path(filename)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File or directory does not exist"}), 404

    try:
        if os.path.isdir(file_path):
            shutil.rmtree(file_path)  # Delete even if non-empty
            return jsonify({"message": f"Deleted directory: {filename}"}), 200
        else:
            os.remove(file_path)
            return jsonify({"message": f"Deleted file: {filename}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/mkdir', methods=['POST'])
def create_directory():
    """Create a new subdirectory inside /home."""
    dirname = request.json.get("dirname")
    dir_path = safe_path(dirname)

    if not dir_path:
        return jsonify({"error": "Invalid directory name"}), 400

    os.makedirs(dir_path, exist_ok=True)
    return jsonify({"message": f"Directory '{dirname}' created successfully"}), 201

@app.route('/rename', methods=['POST'])
def rename_file():
    """Rename a file or directory inside /home."""
    old_name = request.json.get("old_name")
    new_name = request.json.get("new_name")

    old_path = safe_path(old_name)
    new_path = safe_path(new_name)

    if not old_path or not new_path:
        return jsonify({"error": "Invalid filename"}), 400

    if not os.path.exists(old_path):
        return jsonify({"error": "File not found"}), 404

    if os.path.exists(new_path):
        return jsonify({"error": "A file with the new name already exists"}), 409

    os.rename(old_path, new_path)
    return jsonify({"message": f"Renamed '{old_name}' to '{new_name}' successfully"}), 200

@app.route('/dir/<path:foldername>', methods=['GET'])
def list_directory(foldername):
    """List contents of a specific directory inside /home."""
    dir_path = safe_path(foldername)
    
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({"error": "Directory not found"}), 404

    return jsonify(scan_directory(dir_path))

def scan_directory(path):
    """Helper function to get files and directories inside a path."""
    result = []
    try:
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            result.append({
                "name": item,
                "is_directory": os.path.isdir(item_path),
                "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None,
                "last_modified": os.path.getmtime(item_path)
            })
    except Exception as e:
        return {"error": str(e)}

    return result

@app.route('/unzip', methods=['POST'])
def unzip_file():
    """Extracts a zip file to the current directory user is in."""
    filename = request.json.get("filename")  # Name of the zip file
    path = request.json.get("path", "").strip()  # User's current path

    target_dir = safe_path(path) if path else HOME_DIR
    file_path = os.path.join(target_dir, os.path.basename(filename))  # Ensure it's inside target_dir

    if not file_path.endswith(".zip") or not os.path.exists(file_path):
        return jsonify({"error": "Invalid or missing zip file"}), 400

    if not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid target directory"}), 400

    try:
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)

        return jsonify({"message": f"Extracted {filename} to {target_dir}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/create', methods=['POST'])
def create_file():
    """Create a new empty file inside /home."""
    filename = request.json.get("filename")
    file_path = safe_path(filename)

    if not file_path:
        return jsonify({"error": "Invalid filename"}), 400

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("")  # Create an empty file
        return jsonify({"message": f"{filename} created successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload a file to a subdirectory inside /home."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    path = request.form.get("path", "").strip()
    target_dir = safe_path(path) if path else HOME_DIR

    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid target directory"}), 400

    file = request.files['file']
    file_path = os.path.join(target_dir, file.filename)
    file.save(file_path)

    return jsonify({"message": f"{file.filename} uploaded successfully"}), 200

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=7860)
