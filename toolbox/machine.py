
import subprocess
import socket
import os
import sys
from datetime import datetime

def get_hostname():
    return os.uname()[1]

def get_launch_info(string=True):
    launch_info = {}
    launch_info["interpreter"] = sys.executable
    launch_info["datetime"] = datetime.now()
    launch_info["args"] = sys.argv
    launch_info["hostname"] = get_hostname()
    launch_info["local_ip"] = get_local_ip()
    try:
        launch_info["git_branch"], launch_info["git_commit"] = git_info()
    except Exception as e:
        launch_info["git_branch"] = None
        launch_info["git_commit"] = None
    
    if not string:
        return launch_info

    
    launch_info_string = f"""
python {" ".join(sys.argv)}
{launch_info["datetime"]}
Env: {launch_info["interpreter"]}
"""
    if launch_info["git_branch"] is not None:
        launch_info_string += f"Git branch: {launch_info['git_branch']}, {launch_info['git_commit']}\n"
    launch_info_string += f"Host: {launch_info['hostname']} {launch_info['local_ip']}"

    return launch_info_string

def git_info(directory=None):
    """
    Retrieves the current Git branch name and commit hash for a given directory.
    Args:
        directory (str, optional): The directory to run the Git commands in. 
                                   Defaults to None, which uses the current working directory.
    Returns:
        tuple: A tuple containing the branch name and commit hash as strings.
               If the directory is not a Git repository, returns "Not a git repository".
    """
    
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=directory
        ).strip().decode('utf-8')
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=directory
        ).strip().decode('utf-8')
        return branch, commit
    except subprocess.CalledProcessError:
        raise ValueError("Not a git repository")

    
def get_local_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))  # Google's public DNS
        local_ip = s.getsockname()[0]

    return local_ip
