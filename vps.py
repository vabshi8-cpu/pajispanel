import docker
import subprocess
import secrets
import string
from docker.errors import NotFound, APIError

client = docker.from_env()

def generate_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def get_container(container_id):
    try:
        return client.containers.get(container_id)
    except NotFound:
        return None

def create_vps(username, cpu_limit=1, ram_limit_mb=1024, disk_limit_gb=10, image="ubuntu-vps:latest"):
    """
    Create a VPS container with LXCFS mounts so /proc and /sys reflect
    container limits instead of host specs.
    """
    password = generate_password()
    container_name = f"vps_{username}_{secrets.token_hex(4)}"

    # LXCFS mounts — this is the fix for neofetch/free/df showing host specs
    lxcfs_binds = {
        '/var/lib/lxcfs/proc/cpuinfo': {'bind': '/proc/cpuinfo', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/diskstats': {'bind': '/proc/diskstats', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/meminfo': {'bind': '/proc/meminfo', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/stat': {'bind': '/proc/stat', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/swaps': {'bind': '/proc/swaps', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/uptime': {'bind': '/proc/uptime', 'mode': 'rw'},
        '/var/lib/lxcfs/proc/loadavg': {'bind': '/proc/loadavg', 'mode': 'rw'},
        '/var/lib/lxcfs/sys/devices/system/cpu/online': {'bind': '/sys/devices/system/cpu/online', 'mode': 'rw'},
    }

    try:
        container = client.containers.run(
            image,
            detach=True,
            name=container_name,
            hostname=username,
            tty=True,
            stdin_open=True,
            cpu_quota=int(cpu_limit * 100000),
            cpu_period=100000,
            mem_limit=f"{ram_limit_mb}m",
            memswap_limit=f"{ram_limit_mb}m",
            storage_opt={"size": f"{disk_limit_gb}G"} if _supports_storage_opt() else None,
            volumes=lxcfs_binds,
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges"],
            restart_policy={"Name": "unless-stopped"},
            environment={
                "ROOT_PASSWORD": password,
                "USERNAME": username,
            },
            command="/usr/sbin/sshd -D",
            ports={'22/tcp': None},  # random host port
        )

        container.reload()
        ssh_port = _get_ssh_port(container)

        # Set root password inside container
        _set_root_password(container, password)

        return {
            "container_id": container.id,
            "container_name": container_name,
            "ssh_port": ssh_port,
            "password": password,
            "status": "running"
        }
    except APIError as e:
        return {"error": str(e)}

def _supports_storage_opt():
    """Check if docker storage driver supports size quota (overlay2 with xfs pquota)."""
    try:
        info = client.info()
        driver = info.get("Driver", "")
        return driver == "overlay2"
    except Exception:
        return False

def _get_ssh_port(container):
    try:
        ports = container.attrs["NetworkSettings"]["Ports"]
        return int(ports["22/tcp"][0]["HostPort"])
    except (KeyError, TypeError, IndexError):
        return None

def _set_root_password(container, password):
    try:
        container.exec_run(f"bash -c 'echo root:{password} | chpasswd'", user="root")
    except Exception as e:
        print(f"Failed setting password: {e}")

def start_vps(container_id):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    c.start()
    return {"status": "started"}

def stop_vps(container_id):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    c.stop(timeout=10)
    return {"status": "stopped"}

def restart_vps(container_id):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    c.restart(timeout=10)
    return {"status": "restarted"}

def delete_vps(container_id):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    try:
        c.stop(timeout=5)
    except Exception:
        pass
    c.remove(force=True)
    return {"status": "deleted"}

def regenerate_ssh(container_id):
    """Generate a new password and reset SSH host keys inside container."""
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}

    new_password = generate_password()

    try:
        # Reset root password
        c.exec_run(f"bash -c 'echo root:{new_password} | chpasswd'", user="root")

        # Regenerate SSH host keys
        c.exec_run("rm -f /etc/ssh/ssh_host_*", user="root")
        c.exec_run("ssh-keygen -A", user="root")

        # Restart sshd (kill + let container restart policy handle, or exec restart)
        c.exec_run("bash -c 'pkill sshd; /usr/sbin/sshd -D &'", user="root", detach=True)

        c.reload()
        ssh_port = _get_ssh_port(c)

        return {
            "status": "regenerated",
            "password": new_password,
            "ssh_port": ssh_port
        }
    except APIError as e:
        return {"error": str(e)}

def get_vps_status(container_id):
    c = get_container(container_id)
    if not c:
        return {"status": "not_found"}
    c.reload()
    return {
        "status": c.status,
        "ssh_port": _get_ssh_port(c),
        "name": c.name,
    }

def list_all_vps():
    containers = client.containers.list(all=True, filters={"name": "vps_"})
    return [{
        "id": c.id,
        "name": c.name,
        "status": c.status,
        "ssh_port": _get_ssh_port(c),
    } for c in containers]

def exec_in_vps(container_id, cmd):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    result = c.exec_run(cmd, user="root")
    return {
        "exit_code": result.exit_code,
        "output": result.output.decode(errors="replace")
    }
