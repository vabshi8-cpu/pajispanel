import docker
import subprocess
import secrets
import string
import time
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


# --- Bridge functions expected by app.py ---

def create_vps_container(username, cpu_limit=1, ram_limit_mb=1024, disk_limit_gb=10, image="ubuntu-vps:latest"):
    """
    Wrapper matching app.py's expected signature: returns (container_id, ssh_command)
    instead of create_vps()'s dict. Uses tmate for SSH access instead of password auth,
    since regen_tmate() assumes a tmate session is running in the container.
    Requires tmate to be installed in the base image.
    """
    result = create_vps(username, cpu_limit, ram_limit_mb, disk_limit_gb, image)
    if "error" in result:
        raise RuntimeError(result["error"])

    container = get_container(result["container_id"])
    ssh_command = _start_tmate_session(container)

    return result["container_id"], ssh_command


def _start_tmate_session(container):
    """Start (or restart) a tmate session inside the container and return the ssh connection string."""
    container.exec_run("tmate -S /tmp/tmate.sock new-session -d", user="root")
    container.exec_run("tmate -S /tmp/tmate.sock wait tmate-ready", user="root")
    result = container.exec_run("tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'", user="root")
    ssh_line = result.output.decode(errors="replace").strip()
    return ssh_line if ssh_line else None


def regen_tmate(container_id):
    """Kill and restart the tmate session, returning the new ssh connection string."""
    c = get_container(container_id)
    if not c:
        raise RuntimeError("Container not found")
    c.exec_run("pkill tmate", user="root")
    return _start_tmate_session(c)


def destroy_vps(container_id):
    """Alias to match app.py's naming."""
    return delete_vps(container_id)


def suspend_vps(container_id):
    """Pause the container (freezes processes without stopping/losing state)."""
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    c.pause()
    return {"status": "suspended"}


def unsuspend_vps(container_id):
    c = get_container(container_id)
    if not c:
        return {"error": "not found"}
    c.unpause()
    return {"status": "running"}


def get_container_stats(container_id, created_at=None):
    """One-shot (non-streaming) stats snapshot, formatted for the /vps/<id>/stats endpoint."""
    c = get_container(container_id)
    if not c:
        raise RuntimeError("Container not found")

    raw = c.stats(stream=False)

    cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
    system_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"].get("system_cpu_usage", 0)
    num_cpus = raw["cpu_stats"].get("online_cpus") or len(raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
    cpu_percent = 0.0
    if system_delta > 0 and cpu_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

    mem_usage = raw["memory_stats"].get("usage", 0)
    mem_limit = raw["memory_stats"].get("limit", 1)
    mem_percent = (mem_usage / mem_limit) * 100.0 if mem_limit else 0.0

    uptime_seconds = int(time.time() - created_at) if created_at else None

    return {
        "cpu_percent": round(cpu_percent, 2),
        "mem_usage_mb": round(mem_usage / (1024 * 1024), 1),
        "mem_limit_mb": round(mem_limit / (1024 * 1024), 1),
        "mem_percent": round(mem_percent, 2),
        "uptime_seconds": uptime_seconds,
    }


def stats(container_id):
    """
    Alias for monitor.py: same data as get_container_stats, but keyed as
    'cpu' (percent) since that's the field name monitor.py's watch() loop checks.
    """
    s = get_container_stats(container_id)
    return {
        "cpu": s["cpu_percent"],
        "mem_percent": s["mem_percent"],
        "mem_usage_mb": s["mem_usage_mb"],
        "mem_limit_mb": s["mem_limit_mb"],
    }


def suspend(container_id):
    """Alias for monitor.py's naming."""
    return suspend_vps(container_id)


def build_logs_stream(container_id):
    """
    Generator yielding decoded log lines as they arrive.
    Call with the container_id (vps['container_id']), not the DB row id.
    """
    c = get_container(container_id)
    if not c:
        yield "Container not found"
        return
    for chunk in c.logs(stream=True, follow=True, tail=100):
        yield chunk.decode(errors="replace").rstrip("\n")
